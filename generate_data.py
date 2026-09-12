"""Generate ~90 days of realistic synthetic data for a small SaaS company (~Rs 1.2Cr annual).
Bakes in:
  1. AWS/cloud expense spiking 80%+ in the most recent 10 days.
  2. One deliberately overdue invoice (ABC Corp, Rs 8L, 42+ days late at end of window).
Run: python generate_data.py
"""
import csv
import random
from datetime import date, timedelta

random.seed(42)

NUM_DAYS = 90
END_DATE = date(2026, 9, 1)  # end of simulated window
START_DATE = END_DATE - timedelta(days=NUM_DAYS - 1)  # 2026-06-04 approx

DATA_DIR = "data"

import os
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Revenue ----------
# Target ~Rs 1.2Cr annual => ~Rs 10L/month => ~Rs 33k/day avg => ~Rs 30L over 90 days
sources = [
    ("Acme Subscriptions (MRR)", 26000, 0.12),   # daily-ish base
    ("Enterprise Licenses", 9000, 0.5),           # lumpy
    ("Professional Services", 4000, 0.6),
]
revenue_rows = [("date", "source", "amount")]
for i in range(NUM_DAYS):
    d = START_DATE + timedelta(days=i)
    # slight growth trend + weekly seasonality (weekday higher)
    growth = 1 + (i / NUM_DAYS) * 0.12
    weekday_boost = 1.0 if d.weekday() < 5 else 0.35
    for src, base, jitter in sources:
        # enterprise/services fire only some days
        if "Subscription" in src or random.random() > 0.45:
            amt = base * growth * weekday_boost * (1 + random.uniform(-jitter, jitter))
            amt = max(500, round(amt))
            revenue_rows.append((d.isoformat(), src, amt))

# ---------- Expenses ----------
# Categories with monthly budgets; cloud has the deliberate spike
categories = {
    "Cloud Infra": {"vendor": "AWS India", "daily_base": 5200, "jitter": 0.15, "monthly_budget": 170000},
    "Payroll": {"vendor": "Payroll - Team", "daily_base": 22000, "jitter": 0.05, "monthly_budget": 680000},
    "Marketing": {"vendor": "Google Ads", "daily_base": 4500, "jitter": 0.4, "monthly_budget": 140000},
    "Software Tools": {"vendor": "SaaS Tools", "daily_base": 2800, "jitter": 0.25, "monthly_budget": 90000},
    "Office": {"vendor": "WeWork / Office", "daily_base": 1800, "jitter": 0.3, "monthly_budget": 60000},
    "Travel": {"vendor": "MakeMyTrip", "daily_base": 1200, "jitter": 0.7, "monthly_budget": 45000},
}
expense_rows = [("date", "category", "vendor", "amount")]
vendors_extra = {
    "Cloud Infra": ["AWS India", "AWS India (EC2)", "AWS India (RDS)"],
    "Marketing": ["Google Ads", "LinkedIn Ads", "Meta Ads"],
    "Software Tools": ["GitHub", "Slack", "Notion", "Datadog"],
    "Office": ["WeWork / Office", "Swiggy Office", "Amazon Office Supplies"],
    "Travel": ["MakeMyTrip", "IndiGo", "Uber for Business"],
    "Payroll": ["Payroll - Team"],
}
for i in range(NUM_DAYS):
    d = START_DATE + timedelta(days=i)
    is_spike_window = i >= NUM_DAYS - 10  # most recent 10 days
    for cat, spec in categories.items():
        base = spec["daily_base"]
        if cat == "Cloud Infra" and is_spike_window:
            # 80%+ spike: ~2x baseline with upward drift (unoptimized EC2 scale-up story)
            base = base * 2.0 * (1 + (i - (NUM_DAYS - 10)) * 0.03)
        # payroll posts as one aggregated daily accrual (skip weekends for realism except accrual smoothing -> keep daily)
        if cat == "Payroll" and d.weekday() >= 5:
            continue  # no payroll entries on weekends
        if cat != "Payroll" and random.random() < 0.12:
            continue  # some days no spend in small categories
        amt = base * (1 + random.uniform(-spec["jitter"], spec["jitter"]))
        vendor = random.choice(vendors_extra[cat])
        # payroll vendor fixed
        expense_rows.append((d.isoformat(), cat, vendor, round(max(200, amt))))

# (No one-off capex entries: the demo story stays crisp — the Cloud Infra
# spike is the single expense anomaly the agents must discover.)

# ---------- Transactions (bank mirror: revenue in, expenses out) ----------
txn_rows = [("date", "description", "category", "amount")]
for r in revenue_rows[1:]:
    d, src, amt = r
    txn_rows.append((d, f"IN - {src}", "Revenue", amt))
for r in expense_rows[1:]:
    d, cat, vendor, amt = r
    txn_rows.append((d, f"OUT - {vendor} [{cat}]", cat, -amt))
txn_rows[1:] = sorted(txn_rows[1:], key=lambda x: x[0])

# ---------- Invoices ----------
# Mix of paid + unpaid; one deliberate whale overdue 42+ days: ABC Corp Rs 8,00,000
invoice_rows = [("customer", "invoice_id", "amount", "due_date", "status")]
customers = ["Globex Ltd", "Initech", "Umbrella Retail", "Hooli Systems", "Stark Traders",
             "Wayne Logistics", "Massive Dynamic", "Soylent Foods", "Acme Subscriptions"]
inv_id = 1001
# paid invoices spread across window
for i in range(0, NUM_DAYS - 15, 7):
    issue = START_DATE + timedelta(days=i)
    due = issue + timedelta(days=15)
    cust = random.choice(customers)
    amt = random.choice([95000, 120000, 145000, 180000, 220000, 310000])
    status = "paid" if due < END_DATE - timedelta(days=5) else random.choice(["paid", "unpaid"])
    # keep early ones paid so story is clean
    if i < NUM_DAYS - 30:
        status = "paid"
    invoice_rows.append((cust, f"INV-{inv_id}", amt, due.isoformat(), status))
    inv_id += 1

# a few recent unpaid (not yet overdue / mildly overdue)
invoice_rows.append(("Hooli Systems", f"INV-{inv_id}", 240000,
                      (END_DATE - timedelta(days=12)).isoformat(), "unpaid")); inv_id += 1
invoice_rows.append(("Stark Traders", f"INV-{inv_id}", 175000,
                      (END_DATE - timedelta(days=20)).isoformat(), "unpaid")); inv_id += 1
invoice_rows.append(("Wayne Logistics", f"INV-{inv_id}", 320000,
                      (END_DATE - timedelta(days=33)).isoformat(), "unpaid")); inv_id += 1

# THE deliberate whale: 42+ days late at END_DATE, Rs 8L
whale_due = END_DATE - timedelta(days=47)
invoice_rows.append(("ABC Corp", f"INV-{inv_id}", 800000, whale_due.isoformat(), "unpaid")); inv_id += 1

# ---------- Budgets ----------
budget_rows = [("category", "monthly_budget")]
for cat, spec in categories.items():
    budget_rows.append((cat, spec["monthly_budget"]))

def write(path, rows):
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    print(f"wrote {path} ({len(rows)-1} rows)")

write(f"{DATA_DIR}/revenue.csv", revenue_rows)
write(f"{DATA_DIR}/expenses.csv", expense_rows)
write(f"{DATA_DIR}/transactions.csv", txn_rows)
write(f"{DATA_DIR}/invoices.csv", invoice_rows)
write(f"{DATA_DIR}/budgets.csv", budget_rows)

# ---------- Verification print ----------
print(f"\nWindow: {START_DATE} .. {END_DATE} ({NUM_DAYS} days)")
tot_rev = sum(r[2] for r in revenue_rows[1:])
tot_exp = sum(r[3] for r in expense_rows[1:])
print(f"Total revenue 90d: Rs {tot_rev:,.0f}  (~Rs {tot_rev/3:,.0f}/mo, annualised Rs {tot_rev/90*365:,.0f})")
print(f"Total expenses 90d: Rs {tot_exp:,.0f}")
# cloud baseline vs spike check
import statistics
cloud = [(r[0], r[3]) for r in expense_rows[1:] if r[1] == "Cloud Infra"]
cloud_sorted = sorted(cloud)
base_avg = statistics.mean(a for _, a in cloud_sorted[:60])
spike_avg = statistics.mean(a for _, a in cloud_sorted[-10:])
print(f"Cloud daily avg first 60d: Rs {base_avg:,.0f} | last 10d: Rs {spike_avg:,.0f} | uplift {(spike_avg/base_avg-1)*100:.0f}%")
print(f"Whale invoice: ABC Corp Rs 8,00,000 due {whale_due} ({(END_DATE-whale_due).days}d late at end)")
