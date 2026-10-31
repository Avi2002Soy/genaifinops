"""Send synthetic LLM telemetry to the real-time ingest endpoint (POST /api/ingest).

Local (func start, no key needed):
    python scripts/send_test_traffic.py --events 500

Deployed (function key from the portal: Function App -> App keys -> default):
    export FUNCTION_URL="https://finops-func-ci01.azurewebsites.net/api/ingest"
    export FUNCTION_KEY="dXChpo4hgM+TlQ3ea7EmD0AI6w616ssZALqpEEjaMl8947jQh1urxw=="
    python scripts/send_test_traffic.py --events 2000 --bad-rate 0.02 --duplicates 5

The key above is a DUMMY that only shows the format. Replace it with your own (never commit real keys).
Departments are prefixed `demo-` so the data can be removed with `seed_sample_data.py --purge`.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

DUMMY_LOCAL_KEY = "dXChpo4hgM+TlQ3ea7EmD0AI6w616ssZALqpEEjaMl8947jQh1urxw=="
MODELS = [("gpt-4o", 700, 300, 1400), ("gpt-4o-mini", 500, 200, 600), ("gpt-4", 400, 150, 2300), ("claude-3-5-sonnet", 900, 350, 1700)]
DEPARTMENTS = ["demo-support", "demo-analytics", "demo-marketing"]


def make_event(rnd: random.Random, now: datetime, bad: bool) -> dict:
    model, p, c, lat = rnd.choice(MODELS)
    ev = {
        "RequestId": str(uuid.UUID(int=rnd.getrandbits(128), version=4)),
        "Timestamp": (now - timedelta(seconds=rnd.randint(0, 3000))).isoformat(),
        "DepartmentId": rnd.choice(DEPARTMENTS),
        "ModelName": model,
        "PromptTokens": max(1, int(rnd.gauss(p, p * 0.3))),
        "CompletionTokens": max(1, int(rnd.gauss(c, c * 0.3))),
        "LatencyMs": max(50, int(rnd.gauss(lat, lat * 0.25))),
    }
    if bad:
        ev["PromptTokens"] = -1  # what the APIM policy emits when the response had no usage block
    return ev


def post(url: str, key: str, events: list) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps({"events": events}).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-functions-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "body": exc.read().decode()[:300]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("FUNCTION_URL", "http://localhost:7071/api/ingest"))
    ap.add_argument("--key", default=os.environ.get("FUNCTION_KEY", DUMMY_LOCAL_KEY))
    ap.add_argument("--events", type=int, default=500)
    ap.add_argument("--batch-size", type=int, default=250)
    ap.add_argument("--bad-rate", type=float, default=0.0, help="fraction of events with missing usage (-1)")
    ap.add_argument("--duplicates", type=int, default=0, help="re-send this many events to exercise de-duplication")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rnd, now = random.Random(args.seed), datetime.now(timezone.utc)
    events = [make_event(rnd, now, rnd.random() < args.bad_rate) for _ in range(args.events)]
    events += events[: args.duplicates]
    for i in range(0, len(events), args.batch_size):
        print(post(args.url, args.key, events[i : i + args.batch_size]))


if __name__ == "__main__":
    main()
