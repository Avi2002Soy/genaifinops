# Configuration reference

> **Every secret value on this page is a DUMMY.** They are random strings in the right *format* so you can see
> what to paste where. They authenticate to nothing. Never commit real keys: real values live in Function App
> settings, GitHub secrets or a git-ignored `local.settings.json` / `.env`.

## Function App settings

| Setting | Required | Dummy example (format only) | Where the real value comes from |
|---|---|---|---|
| `LOG_ANALYTICS_WORKSPACE_ID` | yes | `bbdc79ef-c388-417b-bdef-d3cf4e4665d2` | Log Analytics workspace → Overview → *Workspace ID* |
| `SQL_CONNECTION_STRING` | yes | `Driver={ODBC Driver 18 for SQL Server};Server=tcp:finops-sql-ci01.database.windows.net,1433;Database=finops-db;Uid=finopsadmin;Pwd=<YOUR_PASSWORD>;Encrypt=yes;TrustServerCertificate=no;` | SQL database → *Connection strings* → ODBC. **Omit `Uid`/`Pwd` when `SQL_USE_MANAGED_IDENTITY=true`.** |
| `SQL_USE_MANAGED_IDENTITY` | no | `true` | `true` in Azure (recommended), `false` for local password auth |
| `EVENTHUB_NAME` | for real-time ingest | `llm-telemetry` | Event Hubs namespace → *Event Hubs* |
| `EVENTHUB_CONNECTION` | for real-time ingest | `Endpoint=sb://finops-ehns-ci01.servicebus.windows.net/;SharedAccessKeyName=ListenSend;SharedAccessKey=<YOUR_SHARED_ACCESS_KEY>;EntityPath=llm-telemetry` | Hub → *Shared access policies* (a **Listen** policy for the Function, a separate **Send** policy for the APIM logger). With managed identity use `EVENTHUB_CONNECTION__fullyQualifiedNamespace` instead and grant *Azure Event Hubs Data Receiver*. |
| `APIM_RESOURCE_ID` | for reconciliation | `/subscriptions/c67e23b8-ae8c-44fa-a102-610fe0f400e9/resourceGroups/rg-genai-finops/providers/Microsoft.ApiManagement/service/apim-finops-ci01` | APIM → Properties → *Resource ID*. Function identity needs **Monitoring Reader** on it. |
| `APIM_METRIC_FILTER` | no | `GatewayResponseCode eq '200'` | Must select the same requests the policy emits events for (see below). |
| `ALERT_WEBHOOK_URL` | no | `<YOUR_WEBHOOK_URL>` | Teams Workflows / Logic App "When an HTTP request is received" URL, or a Slack incoming webhook |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | no | `InstrumentationKey=<YOUR_INSTRUMENTATION_KEY>;IngestionEndpoint=https://centralindia-0.in.applicationinsights.azure.com/;LiveEndpoint=https://centralindia.livediagnostics.monitor.azure.com/;ApplicationId=<YOUR_APP_ID>` | Function App's own telemetry (set automatically when you create the app with App Insights) |
| `AzureWebJobsStorage` | yes (Azure) | `DefaultEndpointsProtocol=https;AccountName=<YOUR_ACCOUNT_NAME>;AccountKey=<YOUR_ACCOUNT_KEY>;EndpointSuffix=core.windows.net` | Storage account → *Access keys*. For local-only work use `UseDevelopmentStorage=true` with Azurite. |

### Tunables (all optional, defaults in `functions/finops/config.py`)

| Group | Settings |
|---|---|
| Daily sync / analytics | `LOOKBACK_DAYS`, `HISTORY_DAYS`, `ANOMALY_*`, `BUDGET_WARN_PCT`, `CAPTURE_RATE_FLOOR`, `CAPTURE_MIN_EVENTS` |
| Routing | `ROUTING_WINDOW_DAYS`, `ROUTING_MIN_REQUESTS`, `ROUTING_MAX_AVG_PROMPT_TOKENS`, `ROUTING_MAX_P95_TOTAL_TOKENS`, `ROUTING_MIN_MONTHLY_SAVINGS_USD` |
| Real-time ingest | `INGEST_MAX_AGE_DAYS` (7), `INGEST_MAX_FUTURE_MINUTES` (10), `INGEST_MAX_BODY_BYTES` (1 MB), `INGEST_RETENTION_DAYS` (14) |
| Reconciliation | `RECONCILE_LOOKBACK_DAYS` (2), `RECONCILE_MIN_GATEWAY_REQUESTS` (1000) |
| Inefficiency detection | `EFF_WINDOW_DAYS`, `EFF_MIN_REQUESTS`, `EFF_PROMPT_BLOAT_AVG_TOKENS`, `EFF_PROMPT_BLOAT_RATIO`, `EFF_PROMPT_TARGET_TOKENS`, `EFF_UNCAPPED_COMPLETION_AVG_TOKENS`, `EFF_COMPLETION_CAP_SAVING_PCT`, `EFF_TELEMETRY_GAP_RATIO`, `EFF_MIN_MONTHLY_WASTE_USD` |
| Impact measurement | `IMPACT_WINDOW_DAYS` (14), `IMPACT_MIN_DAYS_AFTER` (7), `IMPACT_MIN_REQUESTS` (200) |

### Choosing `APIM_METRIC_FILTER`

The reconciliation divides *captured events* by the APIM `Requests` metric. The policy only emits an event for
HTTP 200 JSON responses, so the default denominator counts only `GatewayResponseCode eq '200'`. If APIM also
fronts other APIs, narrow it (for example `ApiId eq 'azure-openai' and GatewayResponseCode eq '200'`).
Streaming responses are 200s that the policy skips, so heavy streaming traffic will show up as a "gap".
If the numerator is larger than the denominator the pipeline reports `over_capture`, which means the filter is wrong.

## GitHub Actions secret

`AZURE_FUNCTIONAPP_PUBLISH_PROFILE` is the full contents of the Function App's publish-profile file
(Function App → *Get publish profile*). Shape (dummy):