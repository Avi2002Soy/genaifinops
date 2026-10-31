-- Reporting views for Power BI. Run with sqlcmd / Azure Data Studio / VS Code (the GO separators matter).
-- Power BI tip for the free SQL tier: use IMPORT mode with one scheduled refresh per day.
-- DirectQuery keeps the serverless DB awake and burns the 100,000 free vCore-seconds.

CREATE OR ALTER VIEW dbo.vw_DailyCost AS
SELECT UsageDate, DepartmentId, ModelName, RequestCount,
       TotalPromptTokens, TotalCompletionTokens,
       TotalPromptTokens + TotalCompletionTokens                         AS TotalTokens,
       TotalCost, UnpricedFlag, AvgLatencyMs, P95LatencyMs,
       CAST(TotalCost / NULLIF(RequestCount, 0) AS DECIMAL(18,6))        AS CostPerRequest,
       CAST(TotalCost / NULLIF((TotalPromptTokens + TotalCompletionTokens) / 1000.0, 0) AS DECIMAL(18,6)) AS CostPer1KTokens
FROM dbo.DailyTokenUsage;
GO

CREATE OR ALTER VIEW dbo.vw_ModelUnitEconomics AS
SELECT DATEFROMPARTS(YEAR(UsageDate), MONTH(UsageDate), 1) AS MonthStart,
       ModelName,
       SUM(RequestCount)                                    AS Requests,
       SUM(TotalPromptTokens + TotalCompletionTokens)       AS TotalTokens,
       SUM(TotalCost)                                       AS TotalCost,
       CAST(SUM(TotalCost) / NULLIF(SUM(RequestCount), 0) AS DECIMAL(18,6)) AS CostPerRequest,
       CAST(SUM(TotalCost) / NULLIF(SUM(TotalPromptTokens + TotalCompletionTokens) / 1000.0, 0) AS DECIMAL(18,6)) AS CostPer1KTokens,
       SUM(CAST(AvgLatencyMs AS FLOAT) * RequestCount)
         / NULLIF(SUM(CASE WHEN AvgLatencyMs IS NOT NULL THEN RequestCount END), 0) AS AvgLatencyMs
FROM dbo.DailyTokenUsage
GROUP BY DATEFROMPARTS(YEAR(UsageDate), MONTH(UsageDate), 1), ModelName;
GO

CREATE OR ALTER VIEW dbo.vw_BudgetStatus AS
SELECT b.DepartmentId, b.MonthlyBudgetUsd,
       COALESCE(SUM(u.TotalCost), 0)                                          AS MonthToDateCost,
       CAST(COALESCE(SUM(u.TotalCost), 0) / NULLIF(b.MonthlyBudgetUsd, 0) AS DECIMAL(9,4)) AS PctOfBudget
FROM dbo.Budgets b
LEFT JOIN dbo.DailyTokenUsage u
       ON u.DepartmentId = b.DepartmentId
      AND u.UsageDate >= DATEFROMPARTS(YEAR(SYSUTCDATETIME()), MONTH(SYSUTCDATETIME()), 1)
GROUP BY b.DepartmentId, b.MonthlyBudgetUsd;
GO

-- Share of gateway events whose token counts were usable. Measures PARSE reliability of events that
-- reached Log Analytics; it can't see events dropped before ingestion (see README).
CREATE OR ALTER VIEW dbo.vw_CaptureReliability AS
SELECT UsageDate,
       SUM(RequestCount)                                                   AS GoodEvents,
       SUM(MalformedCount)                                                 AS MalformedEvents,
       CAST(SUM(RequestCount) * 1.0 / NULLIF(SUM(RequestCount) + SUM(MalformedCount), 0) AS DECIMAL(9,6)) AS CaptureRate
FROM dbo.DailyTokenUsage
GROUP BY UsageDate;
GO

CREATE OR ALTER VIEW dbo.vw_OpenRoutingRecommendations AS
SELECT * FROM dbo.RoutingRecommendations WHERE Status = 'open';
GO

-- ===================================================================================================
-- v3 reporting views (Power BI pages: Unit economics, Latency, Savings & impact, Capture reliability)
-- ===================================================================================================

-- Unit economics per month, department and model, with the department's share of its own spend.
CREATE OR ALTER VIEW dbo.vw_DepartmentUnitEconomics AS
SELECT DATEFROMPARTS(YEAR(UsageDate), MONTH(UsageDate), 1) AS MonthStart,
       DepartmentId, ModelName,
       SUM(RequestCount)                                                          AS Requests,
       SUM(TotalPromptTokens)                                                     AS PromptTokens,
       SUM(TotalCompletionTokens)                                                 AS CompletionTokens,
       SUM(TotalCost)                                                             AS TotalCost,
       CAST(SUM(TotalCost) / NULLIF(SUM(RequestCount), 0) AS DECIMAL(18,6))        AS CostPerRequest,
       CAST(SUM(TotalCost) * 1000.0 / NULLIF(SUM(RequestCount), 0) AS DECIMAL(18,4)) AS CostPer1KRequests,
       CAST(SUM(TotalCost) * 1000000.0 / NULLIF(SUM(TotalPromptTokens + TotalCompletionTokens), 0) AS DECIMAL(18,4)) AS CostPer1MTokens,
       SUM(TotalPromptTokens + TotalCompletionTokens) / NULLIF(SUM(RequestCount), 0) AS TokensPerRequest,
       SUM(CAST(AvgLatencyMs AS FLOAT) * RequestCount)
         / NULLIF(SUM(CASE WHEN AvgLatencyMs IS NOT NULL THEN RequestCount END), 0) AS AvgLatencyMs,
       CAST(SUM(TotalCost) * 1.0 / NULLIF(SUM(SUM(TotalCost)) OVER (PARTITION BY DATEFROMPARTS(YEAR(UsageDate), MONTH(UsageDate), 1), DepartmentId), 0) AS DECIMAL(9,4)) AS ShareOfDepartmentSpend
FROM dbo.DailyTokenUsage
GROUP BY DATEFROMPARTS(YEAR(UsageDate), MONTH(UsageDate), 1), DepartmentId, ModelName;
GO

-- Daily request-weighted latency per model plus a 7-day moving average (smooth line for the latency page).
CREATE OR ALTER VIEW dbo.vw_LatencyTrend AS
WITH d AS (
    SELECT UsageDate, ModelName,
           SUM(RequestCount) AS Requests,
           SUM(CAST(AvgLatencyMs AS FLOAT) * RequestCount)
             / NULLIF(SUM(CASE WHEN AvgLatencyMs IS NOT NULL THEN RequestCount END), 0) AS AvgLatencyMs,
           MAX(P95LatencyMs) AS P95LatencyMs
    FROM dbo.DailyTokenUsage
    GROUP BY UsageDate, ModelName
)
SELECT UsageDate, ModelName, Requests, AvgLatencyMs, P95LatencyMs,
       AVG(AvgLatencyMs) OVER (PARTITION BY ModelName ORDER BY UsageDate ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS AvgLatency7d
FROM d;
GO

-- Today so far, from the hot path (hourly). Refresh more often than the daily import if you want a live tile.
CREATE OR ALTER VIEW dbo.vw_LiveToday AS
SELECT BucketHourUtc, DepartmentId, ModelName, RequestCount, TotalCost, AvgLatencyMs, UnpricedFlag
FROM dbo.HourlyTokenUsage
WHERE BucketHourUtc >= CAST(CAST(SYSUTCDATETIME() AS DATE) AS DATETIME2);
GO

-- Hot path vs. authoritative daily numbers: a large gap means ingest and the Log Analytics sync disagree.
CREATE OR ALTER VIEW dbo.vw_HotColdDrift AS
SELECT d.UsageDate,
       d.Requests AS DailyRequests, h.Requests AS HourlyRequests,
       CAST(h.Requests * 1.0 / NULLIF(d.Requests, 0) AS DECIMAL(9,4)) AS HourlyToDailyRatio,
       d.Cost AS DailyCost, h.Cost AS HourlyCost
FROM (SELECT UsageDate, SUM(RequestCount) AS Requests, SUM(TotalCost) AS Cost
      FROM dbo.DailyTokenUsage GROUP BY UsageDate) d
JOIN (SELECT CAST(BucketHourUtc AS DATE) AS UsageDate, SUM(RequestCount) AS Requests, SUM(TotalCost) AS Cost
      FROM dbo.HourlyTokenUsage GROUP BY CAST(BucketHourUtc AS DATE)) h
  ON h.UsageDate = d.UsageDate;
GO

-- Capture rate against the gateway's own counter (the defensible number), with an SLA flag.
CREATE OR ALTER VIEW dbo.vw_CaptureReconciliation AS
SELECT UsageDate, GatewayRequests, CapturedRequests, CaptureRate, Status,
       CAST(CASE WHEN CaptureRate >= 0.999 THEN 1 ELSE 0 END AS BIT) AS MeetsTarget
FROM dbo.CaptureReconciliation;
GO

CREATE OR ALTER VIEW dbo.vw_OpenInefficiencies AS
SELECT * FROM dbo.CostInefficiencies WHERE Status = 'open';
GO

CREATE OR ALTER VIEW dbo.vw_RoutingImpact AS
SELECT i.*, r.Confidence, r.EstMonthlySavings AS PredictedMonthlySavings, r.LatencyDeltaPct AS PredictedLatencyDeltaPct
FROM dbo.RoutingImpact i
LEFT JOIN dbo.RoutingRecommendations r
  ON r.DepartmentId = i.DepartmentId AND r.CurrentModel = i.CurrentModel AND r.SuggestedModel = i.SuggestedModel;
GO

-- Single-row program KPIs: spend reduction vs. same traffic at the old unit cost, and request-weighted latency change.
CREATE OR ALTER VIEW dbo.vw_ProgramImpact AS
SELECT COUNT(*)                                                        AS RecommendationsMeasured,
       COALESCE(SUM(AvoidedCost), 0)                                   AS AvoidedCostUsd,
       CAST(SUM(AvoidedCost) * 100.0
            / NULLIF(SUM(AvoidedCost) + SUM(AfterCostPerRequest * AfterRequests), 0) AS DECIMAL(7,1)) AS SpendReductionPct,
       CAST(SUM(LatencyChangePct * AfterRequests)
            / NULLIF(SUM(CASE WHEN LatencyChangePct IS NOT NULL THEN AfterRequests END), 0) AS DECIMAL(7,1)) AS AvgLatencyChangePct
FROM dbo.RoutingImpact
WHERE Status = 'measured';
GO
