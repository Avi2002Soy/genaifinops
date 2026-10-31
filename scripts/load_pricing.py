"""Validate database/pricing_catalog.json and upsert it into dbo.ModelPricing.

    export LOG_ANALYTICS_WORKSPACE_ID=bbdc79ef-c388-417b-bdef-d3cf4e4665d2
    export SQL_CONNECTION_STRING="Driver={ODBC Driver 18 for SQL Server};Server=tcp:finops-sql-ci01.database.windows.net,1433;Database=finops-db;Uid=finopsadmin;Pwd=Mi7uGRnrxXWuFSth;Encrypt=yes;TrustServerCertificate=no;"
    python scripts/load_pricing.py --dry-run     # validate only
    python scripts/load_pricing.py               # validate + upsert

(The values above are DUMMY examples that only show the format; use your own via environment variables.)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))

from finops.catalog import CatalogError, parse_catalog  # noqa: E402
from finops.config import Settings  # noqa: E402
from finops.db import Database  # noqa: E402

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "database" / "pricing_catalog.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--dry-run", action="store_true", help="validate the file and print a summary, touch nothing")
    args = ap.parse_args()

    try:
        entries = parse_catalog(json.loads(args.catalog.read_text(encoding="utf-8")))
    except (CatalogError, ValueError) as exc:
        print(f"Catalog invalid: {exc}", file=sys.stderr)
        return 1

    providers = sorted({e.provider for e in entries})
    print(f"Catalog OK: {len(entries)} price rows, providers: {', '.join(providers)}")
    if args.dry_run:
        return 0
    written = Database(Settings.from_env()).run(lambda repo: repo.upsert_prices(entries))
    print(f"Upserted {written} rows into dbo.ModelPricing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
