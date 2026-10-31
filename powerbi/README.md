# Power BI report: build guide

Goal: one report that answers "what do we spend, on what, is it getting cheaper, and is it fast enough?"

## Connect (Import mode, one refresh per day)
1. Get data → **Azure SQL database** → server `finops-sql-ci01.database.windows.net`, database `finops-db`
   (use your own), **Import** (not DirectQuery: DirectQuery keeps the free serverless DB awake).
2. Load the views listed at the top of `measures.dax`. Add the DAX from that file.
3. Schedule refresh once a day, after the 02:00 UTC sync (for example 04:00 UTC).
   Give the service the SQL credentials of a **read-only** user (`db_datareader`).

## Pages
| Page | Visuals | Source |
|---|---|---|
| **Overview** | KPI cards: Cost MTD, Cost MoM %, Budget Used %, Capture Rate (gateway), Spend Reduction %, Latency Change %. Line: daily cost by department | `vw_DailyCost`, `vw_BudgetStatus`, `vw_ProgramImpact` |
| **Unit economics** | Matrix department × model: Cost per 1K Requests, Cost per 1M Tokens, Tokens per Request. Scatter: cost/request vs. latency, size = requests | `vw_DepartmentUnitEconomics` |
| **Latency** | Line: `AvgLatency7d` by model. Table: p95 by model. Slicer: department | `vw_LatencyTrend`, `vw_DailyCost` |
| **Savings & impact** | Table of accepted recommendations: before/after cost per request, before/after latency, Avoided Cost. Cards: Realised Savings, Spend Reduction %, Latency Change % | `vw_RoutingImpact`, `vw_ProgramImpact` |
| **Opportunities** | Open routing recommendations and inefficiencies sorted by monthly saving, with the suggestion text | `vw_OpenRoutingRecommendations`, `vw_OpenInefficiencies` |
| **Data quality** | Capture rate vs. gateway per day with a 99.9 % reference line; unpriced models; hot/cold drift | `vw_CaptureReconciliation`, `vw_HotColdDrift`, `vw_DailyCost` |

## Rules of thumb
- Never average averages: use the weighted `Avg Latency (ms)` measure.
- Show the *Status* of impact rows. `insufficient_data` rows have no percentages on purpose.
- The Savings & impact page only means something after you have **accepted** recommendations
  (`POST /api/recommendations/status`) and waited `IMPACT_MIN_DAYS_AFTER` days.
- No traffic yet? `python scripts/seed_sample_data.py --days 40 --spike --routing-change` fills every page
  with synthetic `demo-*` data (clearly synthetic: do not screenshot it as a result).
