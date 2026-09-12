"""FinPilot Live backend: FastAPI + simulated-day scheduler + WebSocket push.

- Loads CSVs into pandas at startup.
- Background task advances a simulated-day pointer every TICK_SECONDS,
  runs the 3 specialist agents concurrently, and pushes each finding
  over WebSocket the moment it is produced.
- Finance Manager synthesis is pushed when a tick yields warning/critical.
"""
from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from agents import expense_agent, cashflow_agent, ar_agent, manager_synthesize, scenario_simulation
from rag import build_corpus, answer_question

BASE = Path(__file__).parent
DATA = BASE / "data"
STATIC = BASE / "static"

TICK_SECONDS = float(os.getenv("TICK_SECONDS", "3.5"))
START_DAY_IDX = int(os.getenv("START_DAY_IDX", "77"))  # 0-based -> day 78 of 90
SEED_CASH = 2500000

app = FastAPI(title="FinPilot Live")
# Allow a separately-hosted frontend (e.g. Vercel static) to call the API.
# Set FRONTEND_ORIGIN to the exact site URL in production; "*" is demo convenience.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN", "*")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------- Data loading ----------------
def load_data():
    expenses = pd.read_csv(DATA / "expenses.csv", parse_dates=["date"])
    revenue = pd.read_csv(DATA / "revenue.csv", parse_dates=["date"])
    txns = pd.read_csv(DATA / "transactions.csv", parse_dates=["date"])
    invoices = pd.read_csv(DATA / "invoices.csv", parse_dates=["due_date"])
    budgets = pd.read_csv(DATA / "budgets.csv")
    return expenses, revenue, txns, invoices, budgets

expenses_df, revenue_df, txns_df, invoices_df, budgets_df = load_data()
RAG_DOCS, RAG_IDF = build_corpus(expenses_df, revenue_df, txns_df, invoices_df, budgets_df)
ALL_DATES = sorted(set(expenses_df["date"]) | set(revenue_df["date"]))
START_DAY_IDX = min(START_DAY_IDX, len(ALL_DATES) - 1)

# ---------------- Live state ----------------
state = {
    "sim_idx": START_DAY_IDX,
    "sim_date": ALL_DATES[START_DAY_IDX],
    "tick": 0,
    "alerts_active": 0,
}
# Dedup trackers so the feed doesn't re-post the same flag every tick
_last_expense: dict[str, dict] = {}   # category -> {severity, pct}
_last_ar: dict[str, str] = {}         # invoice_id -> severity
_last_cash_severity: str | None = None
_last_cash_numbers: dict = {}
_last_metrics: dict = {}
_recent_notable: list[dict] = []      # findings since last synthesis (for manager)

subscribers: set[WebSocket] = set()
recent_events: list[dict] = []  # ring buffer of findings/syntheses for late joiners


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def broadcast(msg: dict, remember: bool = False):
    """Push one message to every connected client, live, the moment it's produced."""
    if remember:
        recent_events.append(msg)
        del recent_events[:-10]
    text = json.dumps(msg, default=str)
    dead = []
    for ws in list(subscribers):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        subscribers.discard(ws)


def compute_metrics(sim_date: pd.Timestamp) -> dict:
    rev = float(revenue_df[revenue_df["date"] <= sim_date]["amount"].sum())
    exp = float(expenses_df[expenses_df["date"] <= sim_date]["amount"].sum())
    cash = SEED_CASH + rev - exp
    unpaid = invoices_df[invoices_df["status"] != "paid"].copy()
    overdue_total = 0.0
    if not unpaid.empty:
        late = (sim_date - unpaid["due_date"]).dt.days
        overdue_total = float(unpaid[late >= 30]["amount"].sum())
    return {
        "revenue": round(rev), "expenses": round(exp),
        "profit": round(rev - exp), "cash": round(cash),
        "overdue_total": round(overdue_total),
        "sim_date": sim_date.date().isoformat(),
        "sim_day": int((sim_date - ALL_DATES[0]).days) + 1,
        "total_days": len(ALL_DATES),
    }


def _is_new_expense(f: dict) -> bool:
    cat = f["numbers"].get("category", "?")
    prev = _last_expense.get(cat)
    if prev is None or prev["severity"] != f["severity"] or \
            abs(prev["pct"] - f["numbers"].get("pct_change", 0)) >= 10:
        _last_expense[cat] = {"severity": f["severity"],
                              "pct": f["numbers"].get("pct_change", 0)}
        return True
    return False


def _is_new_ar(f: dict) -> bool:
    inv = f["numbers"].get("invoice_id", "?")
    if _last_ar.get(inv) != f["severity"]:
        _last_ar[inv] = f["severity"]
        return True
    return False


async def run_tick():
    """Advance one simulated day, run agents concurrently, push results live."""
    global _last_cash_severity, _last_cash_numbers, _last_metrics
    state["sim_idx"] += 1
    if state["sim_idx"] >= len(ALL_DATES):
        state["sim_idx"] = 0  # loop the demo window
        _last_expense.clear(); _last_ar.clear(); _last_cash_severity = None
        state["alerts_active"] = 0  # fresh cycle, fresh alert count
        _recent_notable.clear()
        recent_events.clear()
    state["sim_date"] = ALL_DATES[state["sim_idx"]]
    state["tick"] += 1
    sim_date = state["sim_date"]

    # Run specialists concurrently (sync pandas work offloaded to threads)
    expense_coro = asyncio.to_thread(expense_agent, expenses_df, sim_date,
                                     txns_df, budgets_df)
    cash_coro = asyncio.to_thread(cashflow_agent, revenue_df, expenses_df, sim_date)
    ar_coro = asyncio.to_thread(ar_agent, invoices_df, sim_date)
    expense_findings, cash_findings, ar_findings = await asyncio.gather(
        expense_coro, cash_coro, ar_coro
    )

    fresh: list[dict] = []
    # Push each finding the moment it is ready — Expense, then Cash, then AR
    for f in expense_findings:
        if _is_new_expense(f):
            fresh.append(f)
            await broadcast({"type": "finding", "ts": now_iso(),
                             "sim_date": sim_date.date().isoformat(), **f},
                            remember=True)
    for f in cash_findings:
        _last_cash_numbers = f["numbers"]
        if _last_cash_severity != f["severity"]:
            _last_cash_severity = f["severity"]
            if f["severity"] in ("warning", "critical"):
                fresh.append(f)
                await broadcast({"type": "finding", "ts": now_iso(),
                                 "sim_date": sim_date.date().isoformat(), **f},
                                remember=True)
            # info-level cash is routine: keep it out of the alert feed
    for f in ar_findings:
        if _is_new_ar(f):
            fresh.append(f)
            await broadcast({"type": "finding", "ts": now_iso(),
                             "sim_date": sim_date.date().isoformat(), **f},
                            remember=True)

    _recent_notable.extend([f for f in fresh if f.get("severity") in ("warning", "critical")])
    del _recent_notable[:-20]  # rolling window only; manager reads the last ~6
    state["alerts_active"] += sum(1 for f in fresh if f.get("severity") in ("warning", "critical"))

    # Metrics every tick so the top bar visibly ticks
    metrics = compute_metrics(sim_date)
    metrics["active_alerts"] = state["alerts_active"]
    metrics["tick"] = state["tick"]
    _last_metrics = metrics
    await broadcast({"type": "metrics", "ts": now_iso(), **metrics})
    await broadcast({"type": "tick", "ts": now_iso(), "tick": state["tick"],
                     "sim_date": sim_date.date().isoformat(),
                     "fresh_findings": len(fresh)})

    # Finance Manager synthesis when this tick produced anything notable.
    # The manager sees a rolling window (not just this tick) so it can
    # connect findings causally (e.g. profit down because of X and Y).
    if any(f.get("severity") in ("warning", "critical") for f in fresh):
        window = _recent_notable[-6:]
        synthesis = await asyncio.to_thread(manager_synthesize, list(window), metrics)
        if synthesis:
            await broadcast({"type": "synthesis", "ts": now_iso(),
                             "sim_date": sim_date.date().isoformat(), **synthesis},
                            remember=True)


async def tick_loop():
    await asyncio.sleep(1.0)  # let the server finish starting
    while True:
        try:
            import time
            t0 = time.time()
            await run_tick()
            print(f"tick {state['tick']} done in {time.time()-t0:.2f}s day={state['sim_date'].date()}",
                  flush=True)
        except Exception:
            import traceback
            traceback.print_exc()
        await asyncio.sleep(TICK_SECONDS)


@app.on_event("startup")
async def on_startup():
    # Seed dedup baseline silently for INFO-level noise only, so the feed
    # isn't spammed with pre-existing state at boot — but warnings/criticals
    # (e.g. the overdue whale invoice) are left unmarked so they surface
    # live within the first ticks of the demo.
    sim_date = state["sim_date"]
    for f in expense_agent(expenses_df, sim_date, txns_df, budgets_df):
        if f.get("severity") == "info":
            _last_expense[f["numbers"].get("category", "?")] = {
                "severity": "info", "pct": f["numbers"].get("pct_change", 0)}
    _last_metrics.update(compute_metrics(sim_date))
    asyncio.create_task(tick_loop())


# ---------------- HTTP + WS ----------------
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def api_state():
    m = dict(_last_metrics) or compute_metrics(state["sim_date"])
    m.update({"tick": state["tick"], "active_alerts": state["alerts_active"],
              "tick_seconds": TICK_SECONDS})
    return m


@app.post("/api/what-should-i-do")
async def what_should_i_do():
    sim_date = state["sim_date"]
    exp = expense_agent(expenses_df, sim_date, txns_df, budgets_df)
    cash = cashflow_agent(revenue_df, expenses_df, sim_date)
    ar = ar_agent(invoices_df, sim_date)
    metrics = compute_metrics(sim_date)
    result = scenario_simulation(metrics, ar, exp, cash[0]["numbers"] if cash else {})
    result.update({"ts": now_iso(), "sim_date": sim_date.date().isoformat(),
                   "type": "recommendations"})
    await broadcast(result)  # everyone watching sees it live too
    return result


@app.post("/api/chat")
async def chat(body: dict):
    """RAG chatbot: grounded answers over the finance CSVs (1:1, no broadcast)."""
    q = str(body.get("question", "")).strip()[:500]
    if not q:
        return {"type": "chat", "ts": now_iso(), "answer":
                "Ask me anything about company finances — e.g. 'Why is profit down?' or 'Who owes us the most?'",
                "sources": []}
    result = await asyncio.to_thread(answer_question, q, state["sim_date"], RAG_DOCS, RAG_IDF)
    result.update({"type": "chat", "ts": now_iso()})
    return result


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    subscribers.add(ws)
    try:
        # greeting snapshot so a fresh client instantly has numbers
        m = dict(_last_metrics) or compute_metrics(state["sim_date"])
        m.update({"active_alerts": state["alerts_active"]})
        await ws.send_text(json.dumps(
            {"type": "hello", "ts": now_iso(), "metrics": m,
             "tick": state["tick"], "tick_seconds": TICK_SECONDS}))
        for evt in recent_events:  # replay so late joiners see current context
            await ws.send_text(json.dumps(evt, default=str))
        while True:
            await ws.receive_text()  # ignore inbound; server pushes only
    except WebSocketDisconnect:
        pass
    finally:
        subscribers.discard(ws)


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
