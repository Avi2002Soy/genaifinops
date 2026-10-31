-- GenAI FinOps schema v2 (idempotent: safe to re-run).
-- Upgrading from v1? Its two tables are derived data - drop them first, then run this file:
--   DROP TABLE IF EXISTS dbo.DailyTokenUsage; DROP TABLE IF EXISTS dbo.ModelPricing;
-- Re-run the pipeline's /api/backfill afterwards to rebuild usage (Log Analytics keeps ~31 days).

-- Effective-dated prices: a price change adds a row instead of rewriting history.
IF OBJECT_ID('dbo.ModelPricing', 'U') IS NULL
CREATE TABLE dbo.ModelPricing (
    ModelName           VARCHAR(100)  NOT NULL,
    EffectiveFrom       DATE          NOT NULL,
    PromptCostPer1K     DECIMAL(12,6) NOT NULL,
    CompletionCostPer1K DECIMAL(12,6) NOT NULL,
    FallbackModel       VARCHAR(100)  NULL,      -- cheaper model the routing review may suggest
    CONSTRAINT PK_ModelPricing PRIMARY KEY (ModelName, EffectiveFrom)
);

-- One row per (day, department, model). The natural key makes re-runs idempotent (MERGE upsert).
IF OBJECT_ID('dbo.DailyTokenUsage', 'U') IS NULL
CREATE TABLE dbo.DailyTokenUsage (
    UsageDate             DATE          NOT NULL,
    DepartmentId          VARCHAR(100)  NOT NULL,
    ModelName             VARCHAR(100)  NOT NULL,
    TotalPromptTokens     BIGINT        NOT NULL,
    TotalCompletionTokens BIGINT        NOT NULL,
    RequestCount          INT           NOT NULL,
    MalformedCount        INT           NOT NULL CONSTRAINT DF_DTU_Malformed DEFAULT 0,
    TotalCost             DECIMAL(18,6) NOT NULL,
    UnpricedFlag          BIT           NOT NULL CONSTRAINT DF_DTU_Unpriced DEFAULT 0,
    P95TotalTokens        INT           NULL,
    AvgLatencyMs          INT           NULL,
    P95LatencyMs          INT           NULL,
    UpdatedAtUtc          DATETIME2     NOT NULL CONSTRAINT DF_DTU_Updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_DailyTokenUsage PRIMARY KEY (UsageDate, DepartmentId, ModelName)
);

IF OBJECT_ID('dbo.Budgets', 'U') IS NULL
CREATE TABLE dbo.Budgets (
    DepartmentId     VARCHAR(100)  NOT NULL CONSTRAINT PK_Budgets PRIMARY KEY,
    MonthlyBudgetUsd DECIMAL(18,2) NOT NULL
);

-- AlertKey is the dedupe key: the same condition notifies once, not every night.
IF OBJECT_ID('dbo.Alerts', 'U') IS NULL
CREATE TABLE dbo.Alerts (
    AlertKey       VARCHAR(200)  NOT NULL CONSTRAINT PK_Alerts PRIMARY KEY,
    AlertType      VARCHAR(30)   NOT NULL,
    Severity       VARCHAR(10)   NOT NULL,
    DepartmentId   VARCHAR(100)  NULL,
    Message        NVARCHAR(1000) NOT NULL,
    Payload        NVARCHAR(MAX) NULL,
    CreatedAtUtc   DATETIME2     NOT NULL CONSTRAINT DF_Alerts_Created DEFAULT SYSUTCDATETIME(),
    NotifiedAtUtc  DATETIME2     NULL
);

IF OBJECT_ID('dbo.RoutingRecommendations', 'U') IS NULL
CREATE TABLE dbo.RoutingRecommendations (
    DepartmentId        VARCHAR(100)  NOT NULL,
    CurrentModel        VARCHAR(100)  NOT NULL,
    SuggestedModel      VARCHAR(100)  NOT NULL,
    Requests            BIGINT        NOT NULL,
    AvgPromptTokens     INT           NOT NULL,
    AvgCompletionTokens INT           NOT NULL,
    P95TotalTokens      INT           NOT NULL,
    WindowCost          DECIMAL(18,6) NOT NULL,
    EstMonthlySavings   DECIMAL(18,2) NOT NULL,
    SavingsPct          DECIMAL(6,1)  NOT NULL,
    Confidence          VARCHAR(10)   NOT NULL,
    LatencyDeltaPct     DECIMAL(7,1)  NULL,
    Status              VARCHAR(10)   NOT NULL CONSTRAINT DF_RR_Status DEFAULT 'open',  -- open | accepted | dismissed | stale
    UpdatedAtUtc        DATETIME2     NOT NULL CONSTRAINT DF_RR_Updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_RoutingRecommendations PRIMARY KEY (DepartmentId, CurrentModel, SuggestedModel)
);

IF OBJECT_ID('dbo.PipelineRuns', 'U') IS NULL
CREATE TABLE dbo.PipelineRuns (
    RunId           BIGINT IDENTITY(1,1) CONSTRAINT PK_PipelineRuns PRIMARY KEY,
    RunType         VARCHAR(20)   NOT NULL,   -- daily | backfill | routing
    StartedAtUtc    DATETIME2     NOT NULL,
    Status          VARCHAR(10)   NOT NULL,   -- success | partial | failed
    WindowStart     DATE          NULL,
    WindowEnd       DATE          NULL,       -- exclusive
    RowsWritten     INT           NOT NULL,
    Requests        BIGINT        NOT NULL,
    MalformedCount  BIGINT        NOT NULL,
    UnpricedRows    INT           NOT NULL,
    AlertsCreated   INT           NOT NULL,
    DurationSeconds DECIMAL(9,2)  NOT NULL,
    Message         NVARCHAR(2000) NULL
);

-- Illustrative prices (USD per 1K tokens). Replace with your actual Azure OpenAI rates.
MERGE dbo.ModelPricing AS t
USING (VALUES
    ('gpt-3.5-turbo', CAST('2000-01-01' AS DATE), 0.000500, 0.001500, NULL),
    ('gpt-4',         CAST('2000-01-01' AS DATE), 0.030000, 0.060000, 'gpt-3.5-turbo'),
    ('gpt-4o',        CAST('2000-01-01' AS DATE), 0.002500, 0.010000, 'gpt-4o-mini'),
    ('gpt-4o-mini',   CAST('2000-01-01' AS DATE), 0.000150, 0.000600, NULL)
) AS s (ModelName, EffectiveFrom, PromptCostPer1K, CompletionCostPer1K, FallbackModel)
ON t.ModelName = s.ModelName AND t.EffectiveFrom = s.EffectiveFrom
WHEN NOT MATCHED THEN INSERT (ModelName, EffectiveFrom, PromptCostPer1K, CompletionCostPer1K, FallbackModel)
     VALUES (s.ModelName, s.EffectiveFrom, s.PromptCostPer1K, s.CompletionCostPer1K, s.FallbackModel);

GO
-- ===================================================================================================
-- v3 additions (idempotent). GO separators matter: run with sqlcmd / Azure Data Studio / VS Code mssql.
-- ===================================================================================================

-- Which vendor a model belongs to (set by scripts/load_pricing.py from database/pricing_catalog.json).
IF COL_LENGTH('dbo.ModelPricing', 'Provider') IS NULL
    ALTER TABLE dbo.ModelPricing ADD Provider VARCHAR(30) NULL;
-- When a human accepted a routing recommendation (starts the before/after impact measurement).
IF COL_LENGTH('dbo.RoutingRecommendations', 'AcceptedAtUtc') IS NULL
    ALTER TABLE dbo.RoutingRecommendations ADD AcceptedAtUtc DATETIME2 NULL;
GO

-- Hot path: near-real-time usage written by the Event Hub / HTTP ingest. ADDITIVE counters (see
-- IngestBatches for replay protection). The daily table above stays the authoritative record.
IF OBJECT_ID('dbo.HourlyTokenUsage', 'U') IS NULL
CREATE TABLE dbo.HourlyTokenUsage (
    BucketHourUtc         DATETIME2(0)  NOT NULL,
    DepartmentId          VARCHAR(100)  NOT NULL,
    ModelName             VARCHAR(100)  NOT NULL,
    TotalPromptTokens     BIGINT        NOT NULL,
    TotalCompletionTokens BIGINT        NOT NULL,
    RequestCount          INT           NOT NULL,
    TotalCost             DECIMAL(18,6) NOT NULL,
    UnpricedFlag          BIT           NOT NULL CONSTRAINT DF_HTU_Unpriced DEFAULT 0,
    P95TotalTokens        INT           NULL,
    AvgLatencyMs          INT           NULL,
    P95LatencyMs          INT           NULL,
    UpdatedAtUtc          DATETIME2     NOT NULL CONSTRAINT DF_HTU_Updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_HourlyTokenUsage PRIMARY KEY (BucketHourUtc, DepartmentId, ModelName)
);

-- One row per stored batch; inserting the same BatchId twice fails, which is how replays are detected.
IF OBJECT_ID('dbo.IngestBatches', 'U') IS NULL
CREATE TABLE dbo.IngestBatches (
    BatchId       VARCHAR(64) NOT NULL CONSTRAINT PK_IngestBatches PRIMARY KEY,
    EventCount    INT         NOT NULL,
    ReceivedAtUtc DATETIME2   NOT NULL CONSTRAINT DF_IB_Received DEFAULT SYSUTCDATETIME()
);

-- Events that failed validation, with the reason. Nothing is dropped silently.
IF OBJECT_ID('dbo.DeadLetterEvents', 'U') IS NULL
CREATE TABLE dbo.DeadLetterEvents (
    DeadLetterId  BIGINT IDENTITY(1,1) CONSTRAINT PK_DeadLetterEvents PRIMARY KEY,
    BatchId       VARCHAR(64)    NOT NULL,
    Reason        NVARCHAR(200)  NOT NULL,
    RawEvent      NVARCHAR(500)  NULL,
    ReceivedAtUtc DATETIME2      NOT NULL CONSTRAINT DF_DLE_Received DEFAULT SYSUTCDATETIME()
);

-- Captured telemetry vs. APIM's own request counter, per complete UTC day.
IF OBJECT_ID('dbo.CaptureReconciliation', 'U') IS NULL
CREATE TABLE dbo.CaptureReconciliation (
    UsageDate        DATE         NOT NULL CONSTRAINT PK_CaptureReconciliation PRIMARY KEY,
    GatewayRequests  BIGINT       NOT NULL,
    CapturedRequests BIGINT       NOT NULL,
    CaptureRate      DECIMAL(9,6) NULL,
    Status           VARCHAR(20)  NOT NULL,   -- ok | gap | over_capture | no_gateway_data
    CheckedAtUtc     DATETIME2    NOT NULL CONSTRAINT DF_CR_Checked DEFAULT SYSUTCDATETIME()
);

-- Costed waste patterns found by the weekly efficiency review.
IF OBJECT_ID('dbo.CostInefficiencies', 'U') IS NULL
CREATE TABLE dbo.CostInefficiencies (
    DepartmentId    VARCHAR(100)   NOT NULL,
    ModelName       VARCHAR(100)   NOT NULL,
    Kind            VARCHAR(30)    NOT NULL,  -- prompt_bloat | uncapped_completion | telemetry_gap
    Severity        VARCHAR(10)    NOT NULL,
    Requests        BIGINT         NOT NULL,
    EstMonthlyWaste DECIMAL(18,2)  NOT NULL,
    Detail          NVARCHAR(500)  NOT NULL,
    Suggestion      NVARCHAR(300)  NOT NULL,
    Status          VARCHAR(10)    NOT NULL CONSTRAINT DF_CI_Status DEFAULT 'open',   -- open | dismissed | stale
    UpdatedAtUtc    DATETIME2      NOT NULL CONSTRAINT DF_CI_Updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_CostInefficiencies PRIMARY KEY (DepartmentId, ModelName, Kind)
);

-- Measured before/after effect of each accepted routing recommendation.
IF OBJECT_ID('dbo.RoutingImpact', 'U') IS NULL
CREATE TABLE dbo.RoutingImpact (
    DepartmentId         VARCHAR(100)  NOT NULL,
    CurrentModel         VARCHAR(100)  NOT NULL,
    SuggestedModel       VARCHAR(100)  NOT NULL,
    AcceptedOn           DATE          NOT NULL,
    Status               VARCHAR(20)   NOT NULL,   -- measured | insufficient_data
    BeforeRequests       BIGINT        NOT NULL,
    AfterRequests        BIGINT        NOT NULL,
    BeforeCostPerRequest DECIMAL(18,6) NULL,
    AfterCostPerRequest  DECIMAL(18,6) NULL,
    AvoidedCost          DECIMAL(18,2) NOT NULL,
    CostReductionPct     DECIMAL(7,1)  NULL,
    BeforeLatencyMs      DECIMAL(10,1) NULL,
    AfterLatencyMs       DECIMAL(10,1) NULL,
    LatencyChangePct     DECIMAL(7,1)  NULL,       -- negative = faster after the switch
    ComputedAtUtc        DATETIME2     NOT NULL CONSTRAINT DF_RI_Computed DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_RoutingImpact PRIMARY KEY (DepartmentId, CurrentModel, SuggestedModel)
);
GO
