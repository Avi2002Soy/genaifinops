"""Seed synthetic usage so you can build the Power BI report and test alerts WITHOUT sending
real traffic through APIM (which would spend your free API-call and Log Analytics quota).

    export SQL_CONNECTION_STRING="Driver={ODBC Driver 18 for SQL Server};Server=tcp:finops-sql-ci01.database.windows.net,1433;Database=finops-db;Uid=finopsadmin;Pwd=Mi7uGRnrxXWuFSth;Encrypt=yes;TrustServerCertificate=no;"
    export LOG_ANALYTICS_WORKSPACE_ID=bbdc79ef-c388-417b-bdef-d3cf4e4665d2
    python scripts/seed_sample_data.py --days 30 --spike          # insert
    python scripts/seed_sample_data.py --days 40 --routing-change # also simulate an ACCEPTED routing recommendation
    python scripts/seed_sample_data.py --purge                    # remove everything it created

(The connection string above is a format example. For real runs, put your own in the environment.)
`--routing-change` moves 85 % of demo-support's gpt-4 traffic to gpt-3.5-turbo 20 days ago and marks the
recommendation accepted, so the impact review and the Power BI "Savings & impact" page have something to
show. The resulting savings/latency numbers are SYNTHETIC: they demonstrate the mechanism, not a result.

All demo departments are prefixed `demo-`, so purging never touches real data.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))

from finops.config import Settings  # noqa: E402
from finops.db import Database  # noqa: E402
from finops.models import RoutingRecommendation, UsageRow  # noqa: E402
from finops.pricing import consolidate, price_usage  # noqa: E402

PREFIX = "demo-"
# (department, model, requests/day, avg prompt, avg completion, avg latency ms, monthly budget USD)
PROFILES = [
    ("support",   "gpt-4",         900, 350, 120, 2300, 400),   # short requests on an expensive model -> routing candidate
    ("support",   "gpt-3.5-turbo", 2500, 300, 90,  650, 400),
    ("analytics", "gpt-4",         220, 2800, 900, 4100, 600),  # long prompts -> correctly NOT recommended
    ("marketing", "gpt-4o",        600, 700, 400, 1500, 150),
    ("marketing", "gpt-4o-mini",   1500, 500, 250, 700, 150),
]


REROUTE_AFTER_DAYS_AGO = 20
REROUTE_SHARE = 0.85


def synth_rows(days: int, seed: int, spike: bool, today: date, reroute: bool = False):
    rnd = random.Random(seed)
    rows = []
    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        weekday_factor = 0.45 if day.weekday() >= 5 else 1.0
        moved = 0  # gpt-4 requests that were re-routed to gpt-3.5-turbo today
        for dept, model, reqs, p, c, lat, _ in PROFILES:
            n = max(1, int(reqs * weekday_factor * rnd.uniform(0.85, 1.15)))
            if reroute and dept == "support" and model == "gpt-4" and offset < REROUTE_AFTER_DAYS_AGO:
                moved = int(n * REROUTE_SHARE)
                n -= moved
            elif reroute and dept == "support" and model == "gpt-3.5-turbo":
                n += moved
            if spike and dept == "support" and model == "gpt-4" and offset == 1:
                n *= 6  # yesterday: runaway job
            bad = int(n * 0.0004 * rnd.random() * 5)
            good = n - bad
            avg_total = p + c
            rows.append(UsageRow(
                usage_date=day, department_id=PREFIX + dept, model_name=model,
                prompt_tokens=int(good * p * rnd.uniform(0.95, 1.05)),
                completion_tokens=int(good * c * rnd.uniform(0.95, 1.05)),
                request_count=good, malformed_count=bad,
                p95_total_tokens=int(avg_total * rnd.uniform(1.6, 2.2)),
                avg_latency_ms=int(lat * rnd.uniform(0.9, 1.1)),
                p95_latency_ms=int(lat * rnd.uniform(1.8, 2.4)),
            ))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--spike", action="store_true", help="inject a cost spike yesterday to exercise anomaly alerts")
    ap.add_argument("--routing-change", action="store_true",
                    help="simulate an accepted gpt-4 -> gpt-3.5-turbo routing change for demo-support (needs --days > 20)")
    ap.add_argument("--purge", action="store_true", help="delete all demo- data and exit")
    args = ap.parse_args()

    db = Database(Settings.from_env())
    if args.purge:
        db.run(lambda repo: repo.purge_prefix(PREFIX))
        print("Purged demo data.")
        return

    if args.routing_change and args.days <= REROUTE_AFTER_DAYS_AGO + 7:
        ap.error(f"--routing-change needs --days > {REROUTE_AFTER_DAYS_AGO + 7} (history before and after the switch)")
    today = date.today()
    rows = consolidate(synth_rows(args.days, args.seed, args.spike, today, args.routing_change))

    def work(repo):
        priced = price_usage(rows, repo.load_price_book())
        repo.upsert_usage(priced)
        for dept, *_rest, budget in PROFILES:
            repo.upsert_budget(PREFIX + dept, Decimal(budget))
        if args.routing_change:
            dept = PREFIX + "support"
            repo.save_recommendations([RoutingRecommendation(
                dept, "gpt-4", "gpt-3.5-turbo", 25_000, 350, 120, 900, Decimal("900.00"),
                Decimal("850.00"), 95.0, "high", -72.0)], datetime(2000, 1, 1))
            repo.set_recommendation_status(
                dept, "gpt-4", "gpt-3.5-turbo", "accepted", today - timedelta(days=REROUTE_AFTER_DAYS_AGO))
        return len(priced), sum(1 for p in priced if not p.priced)

    written, unpriced = db.run(work)
    print(f"Upserted {written} rows ({unpriced} unpriced). Run the daily function or POST /api/backfill to compute alerts.")
    if args.routing_change:
        print("Accepted a synthetic routing change 20 days ago: the next weekly review (or run_impact_review) will measure it.")


if __name__ == "__main__":
    main()
