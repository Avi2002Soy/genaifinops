# GenAI FinOps & Token Analytics (Azure free-tier edition)

Serverless pipeline that turns LLM gateway telemetry into cost attribution, budget control, waste detection,
model-routing advice and *measured* savings/latency impact.

```
Clients ──► Azure API Management ──► LLM backend
               │  policy emits one event per call: dept, model, tokens, latency, request id
               ├─────────────── cold path (authoritative, nightly) ───────────────┐
               │   App Insights ─► Log Analytics (AppTraces) ─ 1 aggregate KQL ─► │
               │                                                                   ▼
               └── hot path (near real time, optional) ──► Event Hub ──► Azure Functions (Python, finops/ library)
                       log-to-eventhub        POST /api/ingest ──────────►  • ingest_eventhub / /api/ingest  validate · dedupe · batch-id · hourly MERGE
                                                                              • daily_cost_sync     02:00 UTC  ingest + price + alerts
APIM `Requests` metric ─► capture_reconciliation 02:30 UTC ───────────────►   • weekly_routing_review Mon 03:00 UTC  routing · waste · impact · cleanup
                                                                              • /api/report /api/backfill /api/status /api/recommendations/status
                                                                                         │ idempotent MERGE upserts
                                                                                         ▼
                                              Azure SQL (free offer) ─► Power BI (Import mode, see powerbi/)
                                                                      └► webhook alerts (Slack / Teams)
```

## What the resume bullets map to

| Resume claim | Code that backs it | What you can honestly show |
|---|---|---|
| Serverless pipeline intercepting LLM telemetry via APIM, parsing 15 M+ monthly logs with 99.9 % capture reliability | `api_management/*.xml`, `finops/ingest.py` (hot path), `finops/kql.py` + `pipeline.py` (cold path), `finops/reconcile.py` (capture vs. APIM's own counter) | Design handles that volume (batched, idempotent). The **capture rate** is measured against the gateway metric (`vw_CaptureReconciliation`). The volume itself needs real or load-tested traffic. |
| Cost-attribution engine querying Log Analytics via KQL against multi-model pricing, finding inefficiencies and cutting spend 28 % | `finops/kql.py`, `pricing.py`, `database/pricing_catalog.json` (4 providers), `attribution.py`, `efficiency.py`, `routing.py`, `impact.py` | Attribution and waste findings from your data. A **28 %** figure is only valid if `vw_ProgramImpact.SpendReductionPct` shows it on real traffic. |
| Azure SQL + Power BI dashboard on unit economics and latency, enabling model routing that cut latency 35 % | `database/*.sql`, `powerbi/measures.dax`, `impact.py` (before/after latency of accepted recommendations) | Dashboard pages in `powerbi/README.md`. A **35 %** figure is only valid if `AvgLatencyChangePct` shows it after real accepted recommendations. |

Write the numbers on your resume from the report, not the other way round.

## What is new in v3

| Area | v2 | v3 |
|---|---|---|
| Ingest | Nightly KQL pull only | **Plus** a hot path: Event Hub trigger and `POST /api/ingest`. Events are validated, de-duplicated, aggregated in memory per hour/department/model and written with one additive MERGE. A deterministic batch id makes redelivery a no-op; rejects go to `DeadLetterEvents` with a reason. Function retries use exponential backoff. |
| Capture reliability | Share of events with usable tokens (can't see lost events) | **Reconciliation** against the APIM `Requests` metric per UTC day, `capture_gap` alerts below 99.9 %, `over_capture` detection when the filter is wrong |
| Pricing | 4 hard-coded models | `pricing_catalog.json`: 13 prices across Azure OpenAI, Anthropic, Google, Mistral, validated (fallback exists, cheaper, same provider), loaded by `scripts/load_pricing.py`, checked in CI |
| Attribution | Per-model cost views | **Showback**: department and model shares, top model per department, unit costs (`attribution.py`, `/api/report`) |
| Waste | – | **Efficiency review**: prompt bloat, uncapped completions, telemetry gaps, each with an estimated monthly cost and a suggestion (assumptions stated in the finding) |
| Routing | Advisory only | **Lifecycle**: accept/dismiss via API; after `IMPACT_MIN_DAYS_AFTER` days the before/after **cost per request and latency** are measured (`RoutingImpact`, `vw_ProgramImpact`). Rates, not totals; windows with too little traffic stay `insufficient_data`. |
| Reporting | 6 views | + unit economics (cost/1K requests, cost/1M tokens), 7-day latency trend, live-today, hot/cold drift, capture reconciliation, impact views; `powerbi/measures.dax` |
| Dev tooling | seed script | + `send_test_traffic.py` (incl. bad events and duplicates), `--routing-change` demo seed, dummy-key `.env.example` |
| Tests | 42 | **81** (no Azure needed) |

## Setup order

1. **Log Analytics workspace + workspace-based Application Insights.** Keep the default 31-day retention.
2. **APIM (Consumption tier)** → add the App Insights logger, set diagnostic sampling to **100 %**, apply
   `api_management/apim-telemetry-policy.xml` to your LLM API. Clients send `X-Department-ID`.
3. **Azure SQL free offer** database. Choose *"Auto-pause the database until next month"* when asked what to do at the
   limit, so you can never be billed. Run `database/schemas.sql`, then `database/views.sql` (both need `GO` support:
   sqlcmd, Azure Data Studio or the VS Code mssql extension). Then `python scripts/load_pricing.py` to load the catalog.
4. **Function App** (Linux, Python 3.11, Consumption). Enable a system-assigned managed identity, then:
   - Log Analytics workspace → give it **Log Analytics Reader**.
   - APIM resource → give it **Monitoring Reader** (for reconciliation).
   - SQL: `CREATE USER [<function-app-name>] FROM EXTERNAL PROVIDER; ALTER ROLE db_datareader ADD MEMBER [<function-app-name>]; ALTER ROLE db_datawriter ADD MEMBER [<function-app-name>];` (needs an Entra admin set on the SQL server).
   - App settings: see **`docs/CONFIGURATION.md`** (every setting with a dummy example value of the right format).
5. **Optional real-time path.** Create an Event Hubs namespace (Basic tier) and a hub `llm-telemetry`. Add a *Send* policy for APIM and a *Listen*
   policy for the Function. Create the APIM logger (see the header of `api_management/apim-telemetry-eventhub-policy.xml`), then apply that
   policy variant instead of the basic one. Set `EVENTHUB_NAME` and `EVENTHUB_CONNECTION`. Without them the hot path simply stays idle and
   everything else works from Log Analytics.
6. **GitHub**: add the secret `AZURE_FUNCTIONAPP_PUBLISH_PROFILE` (format in `docs/CONFIGURATION.md`), set `AZURE_FUNCTIONAPP_NAME` in the workflow, push to `main`.
7. No traffic yet? `python scripts/seed_sample_data.py --days 40 --spike --routing-change` fills the DB with synthetic `demo-*` data
   (`--purge` removes it), or send events with `python scripts/send_test_traffic.py`.
8. Build the report: `powerbi/README.md`.

## HTTP endpoints (function key required)

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest` | one event, an array, or `{"events":[...]}`; returns 202 with `accepted / duplicates / rejected / replayed` |
| `GET /api/report?days=30` | showback, capture rates, measured savings and latency change, open opportunities, spend so far today |
| `POST /api/recommendations/status` | `accepted` / `dismissed` / `open` (optional `accepted_on` to backdate); `accepted` starts impact measurement |
| `POST /api/backfill` | re-ingest up to 31 complete days from Log Analytics |
| `GET /api/status` | last pipeline runs |

Event shape for ingest: `RequestId` (required), `Timestamp` (ISO-8601, optional), `DepartmentId`, `ModelName`, `PromptTokens`, `CompletionTokens`, `LatencyMs`.

## Free-tier guardrails (limits verified Oct 2026; re-check before relying on them)

- **Azure SQL free offer:** 100,000 vCore-seconds, 32 GB per month, up to 10 free DBs per subscription. The nightly job
  uses seconds of compute; what burns the quota is anything that keeps a session open. Connections here are short-lived,
  and Power BI should use **Import mode with one daily refresh** (DirectQuery would keep it awake). Watch the
  *Free amount remaining* metric. **The hot path wakes the database on every delivered batch**, so with high traffic leave
  it off or raise `maxEventBatchSize` / batch window; the nightly path alone is the cheapest configuration.
- **APIM Consumption:** first 1 M calls/month free per subscription, then ~$3.50 per million.
- **Event Hubs:** has no always-free tier; Basic is billed per throughput unit and per million events. Treat the hot path as optional.
- **Log Analytics / App Insights:** first 5 GB/month ingestion per billing account. Run
  `queries/free_tier_ingestion_check.kql` monthly. Set a **daily cap** on the workspace as a hard stop. `host.json` already
  limits Function telemetry (log level Warning, sampling), so your own traces dominate ingestion.
- **Functions Consumption:** 1 M executions and 400 k GB-s per month (check the current figure for your plan).
- The cold path does **one** Log Analytics query per run and all heavy aggregation happens in KQL.

## Honest limitations

- **The 15 M-logs/month figure will not fit in the 5 GB free ingestion.** At roughly 0.5-1 KB per trace that's ~8-15 GB.
  Describe it as a design validated on synthetic/sampled volume, or sample non-critical departments at APIM.
  The hot path avoids Log Analytics ingestion cost but not Event Hubs cost.
- **Capture rate:** `vw_CaptureReliability` (parse rate) still only sees events that reached Log Analytics. The
  `CaptureReconciliation` table is the independent measure, but it is only as good as `APIM_METRIC_FILTER`:
  streaming responses are 200s the policy skips and will appear as a gap.
- **Hot path is eventually consistent with the cold path.** Hourly rows are for "today so far"; the daily table stays authoritative.
  `vw_HotColdDrift` shows disagreement. Hourly rows are purged after `INGEST_RETENTION_DAYS`.
- **Impact numbers are before/after, not an experiment.** Prompt mix, seasonality or a price change inside the windows move them.
  The code compares per-request rates and refuses to report percentages when traffic is too thin, but you still need judgment.
- **Efficiency savings are estimates** built on stated assumptions (target prompt size, % saved by a completion cap).
- Streaming (`text/event-stream`) responses are skipped by the policy (see above).
- Routing output is advisory. It looks only at token shape, not answer quality. Evaluate before moving traffic.
- Latency numbers are gateway-measured (`context.Elapsed`), so they include APIM overhead.
- Prices in `pricing_catalog.json` are **illustrative**. Verify against each vendor before trusting any dollar figure.
- **Not run against live Azure.** All 81 unit tests pass locally with fakes; the SQL, the Event Hub binding, the APIM
  `log-to-eventhub` expression and the Azure Monitor metric query were written to the documented APIs but never executed
  against real resources. Expect to adjust permissions and test policy expressions in the APIM policy editor on first deploy.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q                      # or: PYTHONPATH=functions python -m unittest discover -s tests
cp functions/local.settings.sample.json functions/local.settings.json   # replace the DUMMY values, then `func start` in functions/
cp .env.example .env                                                    # for the scripts/ helpers
python scripts/load_pricing.py --dry-run                                # validate the price catalog
```

The keys, connection strings and IDs in the sample files are **dummy values in realistic formats** (see `docs/CONFIGURATION.md`).
Pyodbc needs the **Microsoft ODBC Driver 18** (and unixODBC on Linux) when you run against a real database.
