# 02 - 慢 SQL 采集（按时间桶聚合，支持与服务器指标对齐）

> **目的**：用 Extended Events 抓取高消耗 SQL，**按 5 分钟时间桶聚合**每条 SQL 指纹的：执行次数、总 CPU、总耗时、总逻辑读。当 PerfMon 显示 CPU / 内存异常时，直接取同一时间桶的 Top SQL 做关联分析。
> **最终产物**：按日导出 `D:\DBA\insagent-data\slow_sql\YYYY-MM-DD\*.csv`，拷贝到 agent 的 `$DATA_ROOT/slow_sql/YYYY-MM-DD/`。
> **关键优化（本次重写）**：
> - 新增 `SlowQuery_TimeBucket` 表：**以 5 分钟桶 + SQL 指纹为主键**，记录 ExecCount / TotalCpuMs / TotalDurationMs / TotalLogicalReads / AvgRowCount
> - 扩展事件增加 `duration` 字段（原脚本缺失，无法统计总耗时）
> - 阈值参数化（默认 CPU > 500ms **或** 逻辑读 > 5万）
> - 补齐原脚本缺失的 `SlowSQL_DailySummary` / `SQL_Text_Dictionary` 表定义
> - 导出 CSV 时，时间桶文件列与 `perfmon_5min_*.csv` 的 `BucketStart` 时间轴完全对齐
>
> **调度频率说明（重要）**：
> - Extended Events 已由 SQL Server 自动写入 `.xel` 文件（`100MB × 20 = 2GB` 滚动缓冲），原始数据不依赖 Job 采集
> - **5 分钟只是"分析粒度"（用于和 PerfMon 5min 桶 JOIN），不是"采集频率"**
> - 因此 `Ingest` 与 `BuildBucket` 改为 **每小时跑一次**（批量增量处理所有未聚合桶），SQL Agent 压力从 288 次/天 → 24 次/天
> - 聚合过程使用 `SlowBucket_Watermark` 表记录断点，保证桶数据完整且可重跑

---

## 第一步：部署扩展事件（捕获原始事件到 .xel）

```sql
-- 仅在 SQL Server 实例本地执行
IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = 'HighResourceUsage_Tracking')
    DROP EVENT SESSION [HighResourceUsage_Tracking] ON SERVER;
GO

CREATE EVENT SESSION [HighResourceUsage_Tracking] ON SERVER
ADD EVENT sqlserver.sql_batch_completed(
    ACTION(sqlserver.client_hostname, sqlserver.database_name, sqlserver.nt_username,
           sqlserver.sql_text, sqlserver.session_id)
    WHERE ([cpu_time] > (500000) OR [logical_reads] > (50000))   -- 500ms CPU 或 5万逻辑读
),
ADD EVENT sqlserver.rpc_completed(
    ACTION(sqlserver.client_hostname, sqlserver.database_name, sqlserver.nt_username,
           sqlserver.sql_text, sqlserver.session_id)
    WHERE ([cpu_time] > (500000) OR [logical_reads] > (50000))
)
ADD TARGET package0.event_file(
    SET filename = N'C:\DBA\xel\HighResourceUsage_Tracking.xel',
        max_file_size = (100),
        max_rollover_files = (20))
WITH (MAX_MEMORY = 8192 KB,
      EVENT_RETENTION_MODE = ALLOW_SINGLE_EVENT_LOSS,
      MAX_DISPATCH_LATENCY = 30 SECONDS,
      TRACK_CAUSALITY = OFF,
      STARTUP_STATE = ON);   -- 开机自启
GO

ALTER EVENT SESSION [HighResourceUsage_Tracking] ON SERVER STATE = START;
GO
```

---

## 第二步：监控库建表

```sql
USE [db_monitor]
GO

-- 1) 原始日志（指纹去重，保留 7 天）
IF OBJECT_ID('dbo.SlowQueryLog','U') IS NULL
CREATE TABLE dbo.SlowQueryLog(
    Id                BIGINT IDENTITY(1,1) PRIMARY KEY,
    SqlFingerprint    AS (CONVERT(CHAR(64), HASHBYTES('SHA2_256', LEFT(LTRIM(SqlText), 500)), 2)) PERSISTED,
    SqlText           NVARCHAR(MAX) NOT NULL,
    SqlTextShort      AS (LEFT(SqlText, 800)) PERSISTED,
    DatabaseName      NVARCHAR(128),
    ExecuteAccount    NVARCHAR(256),
    ClientHostname    NVARCHAR(256),
    CpuMs             BIGINT,
    DurationMs        BIGINT,            -- 新增
    LogicalReads      BIGINT,
    PhysicalReads     BIGINT,
    Writes            BIGINT,
    RowCount_         BIGINT,            -- 新增
    EventTimeUtc      DATETIME2          NOT NULL,
    CollectDate       AS CAST(EventTimeUtc AS DATE) PERSISTED,
    INDEX IX_SlowQueryLog_Date NONCLUSTERED (CollectDate, SqlFingerprint)
);
GO

-- 2) ★ 核心表：5 分钟桶聚合。一个(桶,指纹) 一行
IF OBJECT_ID('dbo.SlowQuery_TimeBucket','U') IS NULL
CREATE TABLE dbo.SlowQuery_TimeBucket(
    Id                BIGINT IDENTITY(1,1) PRIMARY KEY,
    BucketStart       DATETIME2     NOT NULL,                -- 整 5 分钟
    SqlFingerprint    CHAR(64)      NOT NULL,
    SqlTextSample     NVARCHAR(800) NOT NULL,                -- 喂 AI 用
    DatabaseName      NVARCHAR(128),
    ExecCount         INT           NOT NULL,
    TotalCpuMs        BIGINT        NOT NULL,
    TotalDurationMs   BIGINT        NOT NULL,
    TotalLogicalReads BIGINT        NOT NULL,
    AvgCpuMs          AS (CASE WHEN ExecCount = 0 THEN 0 ELSE TotalCpuMs        / ExecCount END) PERSISTED,
    AvgDurationMs     AS (CASE WHEN ExecCount = 0 THEN 0 ELSE TotalDurationMs   / ExecCount END) PERSISTED,
    MaxCpuMs          BIGINT,
    MaxDurationMs     BIGINT,
    MaxLogicalReads   BIGINT,
    DistinctHosts     INT,
    TopHostname       NVARCHAR(256),
    CollectDate       AS CAST(BucketStart AS DATE) PERSISTED,
    CONSTRAINT UK_SlowQuery_TimeBucket UNIQUE (BucketStart, SqlFingerprint),
    INDEX IX_SlowBucket_Hot NONCLUSTERED (CollectDate, TotalCpuMs DESC),
    INDEX IX_SlowBucket_TimeCost NONCLUSTERED (CollectDate, TotalDurationMs DESC)
);
GO

-- 3) 日聚合（给历史趋势）
IF OBJECT_ID('dbo.SlowSQL_DailySummary','U') IS NULL
CREATE TABLE dbo.SlowSQL_DailySummary(
    Id                BIGINT IDENTITY(1,1) PRIMARY KEY,
    StatDate          DATE          NOT NULL,
    SqlFingerprint    CHAR(64)      NOT NULL,
    SqlTextSample     NVARCHAR(800),
    DatabaseName      NVARCHAR(128),
    ExecCount         INT,
    TotalCpuMs        BIGINT,
    AvgCpuMs          BIGINT,
    MaxCpuMs          BIGINT,
    TotalDurationMs   BIGINT,
    AvgDurationMs     BIGINT,
    MaxDurationMs     BIGINT,
    TotalLogicalReads BIGINT,
    AvgLogicalReads   BIGINT,
    MaxLogicalReads   BIGINT,
    TotalPhysicalReads BIGINT,
    TotalWrites       BIGINT,
    LastLoginName     NVARCHAR(256),
    LastHostName      NVARCHAR(256),
    LastExecTime      DATETIME2,
    CONSTRAINT UK_SlowSQL_DailySummary UNIQUE (StatDate, SqlFingerprint)
);
GO

-- 4) SQL 文本字典（方便反查完整 SQL）
IF OBJECT_ID('dbo.SQL_Text_Dictionary','U') IS NULL
CREATE TABLE dbo.SQL_Text_Dictionary(
    SqlFingerprint CHAR(64)      PRIMARY KEY,
    SqlTextFull    NVARCHAR(MAX) NOT NULL,
    SqlTextShort   NVARCHAR(800) NOT NULL,
    FirstSeenUtc   DATETIME2     DEFAULT SYSUTCDATETIME()
);
GO

-- 5) ★ 聚合水位表（记录上次已完成聚合的最后一个桶，支持批量增量处理）
IF OBJECT_ID('dbo.SlowBucket_Watermark','U') IS NULL
BEGIN
    CREATE TABLE dbo.SlowBucket_Watermark(
        Id                  INT IDENTITY(1,1) PRIMARY KEY,
        LastProcessedBucket DATETIME2 NOT NULL,     -- 最近已完成聚合的桶起点（UTC）
        UpdatedUtc          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
    -- 初始化：从 24 小时前开始补数
    INSERT INTO dbo.SlowBucket_Watermark(LastProcessedBucket)
    VALUES (DATEADD(MINUTE, (DATEDIFF(MINUTE, 0, DATEADD(HOUR,-24,SYSUTCDATETIME())) / 5) * 5, CAST(0 AS DATETIME2)));
END
GO
```

---

## 第三步：从 XEL → SlowQueryLog（每 5 分钟执行一次）

```sql
USE [db_monitor]
GO
CREATE OR ALTER PROCEDURE dbo.usp_SlowSQL_IngestFromXEL
AS
BEGIN
    SET NOCOUNT ON;

    -- 只摄入新事件（上次标记之后到现在）
    DECLARE @highwater DATETIME2 = ISNULL(
        (SELECT MAX(EventTimeUtc) FROM dbo.SlowQueryLog),
        DATEADD(DAY, -1, SYSUTCDATETIME())
    );

    ;WITH raw AS (
        SELECT CAST(event_data AS XML) AS xe
        FROM sys.fn_xe_file_target_read_file(
            'C:\DBA\xel\HighResourceUsage_Tracking*.xel', NULL, NULL, NULL)
    ),
    parsed AS (
        SELECT
            xe.value('(event/@timestamp)[1]','datetime2')                              AS EventTimeUtc,
            LEFT(xe.value('(event/action[@name="sql_text"]/value)[1]','nvarchar(max)'), 4000) AS SqlText,
            xe.value('(event/action[@name="nt_username"]/value)[1]','nvarchar(256)')    AS ExecuteAccount,
            xe.value('(event/action[@name="client_hostname"]/value)[1]','nvarchar(256)')AS ClientHostname,
            xe.value('(event/action[@name="database_name"]/value)[1]','nvarchar(128)')  AS DatabaseName,
            xe.value('(event/data[@name="cpu_time"]/value)[1]','bigint')      / 1000    AS CpuMs,
            xe.value('(event/data[@name="duration"]/value)[1]','bigint')      / 1000    AS DurationMs,
            xe.value('(event/data[@name="logical_reads"]/value)[1]','bigint')           AS LogicalReads,
            xe.value('(event/data[@name="physical_reads"]/value)[1]','bigint')          AS PhysicalReads,
            xe.value('(event/data[@name="writes"]/value)[1]','bigint')                  AS Writes,
            xe.value('(event/data[@name="row_count"]/value)[1]','bigint')               AS RowCount_
        FROM raw
    )
    INSERT INTO dbo.SlowQueryLog(
        SqlText, ExecuteAccount, ClientHostname, DatabaseName,
        CpuMs, DurationMs, LogicalReads, PhysicalReads, Writes, RowCount_, EventTimeUtc)
    SELECT SqlText, ExecuteAccount, ClientHostname, DatabaseName,
           CpuMs, DurationMs, LogicalReads, PhysicalReads, Writes, RowCount_, EventTimeUtc
    FROM parsed
    WHERE SqlText IS NOT NULL
      AND EventTimeUtc > @highwater;

    -- 补全 SQL 字典
    INSERT INTO dbo.SQL_Text_Dictionary(SqlFingerprint, SqlTextFull, SqlTextShort)
    SELECT DISTINCT l.SqlFingerprint, l.SqlText, l.SqlTextShort
    FROM dbo.SlowQueryLog l
    WHERE NOT EXISTS (SELECT 1 FROM dbo.SQL_Text_Dictionary d WHERE d.SqlFingerprint = l.SqlFingerprint);
END
GO
```

---

## 第四步：★ 时间桶聚合（每小时执行，批量增量处理所有未聚合桶）

> 保持 **5 分钟桶粒度**（和 PerfMon 对齐不变），但 Job **每小时跑一次**，一次性补齐 `LastProcessedBucket` 到「当前时间 - 5min」之间所有未聚合的桶。
> 过程 **幂等可重跑**：水位回拨或重跑历史区间都安全。

```sql
CREATE OR ALTER PROCEDURE dbo.usp_SlowSQL_BuildBucket_5min
    @OverrideFromUtc DATETIME2 = NULL,   -- 可选：强制从某个桶开始重跑（手动补数用）
    @OverrideToUtc   DATETIME2 = NULL    -- 可选：强制处理到某个桶结束
AS
BEGIN
    SET NOCOUNT ON;

    -- 1) 计算本次处理区间 [from, to)
    DECLARE @from DATETIME2, @to DATETIME2;

    IF @OverrideFromUtc IS NOT NULL
        SET @from = @OverrideFromUtc;
    ELSE
        SELECT @from = DATEADD(MINUTE, 5, MAX(LastProcessedBucket))   -- 从水位的下一个桶开始
        FROM dbo.SlowBucket_Watermark;

    -- 上界：当前时间对齐到 5min 桶起点（这一桶还没满，不处理）
    DECLARE @nowBucket DATETIME2 =
        DATEADD(MINUTE, (DATEDIFF(MINUTE, 0, SYSUTCDATETIME()) / 5) * 5, CAST(0 AS DATETIME2));

    IF @OverrideToUtc IS NOT NULL
        SET @to = @OverrideToUtc;
    ELSE
        SET @to = @nowBucket;   -- 不包含当前未完成桶

    IF @from IS NULL OR @from >= @to
    BEGIN
        PRINT 'No new buckets to process.';
        RETURN;
    END

    -- 2) 批量按 5 分钟桶聚合（FLOOR 到 5min 对齐）
    ;WITH tagged AS (
        SELECT
            DATEADD(MINUTE, (DATEDIFF(MINUTE, 0, EventTimeUtc) / 5) * 5, CAST(0 AS DATETIME2)) AS BucketStart,
            SqlFingerprint, SqlTextShort, DatabaseName, ClientHostname,
            CpuMs, DurationMs, LogicalReads
        FROM dbo.SlowQueryLog
        WHERE EventTimeUtc >= @from AND EventTimeUtc < @to
    ),
    bucket AS (
        SELECT
            BucketStart,
            SqlFingerprint,
            MIN(SqlTextShort)                    AS SqlTextSample,
            MAX(DatabaseName)                    AS DatabaseName,
            COUNT(*)                             AS ExecCount,
            SUM(CpuMs)                           AS TotalCpuMs,
            SUM(DurationMs)                      AS TotalDurationMs,
            SUM(LogicalReads)                    AS TotalLogicalReads,
            MAX(CpuMs)                           AS MaxCpuMs,
            MAX(DurationMs)                      AS MaxDurationMs,
            MAX(LogicalReads)                    AS MaxLogicalReads,
            COUNT(DISTINCT ClientHostname)       AS DistinctHosts
        FROM tagged
        GROUP BY BucketStart, SqlFingerprint
    )
    MERGE dbo.SlowQuery_TimeBucket AS tgt
    USING bucket AS src
      ON tgt.BucketStart = src.BucketStart AND tgt.SqlFingerprint = src.SqlFingerprint
    WHEN MATCHED THEN UPDATE SET
        ExecCount         = src.ExecCount,
        TotalCpuMs        = src.TotalCpuMs,
        TotalDurationMs   = src.TotalDurationMs,
        TotalLogicalReads = src.TotalLogicalReads,
        MaxCpuMs          = src.MaxCpuMs,
        MaxDurationMs     = src.MaxDurationMs,
        MaxLogicalReads   = src.MaxLogicalReads,
        DistinctHosts     = src.DistinctHosts
    WHEN NOT MATCHED THEN
        INSERT (BucketStart, SqlFingerprint, SqlTextSample, DatabaseName,
                ExecCount, TotalCpuMs, TotalDurationMs, TotalLogicalReads,
                MaxCpuMs, MaxDurationMs, MaxLogicalReads, DistinctHosts)
        VALUES (src.BucketStart, src.SqlFingerprint, src.SqlTextSample, src.DatabaseName,
                src.ExecCount, src.TotalCpuMs, src.TotalDurationMs, src.TotalLogicalReads,
                src.MaxCpuMs, src.MaxDurationMs, src.MaxLogicalReads, src.DistinctHosts);

    -- 3) 推进水位（仅自动模式推进；手工 override 不更新水位，避免把正常水位冲掉）
    IF @OverrideFromUtc IS NULL AND @OverrideToUtc IS NULL
    BEGIN
        UPDATE dbo.SlowBucket_Watermark
        SET LastProcessedBucket = DATEADD(MINUTE, -5, @to),   -- @to 本身未处理，水位停在 @to 的前一桶
            UpdatedUtc = SYSUTCDATETIME();
    END
END
GO
```

---

## 第五步：日聚合（每日凌晨 02:00）

```sql
CREATE OR ALTER PROCEDURE dbo.usp_SlowSQL_DailySummary
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @d DATE = CAST(DATEADD(DAY, -1, GETDATE()) AS DATE);

    INSERT INTO dbo.SlowSQL_DailySummary
        (StatDate, SqlFingerprint, SqlTextSample, DatabaseName,
         ExecCount, TotalCpuMs, AvgCpuMs, MaxCpuMs,
         TotalDurationMs, AvgDurationMs, MaxDurationMs,
         TotalLogicalReads, AvgLogicalReads, MaxLogicalReads,
         TotalPhysicalReads, TotalWrites,
         LastLoginName, LastHostName, LastExecTime)
    SELECT
        @d, SqlFingerprint, MAX(SqlTextShort), MAX(DatabaseName),
        COUNT(*),           SUM(CpuMs),       AVG(CpuMs),       MAX(CpuMs),
        SUM(DurationMs),    AVG(DurationMs),  MAX(DurationMs),
        SUM(LogicalReads),  AVG(LogicalReads),MAX(LogicalReads),
        SUM(PhysicalReads), SUM(Writes),
        MAX(ExecuteAccount),MAX(ClientHostname), MAX(EventTimeUtc)
    FROM dbo.SlowQueryLog
    WHERE CollectDate = @d
    GROUP BY SqlFingerprint;

    -- 清理 7 天前原始
    DELETE FROM dbo.SlowQueryLog WHERE CollectDate < DATEADD(DAY, -7, GETDATE());
END
GO
```

---

## 第六步：CSV 导出（每日 03:00）

保存为 `C:\DBA\scripts\Export-SlowSQL-Csv.ps1`。

```powershell
param(
    [string]$StatDate = $(Get-Date -Format "yyyy-MM-dd"),
    [string]$RootDir  = "D:\DBA\insagent-data\slow_sql"
)

$Server   = "YOUR_MONITOR_DB_HOST"
$Database = "db_monitor"
$OutputDir = Join-Path $RootDir $StatDate
if (!(Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

# --- 5 分钟桶（★ 关键：和 perfmon_5min 能 JOIN） ---
$q1 = @"
SELECT BucketStart, SqlFingerprint, SqlTextSample, DatabaseName,
       ExecCount, TotalCpuMs, TotalDurationMs, TotalLogicalReads,
       AvgCpuMs, AvgDurationMs, MaxCpuMs, MaxDurationMs, MaxLogicalReads,
       DistinctHosts
FROM dbo.SlowQuery_TimeBucket
WHERE CollectDate = '$StatDate'
ORDER BY BucketStart, TotalCpuMs DESC
"@
Invoke-Sqlcmd -ServerInstance $Server -Database $Database -Query $q1 -QueryTimeout 0 |
    Export-Csv -Path (Join-Path $OutputDir "slowsql_5min_$($StatDate).csv") -NoTypeInformation -Encoding UTF8

# --- 日汇总 ---
$q2 = @"
SELECT StatDate, SqlFingerprint, SqlTextSample, DatabaseName,
       ExecCount, TotalCpuMs, AvgCpuMs, MaxCpuMs,
       TotalDurationMs, AvgDurationMs, MaxDurationMs,
       TotalLogicalReads, AvgLogicalReads, MaxLogicalReads,
       TotalPhysicalReads, TotalWrites,
       LastLoginName, LastHostName, LastExecTime
FROM dbo.SlowSQL_DailySummary
WHERE StatDate = '$StatDate'
ORDER BY TotalCpuMs DESC
"@
Invoke-Sqlcmd -ServerInstance $Server -Database $Database -Query $q2 -QueryTimeout 0 |
    Export-Csv -Path (Join-Path $OutputDir "slowsql_daily_$($StatDate).csv") -NoTypeInformation -Encoding UTF8

Write-Host "DONE -> $OutputDir"
```

---

## 第七步：调度总览

| Job | 频率 | 说明 |
|---|---|---|
| XEvent Session `HighResourceUsage_Tracking` | 开机自启（`STARTUP_STATE = ON`） | SQL Server 原生写入 `.xel`，不依赖 Job |
| `usp_SlowSQL_IngestFromXEL` | **每小时 `:55`** | 从 `.xel` 读入 `SlowQueryLog`；`.xel` 2GB 滚动缓冲足够容纳 1 小时事件 |
| `usp_SlowSQL_BuildBucket_5min` | **每小时 `:58`**（紧跟上一步） | 批量增量补齐所有未聚合的 5 分钟桶 |
| `usp_SlowSQL_DailySummary` | 每日 02:10 | 日聚合 + 清理 7 天前原始日志 |
| `Export-SlowSQL-Csv.ps1` | 每日 03:00 | 导出 CSV 给 agent |

> **降频理由**：5 分钟桶只是 **分析粒度**（与 PerfMon 对齐），不是采集频率。原始事件由 Extended Events 自动持久化到 `.xel`，Ingest/Bucket 改小时级后：
> - SQL Agent 调度次数从 288 次/天 → 24 次/天
> - 分析能力不变（桶数据依然是完整的 5 分钟粒度）
> - 日内故障排查最长延迟 ≤ 1 小时；若需实时排查可手动执行 `EXEC dbo.usp_SlowSQL_IngestFromXEL; EXEC dbo.usp_SlowSQL_BuildBucket_5min;`

### 手动补数示例

```sql
-- 场景：发现 2026-05-08 14:00~15:00 区间桶数据缺失，手动重跑
EXEC dbo.usp_SlowSQL_BuildBucket_5min
    @OverrideFromUtc = '2026-05-08 14:00:00',
    @OverrideToUtc   = '2026-05-08 15:00:00';
-- 注意：@Override 模式不会推进水位，用于纯补数
```

---

## 第八步：与 PerfMon 时间桶对齐分析（示例）

**场景**：PerfMon 显示昨天 14:25 CPU 飙到 95%，想查原因。

```sql
-- 在监控库直接跑（本地自检用，agent 侧由 skill 做）
SELECT TOP 20
       b.BucketStart, b.SqlTextSample, b.ExecCount,
       b.TotalCpuMs, b.TotalDurationMs, b.TotalLogicalReads,
       b.AvgDurationMs, b.DatabaseName
FROM dbo.SlowQuery_TimeBucket b
WHERE b.BucketStart = '2026-05-08 14:25:00'
ORDER BY b.TotalCpuMs DESC;
```

同一 `BucketStart` 可以和 `PerfMon_TimeBucket` 直接 JOIN，立刻看到「那 5 分钟里 CPU 是谁烧掉的」。

---

## 交付给 agent

CSV 文件落地路径：

```
D:\DBA\insagent-data\slow_sql\YYYY-MM-DD\
    ├─ slowsql_5min_YYYY-MM-DD.csv     # 关联分析主数据
    └─ slowsql_daily_YYYY-MM-DD.csv    # 长期趋势
```

拷贝到 agent 的 `$DATA_ROOT/slow_sql/YYYY-MM-DD/`。
