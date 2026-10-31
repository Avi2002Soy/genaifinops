# GenAI FinOps & Token Analytics (Azure Free-Tier Edition)

A serverless, event-driven pipeline designed to turn LLM gateway telemetry into actionable FinOps insights. This project intercepts traffic data to provide precise cost attribution, strict budget control, waste detection, model-routing advice, and empirically measured savings and latency impact.

## Project Overview & Architecture

Managing Generative AI costs at scale requires visibility beyond standard API billing. This pipeline intercepts traffic at the API gateway layer to track usage per department, model, and request, allowing organizations to attribute costs accurately and route traffic efficiently.

### Architecture Flow

```text
Clients ──► Azure API Management ──► LLM Backend
               │  (Policy emits one event per call: dept, model, tokens, latency, request id)
               │
               ├─────────────── COLD PATH (Authoritative, Nightly) ───────────────┐
               │   App Insights ─► Log Analytics (AppTraces) ─ 1 aggregate KQL ─► │
               │                                                                  ▼
               └── HOT PATH (Near Real-Time, Optional) ──► Event Hub ──► Azure Functions (Python, finops/ library)
                       log-to-eventhub        POST /api/ingest ─────────►  • ingest_eventhub / /api/ingest
                                                                             (validate · dedupe · batch-id · hourly MERGE)
                                                                           
- daily_cost_sync     (02:00 UTC)  ingest + price + alerts                 • weekly_routing_review (Mon 03:00 UTC)
APIM `Requests` metric ─► capture_reconciliation (02:30 UTC) ────────────►   (routing · waste · impact · cleanup)
- /api/report, /api/backfill, /api/status, /api/recommendations/status
                                                                           │ idempotent MERGE upserts
                                                                           ▼
                                              Azure SQL (Free Offer) ─► Power BI (Import mode, see powerbi/)
                                                                      └─► Webhook Alerts (Slack / Teams)
```

## Core Capabilities (What's New in v3)

This major release introduces a near real-time ingestion path, verifiable capture reliability, and a comprehensive recommendations lifecycle.

| Area | v2 Implementation | v3 Enhancements |
|---|---|---|
| **Ingest** | Nightly KQL pull only | **Added Hot Path:** Event Hub trigger and `POST /api/ingest`. Events are validated, de-duplicated, aggregated in memory, and written with an additive MERGE. Rejects route to `DeadLetterEvents`. |
| **Capture Reliability** | Share of events with usable tokens | **Reconciliation:** Compares data against the APIM `Requests` metric per UTC day. Triggers `capture_gap` alerts (if < 99.9%) and detects `over_capture`. |
| **Pricing** | 4 hard-coded models | **Dynamic Catalog:** `pricing_catalog.json` with 13 prices across Azure OpenAI, Anthropic, Google, and Mistral. Validated and checked in CI. |
| **Attribution** | Per-model cost views | **Showback:** Department and model shares, top models per department, and detailed unit costs. |
| **Waste Detection** | None | **Efficiency Review:** Identifies prompt bloat, uncapped completions, and telemetry gaps, paired with estimated monthly costs and mitigation suggestions. |
| **Routing** | Advisory only | **Lifecycle Management:** Accept/dismiss via API. After `IMPACT_MIN_DAYS_AFTER`, the before/after cost per request and latency are empirically measured. |
| **Reporting** | 6 basic views | **Expanded Metrics:** Unit economics (cost/1K requests, cost/1M tokens), 7-day latency trends, hot/cold drift, capture reconciliation, and impact views. |
| **Testing** | 42 tests | **81 tests** (Fully abstracted; no active Azure connection required). |

## Portfolio Guide: Mapping Claims to Code

Write the numbers on your resume based on what your deployed report actually measures, not the other way around.

| Resume Claim | Backing Code | Verification Standard |
|---|---|---|
| Serverless pipeline intercepting LLM telemetry via APIM, parsing 15M+ monthly logs with 99.9% capture reliability. | `api_management/*.xml`, `finops/ingest.py`, `finops/kql.py`, `finops/reconcile.py` | Design handles volume via idempotent batching. Capture rate is explicitly measured in `vw_CaptureReconciliation`. |
| Cost-attribution engine querying Log Analytics against multi-model pricing, cutting spend by 28%. | `finops/kql.py`, `pricing.py`, `database/pricing_catalog.json`, `efficiency.py` | A 28% figure is only valid if `vw_ProgramImpact.SpendReductionPct` demonstrates it using real traffic data. |
| Azure SQL + Power BI dashboard on unit economics and latency, enabling model routing that cut latency 35%. | `database/*.sql`, `powerbi/measures.dax`, `impact.py` | A 35% figure is only valid if `AvgLatencyChangePct` reflects it after logging real accepted recommendations. |

## Deployment & Setup

Follow these steps in order to deploy the infrastructure and start processing data.

1. **Log Analytics & App Insights:** Provision a workspace-based Application Insights instance. Keep the default 31-day retention.
2. **API Management (Consumption Tier):** 
   * Add the App Insights logger and set diagnostic sampling to **100%**.
   * Apply `api_management/apim-telemetry-policy.xml` to your LLM API. 
   * Ensure clients send the `X-Department-ID` header.
3. **Azure SQL (Free Offer):** 
   * Provision the database and select *"Auto-pause the database until next month"* to strictly prevent billing. 
   * Execute `database/schemas.sql`, followed by `database/views.sql`. 
   * Run `python scripts/load_pricing.py` to seed the pricing catalog.
4. **Function App (Linux, Python 3.11, Consumption Tier):** 
   * Enable a system-assigned managed identity.
   * Grant **Log Analytics Reader** on the Log Analytics workspace.
   * Grant **Monitoring Reader** on the APIM resource (for reconciliation).
   * Configure SQL Access: `CREATE USER [<function-app-name>] FROM EXTERNAL PROVIDER; ALTER ROLE db_datareader ADD MEMBER [<function-app-name>]; ALTER ROLE db_datawriter ADD MEMBER [<function-app-name>];`
   * Populate App Settings as documented in `docs/CONFIGURATION.md`.
5. **Real-Time Path (Optional):** 
   * Create an Event Hubs namespace (Basic tier) and a hub named `llm-telemetry`. 
   * Add a *Send* policy for APIM and a *Listen* policy for the Function. 
   * Apply `api_management/apim-telemetry-eventhub-policy.xml` to APIM. 
   * Set `EVENTHUB_NAME` and `EVENTHUB_CONNECTION` in the Function App.
6. **GitHub Actions:** Add `AZURE_FUNCTIONAPP_PUBLISH_PROFILE` to repository secrets, set `AZURE_FUNCTIONAPP_NAME` in your workflow, and push to the `main` branch.
7. **Generate Traffic:** Run `python scripts/seed_sample_data.py --days 40 --spike --routing-change` to populate the DB with synthetic data. Use `--purge` to clean it later.
8. **Visualization:** Build the Power BI report using the guide in `powerbi/README.md`.

## HTTP Endpoints

*Note: A valid function key is required for all endpoints.*

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest` | Accepts a single event, an array, or `{"events":[...]}`. Returns `202` with accepted/rejected counts. |
| `GET /api/report?days=30` | Returns showback data, capture rates, measured savings, open opportunities, and daily spend. |
| `POST /api/recommendations/status` | Updates routing status: `accepted` / `dismissed` / `open`. Accepting triggers impact measurement. |
| `POST /api/backfill` | Re-ingests up to 31 complete days of historical data from Log Analytics. |
| `GET /api/status` | Reports the timestamps of the last pipeline runs. |

> **Ingest Payload Shape:** `RequestId` (required), `Timestamp` (ISO-8601, optional), `DepartmentId`, `ModelName`, `PromptTokens`, `CompletionTokens`, `LatencyMs`.

## Azure Free-Tier Guardrails (As of Oct 2026)

* **Azure SQL:** 100,000 vCore-seconds and 32 GB/month. The nightly job uses minimal compute. Ensure Power BI uses **Import mode with a daily refresh** (DirectQuery will keep the DB awake and drain quota). Note: The hot path wakes the database on every batch; for high traffic, raise `maxEventBatchSize` or rely solely on the cold path.
* **APIM Consumption:** First 1M calls/month are free.
* **Event Hubs:** No always-free tier. Billed per throughput unit and per million events. Treat the hot path as strictly optional for cost-savings.
* **Log Analytics / App Insights:** First 5 GB/month ingestion is free. Run `queries/free_tier_ingestion_check.kql` regularly and set a daily cap.
* **Functions Consumption:** 1M executions and 400k GB-s per month. The cold path requires only one Log Analytics query per run.

## Known Limitations & Trade-offs

* **Ingestion Limits:** A volume of 15M logs/month will exceed the 5 GB free Log Analytics tier. To run entirely on the free tier, sample non-critical departments or rely on synthetic/load-tested data to prove the architecture.
* **Event Consistency:** The hot path is eventually consistent with the cold path. Hourly rows represent "today so far," while the daily table remains authoritative. `vw_HotColdDrift` highlights any discrepancies.
* **Impact Measurement Methodology:** Impact numbers are calculated before/after rather than via A/B testing. Changes in prompt mix, seasonality, or vendor pricing during the measurement window will skew results.
* **Streaming Responses:** Streaming (`text/event-stream`) responses return an HTTP 200 immediately, causing the policy to skip token counting. Heavy streaming traffic will appear as a capture gap.
* **Pricing Accuracy:** Prices in `pricing_catalog.json` are illustrative. Always verify against vendor documentation before reporting financial metrics.
* **Local Testing vs. Live Azure:** All 81 unit tests pass locally using fakes. Cloud-specific configurations (SQL permissions, Event Hub bindings, APIM policies) will require manual adjustment upon initial deployment.

## Local Development

```bash
# Setup virtual environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Run test suite
python -m pytest -q
# Alternatively: PYTHONPATH=functions python -m unittest discover -s tests

# Configure local settings (Replace DUMMY values in the copied files)
cp functions/local.settings.sample.json functions/local.settings.json
cp .env.example .env

# Validate the price catalog locally
python scripts/load_pricing.py --dry-run
```

*For details on the dummy connection strings used in the sample configurations, see `docs/CONFIGURATION.md`.*
