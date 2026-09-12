"""RAG chatbot over the company finance CSVs. Zero new dependencies.

- Corpus: short text docs built deterministically from expenses, revenue,
  transactions, invoices, budgets (+ a baked-in anomaly brief).
- Retrieval: pure-Python TF-IDF-ish keyword scoring (no vector store).
- Answering: Claude narrates USING ONLY retrieved records (numbers must come
  from context); offline fallback returns an extractive summary of the same
  records. Never accusatory language — flags are for human review.
"""
from __future__ import annotations
import math
import re
from collections import Counter

import pandas as pd
from llm import narrate

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set(
    "the a an and or of to in on for with is are was were be been by as at from "
    "that this it its rs inr what which who whom whose why how much many does do "
    "show tell give me my our company s".split()
)

# Query expansion: map everyday phrasing onto the books' vocabulary.
_SYNONYMS = {
    "owe": ["unpaid", "overdue", "invoice", "customer", "due"],
    "owes": ["unpaid", "overdue", "invoice", "customer", "due"],
    "owing": ["unpaid", "overdue", "invoice", "customer", "due"],
    "owed": ["unpaid", "overdue", "invoice", "customer", "due"],
    "debt": ["unpaid", "invoice", "overdue"],
    "debtors": ["unpaid", "invoice", "customer"],
    "late": ["overdue", "due", "unpaid"],
    "overdue": ["overdue", "due", "unpaid"],
    "customer": ["customer", "invoice"],
    "customers": ["customer", "invoice"],
    "invoice": ["invoice", "customer"],
    "invoices": ["invoice", "customer"],
    "profit": ["profit"],
    "loss": ["profit"],
    "losing": ["profit"],
    "spend": ["spend", "expenses"],
    "spending": ["spend", "expenses"],
    "spent": ["spend", "expenses"],
    "cost": ["spend", "expenses"],
    "costs": ["spend", "expenses"],
    "revenue": ["revenue"],
    "sales": ["revenue"],
    "earning": ["revenue"],
    "earnings": ["revenue"],
    "cash": ["cash"],
    "runway": ["cash"],
    "balance": ["cash"],
}

# Full-window frames kept for the live cash line (same objects app.py holds).
_REV_DF = None
_EXP_DF = None
_SEED_CASH = 2500000


def _toks(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(str(text).lower()) if t not in _STOP]


def _inr(n) -> str:
    try:
        return f"Rs {float(n):,.0f}"
    except (TypeError, ValueError):
        return str(n)


def build_corpus(expenses: pd.DataFrame, revenue: pd.DataFrame,
                 transactions: pd.DataFrame, invoices: pd.DataFrame,
                 budgets: pd.DataFrame):
    """Build (docs, idf). Each doc: {id, title, text}. Fully deterministic."""
    global _REV_DF, _EXP_DF
    _REV_DF, _EXP_DF = revenue, expenses
    docs: list[dict] = []

    exp_total = expenses.groupby("category")["amount"].sum().sort_values(ascending=False)
    bmap = dict(zip(budgets["category"], budgets["monthly_budget"])) if budgets is not None else {}
    for cat, total in exp_total.items():
        top_v = (expenses[expenses["category"] == cat].groupby("vendor")["amount"]
                 .sum().sort_values(ascending=False).head(3))
        tops = "; ".join(f"{v} {_inr(a)}" for v, a in top_v.items())
        bud = bmap.get(cat)
        docs.append({
            "id": f"spend:{cat}", "title": f"Spend — {cat}",
            "text": (f"{cat} expenses total {_inr(total)} over the 90-day window "
                     f"(~{_inr(total / 3)} per month). Monthly budget "
                     f"{_inr(bud) if bud else 'not set'}. Top vendors: {tops}."),
        })

    rev_total = revenue.groupby("source")["amount"].sum().sort_values(ascending=False)
    for src, total in rev_total.items():
        docs.append({
            "id": f"rev:{src}", "title": f"Revenue — {src}",
            "text": f"Revenue from {src} totals {_inr(total)} over 90 days (~{_inr(total / 3)} per month).",
        })
    docs.append({
        "id": "rev:total", "title": "Revenue — total",
        "text": (f"Total revenue {_inr(revenue['amount'].sum())} over 90 days. "
                 f"Total expenses {_inr(expenses['amount'].sum())}. "
                 f"Net profit {_inr(revenue['amount'].sum() - expenses['amount'].sum())}."),
    })

    # Monthly P&L
    tmp_e = expenses.copy(); tmp_e["month"] = tmp_e["date"].dt.strftime("%Y-%m")
    tmp_r = revenue.copy(); tmp_r["month"] = tmp_r["date"].dt.strftime("%Y-%m")
    for m in sorted(set(tmp_e["month"]) | set(tmp_r["month"])):
        e = float(tmp_e[tmp_e["month"] == m]["amount"].sum())
        r = float(tmp_r[tmp_r["month"] == m]["amount"].sum())
        docs.append({
            "id": f"pnl:{m}", "title": f"P&L — {m}",
            "text": f"In {m}: revenue {_inr(r)}, expenses {_inr(e)}, profit {_inr(r - e)}.",
        })

    # One doc per invoice (days-late rendered at query time)
    for _, row in invoices.iterrows():
        docs.append({
            "id": f"inv:{row['invoice_id']}", "title": f"Invoice {row['invoice_id']}",
            "text": (f"Invoice {row['invoice_id']}: customer {row['customer']}, "
                     f"amount {_inr(row['amount'])}, due {row['due_date'].date()}, "
                     f"status {row['status']}."),
            "due": row["due_date"], "status": row["status"],
            "amount": float(row["amount"]), "customer": str(row["customer"]),
        })

    # Notable events brief (deterministic, from the data story)
    cloud = expenses[expenses["category"] == "Cloud Infra"].sort_values("date")
    if len(cloud) >= 20:
        base = float(cloud.iloc[:-10]["amount"].mean())
        spike = float(cloud.iloc[-10:]["amount"].mean())
        docs.append({
            "id": "event:cloud-spike", "title": "Event — Cloud Infra spike",
            "text": (f"Cloud Infra (AWS India) spend averaged {_inr(base)} per day, then rose to "
                     f"{_inr(spike)} per day over the most recent 10 days "
                     f"(about {(spike / base - 1) * 100:.0f}% higher) — flagged for review with engineering, "
                     f"not confirmed as an issue."),
        })
    unpaid = invoices[invoices["status"] != "paid"].sort_values("amount", ascending=False)
    if not unpaid.empty:
        top = unpaid.iloc[0]
        docs.append({
            "id": "event:overdue", "title": "Event — largest overdue invoice",
            "text": (f"Largest unpaid invoice: {top['customer']} {top['invoice_id']} "
                     f"{_inr(top['amount'])} due {top['due_date'].date()}, status unpaid — "
                     f"flagged for review with a polite payment follow-up."),
        })

    # IDF over the corpus
    df = Counter()
    doc_toks = []
    for d in docs:
        ts = set(_toks(d["title"] + " " + d["text"]))
        doc_toks.append(ts)
        for t in ts:
            df[t] += 1
    n = len(docs)
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    for d, ts in zip(docs, doc_toks):
        d["_toks"] = ts
    return docs, idf


def retrieve(question: str, docs: list[dict], idf: dict, top_k: int = 5) -> list[dict]:
    """Score docs by IDF-weighted keyword overlap (with synonym expansion). Deterministic."""
    qt = [t for t in _toks(question) if t in idf]
    for t in _toks(question):
        qt.extend(s for s in _SYNONYMS.get(t, []) if s in idf)
    if not qt:
        return [d for d in docs if d["id"] in ("rev:total", "event:cloud-spike",
                                               "event:overdue")][:top_k]
    scored = sorted(
        ((sum(idf[t] for t in set(qt) & d["_toks"]), d["id"], d) for d in docs),
        key=lambda x: (-x[0], x[1]),
    )
    return [d for s, _, d in scored[:top_k] if s > 0] or [d for _, _, d in scored[:top_k]]


def _render_context(hits: list[dict], sim_date) -> str:
    lines = []
    for d in hits:
        txt = d["text"]
        if d["id"].startswith("inv:") and d.get("status") != "paid" and sim_date is not None:
            late = (pd.Timestamp(sim_date) - pd.Timestamp(d["due"])).days
            if late >= 0:
                txt += f" As of {pd.Timestamp(sim_date).date()} it is {late} days overdue."
        lines.append(f"[{d['title']}] {txt}")
    return "\n".join(lines)


def answer_question(question: str, sim_date, docs: list[dict], idf: dict) -> dict:
    """Grounded answer + source titles. Numbers always trace to the CSVs."""
    hits = retrieve(question, docs, idf)
    # Live cash line for cash/runway/balance questions (depends on sim day).
    qt = set(_toks(question))
    if qt & {"cash", "runway", "balance", "healthy", "health"} and \
            _REV_DF is not None and sim_date is not None:
        rev = float(_REV_DF[_REV_DF["date"] <= sim_date]["amount"].sum())
        exp = float(_EXP_DF[_EXP_DF["date"] <= sim_date]["amount"].sum())
        cash = _SEED_CASH + rev - exp
        hits = [{
            "id": "cash:live", "title": "Cash — current position",
            "text": (f"Current cash {_inr(cash)} as of {pd.Timestamp(sim_date).date()} "
                     f"(Rs 25,00,000 seed plus {_inr(rev)} revenue minus {_inr(exp)} expenses to date). "
                     f"30-day runway target Rs 20,00,000."),
        }] + hits
        hits = hits[:5]
    context = _render_context(hits, sim_date)
    fallback = ("Based on company records: " + " ".join(
        h["text"] for h in hits[:3]))[:900]
    prompt = (f"Question: {question}\n\nCompany finance records (as of {sim_date.date() if sim_date is not None else 'latest'}):\n"
              f"{context}\n\nAnswer in 2-4 sentences using ONLY these records.")
    answer = narrate("chat", prompt, fallback, max_tokens=250)
    return {"answer": answer, "sources": [h["title"] for h in hits]}
