"""Deterministic specialist-agent detection logic + Finance Manager synthesis.

Thresholds are pure pandas/Python — the LLM only narrates pre-computed numbers.
All money figures are in INR (Rs).
"""
from __future__ import annotations
import pandas as pd
from llm import narrate

# ---- Tunable deterministic thresholds ----
EXPENSE_PCT_THRESHOLD = 0.30   # flag if |change| > 30% vs trailing avg
EXPENSE_WINDOW_RECENT = 7       # last 7 days
EXPENSE_WINDOW_BASELINE = 21    # prior 21 days
LARGE_TXN_THRESHOLD = 100000    # single expense above this => "flagged for review"
MIN_BASELINE_7D = 12000         # ignore % swings on tiny-spend categories (noise guard)
OVERDUE_THRESHOLD_DAYS = 30
SEED_CASH = 2500000             # Rs 25L starting cash
CASH_RUNWAY_TARGET_30D = 2000000  # warn if projected 30d cash below Rs 20L
RUNWAY_WINDOW_DAYS = 14


def _inr(n: float) -> str:
    return f"Rs {n:,.0f}"


# ================= Expense Agent =================
def expense_agent(expenses: pd.DataFrame, sim_date: pd.Timestamp,
                  transactions: pd.DataFrame | None = None,
                  budgets: pd.DataFrame | None = None) -> list[dict]:
    """Compare last-7d spend per category vs prior-21d daily average scaled to 7d.

    Reads expenses.csv (+ transactions.csv ledger for the large-single scan,
    budgets.csv for budget context in the numbers payload).
    """
    findings: list[dict] = []
    if expenses.empty:
        return findings
    df = expenses[expenses["date"] <= sim_date]
    if df.empty:
        return findings

    recent_start = sim_date - pd.Timedelta(days=EXPENSE_WINDOW_RECENT - 1)
    base_end = recent_start - pd.Timedelta(days=1)
    base_start = base_end - pd.Timedelta(days=EXPENSE_WINDOW_BASELINE - 1)
    recent = df[(df["date"] >= recent_start) & (df["date"] <= sim_date)]
    baseline = df[(df["date"] >= base_start) & (df["date"] <= base_end)]
    if baseline.empty or recent.empty:
        return findings

    base_per_cat = baseline.groupby("category")["amount"].mean() * EXPENSE_WINDOW_RECENT
    recent_per_cat = recent.groupby("category")["amount"].sum()
    budget_map = {}
    if budgets is not None and not budgets.empty:
        budget_map = dict(zip(budgets["category"], budgets["monthly_budget"]))
    month_start = sim_date.replace(day=1)
    mtd = df[(df["date"] >= month_start) & (df["date"] <= sim_date)] \
        .groupby("category")["amount"].sum().to_dict()

    for cat, recent_total in recent_per_cat.items():
        base_total = float(base_per_cat.get(cat, 0.0))
        if base_total <= 0 or base_total < MIN_BASELINE_7D:
            continue
        pct = (recent_total - base_total) / base_total
        if abs(pct) <= EXPENSE_PCT_THRESHOLD:
            continue
        severity = "info"
        if abs(pct) >= 0.80:
            severity = "critical"
        elif abs(pct) >= 0.50:
            severity = "warning"
        direction = "up" if pct > 0 else "down"
        vendors = (
            recent[recent["category"] == cat]
            .groupby("vendor")["amount"].sum()
            .sort_values(ascending=False).head(2)
        )
        vendor_txt = ", ".join(f"{v} ({_inr(a)})" for v, a in vendors.items())
        numbers = {
            "category": cat,
            "recent_7d": round(float(recent_total)),
            "baseline_7d": round(float(base_total)),
            "pct_change": round(float(pct) * 100, 1),
            "direction": direction,
        }
        if cat in budget_map:
            numbers["monthly_budget"] = int(budget_map[cat])
            numbers["mtd_spend"] = round(float(mtd.get(cat, 0.0)))
        fallback = (
            f"{cat} spend is {direction} {abs(pct)*100:.0f}% vs trailing average "
            f"({_inr(recent_total)} vs {_inr(base_total)} over 7 days; top: {vendor_txt}) — flagged for review."
        )
        payload = (
            f"Category: {cat}. Recent 7-day spend {round(float(recent_total))}, "
            f"baseline 7-day equivalent {round(float(base_total))}, "
            f"change {round(float(pct)*100,1)}% {direction}. Top vendors: {vendor_txt}. "
            f"review_flag=true."
        )
        findings.append({
            "agent": "Expense Agent",
            "finding": narrate("expense", payload, fallback),
            "severity": severity,
            "numbers": numbers,
        })

    # Large / unusual single transactions -> flagged for review (never fraud).
    # Scanned on the transactions.csv bank ledger when provided (OUT rows only),
    # else on the expenses slice directly. Same story either way: the ledger
    # mirrors expenses one-for-one.
    if transactions is not None and not transactions.empty:
        ledger = transactions[(transactions["date"] >= recent_start) &
                              (transactions["date"] <= sim_date)]
        ledger = ledger[(ledger["amount"] < 0) &
                        (~ledger["category"].isin(["Payroll", "Revenue"]))]
        big_rows = [(abs(float(r["amount"])), str(r["description"]),
                     str(r["category"]), r["date"])
                    for _, r in ledger.iterrows()
                    if abs(float(r["amount"])) >= LARGE_TXN_THRESHOLD]
    else:
        recent_big = recent[(recent["amount"] >= LARGE_TXN_THRESHOLD) &
                            (recent["category"] != "Payroll")]
        big_rows = [(float(r["amount"]), str(r["vendor"]), str(r["category"]), r["date"])
                    for _, r in recent_big.iterrows()]
    for amt, vendor, cat, dt in big_rows:
        numbers = {
            "vendor": vendor,
            "category": cat,
            "amount": round(float(amt)),
            "date": str(pd.Timestamp(dt).date()),
        }
        fallback = (
            f"Large single expense of {_inr(amt)} to {vendor} "
            f"({cat}, {pd.Timestamp(dt).date()}) — flagged for review, not confirmed as an issue."
        )
        findings.append({
            "agent": "Expense Agent",
            "finding": narrate(
                "expense",
                f"Single large transaction: {numbers}. review_flag=true. Describe it as flagged for review.",
                fallback,
            ),
            "severity": "warning",
            "numbers": numbers,
            "review_flag": True,
        })
    return findings


# ================= Cash Flow Agent =================
def cashflow_agent(revenue: pd.DataFrame, expenses: pd.DataFrame,
                   sim_date: pd.Timestamp) -> list[dict]:
    rev = revenue[revenue["date"] <= sim_date]["amount"].sum() if not revenue.empty else 0.0
    exp = expenses[expenses["date"] <= sim_date]["amount"].sum() if not expenses.empty else 0.0
    current_cash = SEED_CASH + float(rev) - float(exp)

    # trailing run-rate from last RUNWAY_WINDOW_DAYS of net daily flow
    start = sim_date - pd.Timedelta(days=RUNWAY_WINDOW_DAYS - 1)
    r = revenue[(revenue["date"] >= start) & (revenue["date"] <= sim_date)]["amount"].sum() \
        if not revenue.empty else 0.0
    e = expenses[(expenses["date"] >= start) & (expenses["date"] <= sim_date)]["amount"].sum() \
        if not expenses.empty else 0.0
    daily_net = (float(r) - float(e)) / RUNWAY_WINDOW_DAYS
    proj_30 = current_cash + daily_net * 30
    proj_60 = current_cash + daily_net * 60

    severity = "info"
    if proj_30 < CASH_RUNWAY_TARGET_30D:
        severity = "critical" if proj_30 < CASH_RUNWAY_TARGET_30D * 0.8 else "warning"
    numbers = {
        "current_cash": round(current_cash),
        "projected_30d": round(proj_30),
        "projected_60d": round(proj_60),
        "daily_net_runrate": round(daily_net),
        "runway_target_30d": CASH_RUNWAY_TARGET_30D,
    }
    if severity == "info":
        fallback = (
            f"Cash position {_inr(current_cash)} is healthy; 30-day projection {_inr(proj_30)} "
            f"is above the {_inr(CASH_RUNWAY_TARGET_30D)} target (daily run-rate {_inr(daily_net)})."
        )
    else:
        fallback = (
            f"Projected 30-day cash {_inr(proj_30)} is below the {_inr(CASH_RUNWAY_TARGET_30D)} target "
            f"(current {_inr(current_cash)}, daily run-rate {_inr(daily_net)}) — flagged for review."
        )
    payload = (
        f"Current cash {round(current_cash)}, projected 30d {round(proj_30)}, "
        f"projected 60d {round(proj_60)}, target {CASH_RUNWAY_TARGET_30D}, "
        f"daily net run-rate {round(daily_net)}, status {severity}."
    )
    return [{
        "agent": "Cash Flow Agent",
        "finding": narrate("cashflow", payload, fallback),
        "severity": severity,
        "numbers": numbers,
    }]


# ================= AR Agent =================
def ar_agent(invoices: pd.DataFrame, sim_date: pd.Timestamp) -> list[dict]:
    findings: list[dict] = []
    if invoices.empty:
        return findings
    df = invoices[invoices["status"] != "paid"].copy()
    if df.empty:
        return findings
    df["days_late"] = (sim_date - df["due_date"]).dt.days
    due = df[df["days_late"] >= OVERDUE_THRESHOLD_DAYS].copy()
    if due.empty:
        return findings
    due["score"] = due["amount"] * due["days_late"]
    due = due.sort_values("score", ascending=False)
    for _, row in due.iterrows():
        days = int(row["days_late"])
        amt = float(row["amount"])
        if days >= 45 and amt >= 500000:
            severity = "critical"
        elif days >= 30:
            severity = "warning"
        else:
            severity = "info"
        numbers = {"customer": str(row["customer"]), "amount": round(amt),
                   "days_late": days, "invoice_id": str(row["invoice_id"])}
        fallback = (
            f"Invoice {row['invoice_id']} from {row['customer']} for {_inr(amt)} "
            f"is {days} days overdue — flagged for review; recommend a polite payment follow-up."
        )
        findings.append({
            "agent": "AR Agent",
            "finding": narrate(
                "ar",
                f"Invoice {row['invoice_id']}, customer {row['customer']}, amount {round(amt)}, "
                f"days late {days}. review_flag=true.",
                fallback,
            ),
            "severity": severity,
            "numbers": numbers,
            "review_flag": True,
        })
    return findings


# ================= Finance Manager =================
def manager_synthesize(findings: list[dict], metrics: dict) -> dict | None:
    """Synthesise ONE narrative from warning/critical findings. Deterministic numbers only."""
    notable = [f for f in findings if f.get("severity") in ("warning", "critical")]
    if not notable:
        return None
    bullets = "\n".join(
        f"- [{f['agent']}/{f['severity']}] {f['finding']} NUMBERS={f['numbers']}"
        for f in notable
    )
    ctx = (
        f"Metrics: revenue_to_date={metrics.get('revenue')}, expenses_to_date={metrics.get('expenses')}, "
        f"profit={metrics.get('profit')}, cash={metrics.get('cash')}, overdue_total={metrics.get('overdue_total')}.\n"
        f"Findings:\n{bullets}\nWrite 2-3 sentences connecting them causally where supported."
    )
    # deterministic fallback built from real numbers (used offline too)
    parts = []
    for f in notable:
        parts.append(f"{f['agent']}: {f['finding']}")
    fallback = " ".join(parts[:3])
    synthesis = narrate("manager", ctx, fallback, max_tokens=220)
    return {
        "agent": "Finance Manager",
        "synthesis": synthesis,
        "source_findings": [
            {"agent": f["agent"], "severity": f["severity"], "numbers": f["numbers"]}
            for f in notable
        ],
    }


def scenario_simulation(metrics: dict, ar_findings: list[dict],
                        expense_findings: list[dict], cash_numbers: dict) -> dict:
    """On-demand 'What should I do?' — deterministic before/after cash math."""
    before = float(cash_numbers.get("projected_30d", metrics.get("cash", 0)))
    recommendations: list[dict] = []
    after = before

    # 1. Collect overdue invoices (30d cash-in)
    collectible = sum(
        f["numbers"]["amount"] for f in ar_findings
        if f.get("severity") in ("warning", "critical")
    )
    if collectible > 0:
        # assume 70% collectible within 30 days
        gain = round(collectible * 0.7)
        after += gain
        top = sorted(ar_findings, key=lambda f: f["numbers"]["amount"] *
                     f["numbers"]["days_late"], reverse=True)[:2]
        names = ", ".join(f"{f['numbers']['customer']} ({_inr(f['numbers']['amount'])})" for f in top)
        recommendations.append({
            "action": f"Chase overdue invoices first: {names} — send payment reminders this week.",
            "impact_estimate": f"+{_inr(gain)} cash if 70% collected in 30 days",
        })

    # 2. Roll back cloud spike to baseline
    cloud = next((f for f in expense_findings
                  if f["numbers"].get("category") == "Cloud Infra"
                  and "recent_7d" in f["numbers"]), None)
    if cloud:
        excess_7d = max(0, cloud["numbers"]["recent_7d"] - cloud["numbers"]["baseline_7d"])
        monthly_saving = round(excess_7d / 7 * 30)
        after += monthly_saving
        recommendations.append({
            "action": "Roll back the Cloud Infra spike to baseline (audit last 10 days of AWS usage, "
                      "downsize idle instances) — flagged for review with engineering.",
            "impact_estimate": f"+{_inr(monthly_saving)}/month run-rate saving",
        })

    # 3. Discretionary freeze as generic lever
    disc = round(metrics.get("expenses", 0) * 0.03)
    if disc > 0:
        after += disc
        recommendations.append({
            "action": "Freeze discretionary spend (travel, office extras) for 30 days.",
            "impact_estimate": f"+{_inr(disc)} estimated saving",
        })

    if not recommendations:
        recommendations.append({
            "action": "No urgent levers — position is stable. Keep monitoring weekly.",
            "impact_estimate": "No change needed",
        })

    # Rank by parsed rupee impact (already added in order of size roughly); keep order.
    return {
        "agent": "Finance Manager",
        "recommendations": recommendations,
        "projected_cash_before": round(before),
        "projected_cash_after": round(after),
    }
