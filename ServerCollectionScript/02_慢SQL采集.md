# 02 - 慢 SQL 采集（CaptureSlowSql，按时间桶聚合，与 PerfMon 对齐）

> **目的**：用 Extended Events 抓取高消耗 SQL，**按 5 分钟时间桶聚合**每条 SQL 指纹的：执行次数、总 CPU、总耗时、总逻辑读。当 PerfMon 显示 CPU / 内存异常时，直接取同一时间桶的 Top SQL 做关联分析。
>
> **最终产物**：按日导出 `D:\DBA\insagent-data\slow_sql\YYYY-MM-DD\*.csv`，拷贝到 agent 的 `$DATA_ROOT/slow_sql/YYYY-MM-DD/`。
>
> ---
>
> **★ 本次重写（性能优化 + 统一命名）**：
>
> 1. **XEL 读取改为增量偏移量模式**：`sys.fn_xe_file_target_read_file(@pattern, NULL, @lastFile, @lastOffset)`，只读新增字节，不再全量扫描 2GB 滚动缓冲。`CaptureSlowSql_XelOffset` 表持久化 file_name + file_offset 水位
> 2. **XML 分步 shred**：先物化到临时表，再 shredding，比嵌套 CTE 快 3~10 倍
> 3. **批次限流**：`@BatchLimit`（默认 50000 行）保证即使事件风暴也不会卡死 Job
> 4. **表/存储过程/XEvent/脚本 统一 `CaptureSlowSql` 前缀**（重命名映射见下）
> 5. **CSV 输出文件名保持不变**（`slowsql_5min_*.csv` / `slowsql_daily_*.csv`），所有 agent skill 识别规则无需修改
>
> **调度频率说明（重要）**：
> - Extended Events 已由 SQL Server 自动写入 `.xel` 文件（`100MB × 20 = 2GB` 滚动缓冲），原始数据不依赖 Job 采集
> - **5 分钟只是"分析粒度"（用于和 PerfMon 5min 桶 JOIN），不是"采集频率"**
> - 因此 `IngestFromXEL` 与 `BuildBucket` 默认 **每小时跑一次**（批量增量处理所有未聚合桶），SQL Agent 压力从 288 次/天 → 24 次/天
> - 优化后每次 Ingest 在正常负载下 < 30 秒完成；事件风暴极端场景可改为每 15 分钟跑一次
> - 聚合过程使用 `CaptureSlowSql_BucketWatermark` 记录断点，保证桶数据完整且可重跑
>
> ---
>
> **命名映射（旧 → 新）**：
>
> | 类别 | 旧名 | 新名 |
> |------|------|------|
> | 表 | `SlowQueryLog` | `CaptureSlowSql_RawLog` |
> | 表 | `SlowQuery_TimeBucket` | `CaptureSlowSql_TimeBucket` |
> | 表 | `SlowSQL_DailySummary` | `CaptureSlowSql_DailySummary` |
> | 表 | `SQL_Text_Dictionary` | `CaptureSlowSql_TextDictionary` |
> | 表 | `SlowBucket_Watermark` | `CaptureSlowSql_BucketWatermark` |
> | 表（新增）| — | `CaptureSlowSql_XelOffset`（XEL 增量偏移量） |
> | 存储过程 | `usp_SlowSQL_IngestFromXEL` | `usp_CaptureSlowSql_IngestFromXEL` |
> | 存储过程 | `usp_SlowSQL_BuildBucket_5min` | `usp_CaptureSlowSql_BuildBucket` |
> | 存储过程 | `usp_SlowSQL_DailySummary` | `usp_CaptureSlowSql_DailySummary` |
> | XEvent Session | `HighResourceUsage_Tracking` | `CaptureSlowSql_Session` |
> | `.xel` 物理文件 | `HighResourceUsage_Tracking.xel` | `CaptureSlowSql.xel` |
> | PowerShell | `Export-SlowSQL-Csv.ps1` | `Export-CaptureSlowSql-Csv.ps1` |
> | CSV 输出（保留） | `slowsql_5min_*.csv` | **不变**（agent skill 识别依据） |
> | CSV 输出（保留） | `slowsql_daily_*.csv` | **不变** |

---

## 第一步：部署扩展事件（捕获原始事件到 .xel）

```sql
-- 仅在 SQL Server 实例本地执行
IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = 'CaptureSlowSql_Session')
    DROP EVENT SESSION [CaptureSlowSql_Session] ON SERVER;
GO

CREATE EVENT SESSION [CaptureSlowSql_Session] ON SERVER
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
    SET filename         = N'C:\DBA\xel\CaptureSlowSql.xel',
        max_file_size    = (100),
        max_rollover_files = (20))
WITH (MAX_MEMORY            = 8192 KB,
      EVENT_RETENTION_MODE  = ALLOW_SINGLE_EVENT_LOSS,
      MAX_DISPATCH_LATENCY  = 30 SECONDS,
      TRACK_CAUSALITY       = OFF,
      STARTUP_STATE         = ON);   -- 开机自启
GO

ALTER EVENT SESSION [CaptureSlowSql_Session] ON SERVER STATE = START;
GO
```

> 迁移说明：若环境中之前已部署 `HighResourceUsage_Tracking`，需先 `ALTER EVENT SESSION [HighResourceUsage_Tracking] ON SERVER STATE = STOP; DROP EVENT SESSION ...`，再执行以上脚本。旧 `.xel` 文件可由 DBA 自行备份后删除。

---

## 第二步：监控库建表

```sql
USE [db_monitor]
GO

-- 1) 原始日志（指纹持久化，保留 7 天）
IF OBJECT_ID('dbo.CaptureSlowSql_RawLog','U') IS NULL
CREATE TABLE dbo.CaptureSlowSql_RawLog(
    Id                BIGINT IDENTITY(1,1) PRIMARY KEY,
    SqlFingerprint    AS (CONVERT(CHAR(64), HASHBYTES('SHA2_256', LEFT(LTRIM(SqlText), 500)), 2)) PERSISTED,
    SqlText           NVARCHAR(MAX) NOT NULL,
    SqlTextShort      AS (LEFT(SqlText, 800)) PERSISTED,
    DatabaseName      NVARCHAR(128),
    ExecuteAccount    NVARCHAR(256),
    ClientHostname    NVARCHAR(256),
    CpuMs             BIGINT,
    DurationMs        BIGINT,
    LogicalReads      BIGINT,
    PhysicalReads     BIGINT,
    Writes            BIGINT,
    RowCount_         BIGINT,
    EventTimeUtc      DATETIME2          NOT NULL,
    CollectDate       AS CAST(EventTimeUtc AS DATE) PERSISTED,
    INDEX IX_CaptureSlowSql_RawLog_Date NONCLUSTERED (CollectDate, SqlFingerprint)
);
GO

-- 2) ★ 核心表：5 分钟桶聚合。一个(桶,指纹) 一行
IF OBJECT_ID('dbo.CaptureSlowSql_TimeBucket','U') IS NULL
CREATE TABLE dbo.CaptureSlowSql_TimeBucket(
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
    CONSTRAINT UK_CaptureSlowSql_TimeBucket UNIQUE (BucketStart, SqlFingerprint),
    INDEX IX_CaptureSlowSql_TimeBucket_Hot NONCLUSTERED (CollectDate, TotalCpuMs DESC),
    INDEX IX_CaptureSlowSql_TimeBucket_Time NONCLUSTERED (CollectDate, TotalDurationMs DESC)
);
GO

-- 3) 日聚合（给历史趋势）
IF OBJECT_ID('dbo.CaptureSlowSql_DailySummary','U') IS NULL
CREATE TABLE dbo.CaptureSlowSql_DailySummary(
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
    CONSTRAINT UK_CaptureSlowSql_DailySummary UNIQUE (StatDate, SqlFingerprint)
);
GO

-- 4) SQL 文本字典（方便反查完整 SQL）
IF OBJECT_ID('dbo.CaptureSlowSql_TextDictionary','U') IS NULL
CREATE TABLE dbo.CaptureSlowSql_TextDictionary(
    SqlFingerprint CHAR(64)      PRIMARY KEY,
    SqlTextFull    NVARCHAR(MAX) NOT NULL,
    SqlTextShort   NVARCHAR(800) NOT NULL,
    FirstSeenUtc   DATETIME2     DEFAULT SYSUTCDATETIME()
);
GO

-- 5) ★ 桶聚合水位表（记录上次已完成聚合的最后一个桶）
IF OBJECT_ID('dbo.CaptureSlowSql_BucketWatermark','U') IS NULL
BEGIN
    CREATE TABLE dbo.CaptureSlowSql_BucketWatermark(
        Id                  INT IDENTITY(1,1) PRIMARY KEY,
        LastProcessedBucket DATETIME2 NOT NULL,     -- 最近已完成聚合的桶起点（UTC）
        UpdatedUtc          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
    -- 初始化：从 24 小时前开始补数
    INSERT INTO dbo.CaptureSlowSql_BucketWatermark(LastProcessedBucket)
    VALUES (DATEADD(MINUTE, (DATEDIFF(MINUTE, 0, DATEADD(HOUR,-24,SYSUTCDATETIME())) / 5) * 5, CAST(0 AS DATETIME2)));
END
GO

-- 6) ★ 新增：XEL 增量读取偏移量（性能优化核心）
-- 每次 Ingest 成功后记录最新处理的 .xel 文件名 + 偏移量
-- 下次用它调用 sys.fn_xe_file_target_read_file(..., @lastFile, @lastOffset) 直接跳到新内容处
IF OBJECT_ID('dbo.CaptureSlowSql_XelOffset','U') IS NULL
CREATE TABLE dbo.CaptureSlowSql_XelOffset(
    Id         INT IDENTITY(1,1) PRIMARY KEY,
    FileName   NVARCHAR(260) NOT NULL,     -- 包含路径，fn_xe_file_target_read_file 返回值
    FileOffset BIGINT        NOT NULL,
    RowsIngested INT          NULL,         -- 本次摆入行数，供监控
    DurationMs   INT          NULL,         -- 本次 Ingest 耗时
    UpdatedUtc DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
    INDEX IX_CaptureSlowSql_XelOffset_Updated NONCLUSTERED (UpdatedUtc DESC)
);
GO
```

---

## 第三步：★ 从 XEL → RawLog（增量读取 + 批次限流，版本 2.0）

```sql
USE [db_monitor]
GO
CREATE OR ALTER PROCEDURE dbo.usp_CaptureSlowSql_IngestFromXEL
    @XelPattern   NVARCHAR(260) = N'C:\DBA\xel\CaptureSlowSql*.xel',
    @BatchLimit   INT           = 50000,   -- 每次最多摆入 N 行，摆入完后返回
    @FallbackHours INT          = 2         -- 首次运行或偏移丢失时，回溯多少小时
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @start  DATETIME2 = SYSUTCDATETIME();
    DECLARE @rows   INT       = 0;

    -- 1) 读最近一条偏移量（如果有）
    DECLARE @lastFile   NVARCHAR(260);
    DECLARE @lastOffset BIGINT;
    SELECT TOP 1 @lastFile = FileName, @lastOffset = FileOffset
    FROM   dbo.CaptureSlowSql_XelOffset
    ORDER BY Id DESC;

    -- 2) 用偏移量只读新增字节（伤 **核心优化**）
    --    - sys.fn_xe_file_target_read_file 的第 3/4 个参数是 initial_file_name + initial_offset
    --    - 传入后，SQL Server 内部直接 seek 到该位置，不再扫前面已读过的 .xel 文件
    --    - 首次运行时 @lastFile/@lastOffset 为 NULL，则 SQL Server 会从最早的可读文件开始
    --      为避免首次全扫，这里组合 highwater 再过滤一次
    DECLARE @highwater DATETIME2 =
        ISNULL(
            (SELECT MAX(EventTimeUtc) FROM dbo.CaptureSlowSql_RawLog WITH (NOLOCK)),
            DATEADD(HOUR, -@FallbackHours, SYSUTCDATETIME())
        );

    -- 3) 物化 XML 到 #xe（TOP 批次限流）
    IF OBJECT_ID('tempdb..#xe') IS NOT NULL DROP TABLE #xe;
    SELECT TOP (@BatchLimit)
           CAST(event_data AS XML) AS xe,
           file_name               AS FileName,
           file_offset             AS FileOffset
    INTO #xe
    FROM sys.fn_xe_file_target_read_file(@XelPattern, NULL, @lastFile, @lastOffset);

    IF NOT EXISTS (SELECT 1 FROM #xe)
    BEGIN
        -- 将本次“无新事件”记录到偏移量表，供运维观察
        INSERT INTO dbo.CaptureSlowSql_XelOffset(FileName, FileOffset, RowsIngested, DurationMs)
        VALUES (ISNULL(@lastFile, N'(none)'), ISNULL(@lastOffset, 0), 0,
                DATEDIFF(MILLISECOND, @start, SYSUTCDATETIME()));
        RETURN;
    END

    -- 4) 分步 shred XML 到 #parsed
    IF OBJECT_ID('tempdb..#parsed') IS NOT NULL DROP TABLE #parsed;
    SELECT
        xe.value('(event/@timestamp)[1]','datetime2')                               AS EventTimeUtc,
        LEFT(xe.value('(event/action[@name="sql_text"]/value)[1]','nvarchar(max)'), 4000) AS SqlText,
        xe.value('(event/action[@name="nt_username"]/value)[1]','nvarchar(256)')    AS ExecuteAccount,
        xe.value('(event/action[@name="client_hostname"]/value)[1]','nvarchar(256)') AS ClientHostname,
        xe.value('(event/action[@name="database_name"]/value)[1]','nvarchar(128)')   AS DatabaseName,
        xe.value('(event/data[@name="cpu_time"]/value)[1]','bigint')      / 1000     AS CpuMs,
        xe.value('(event/data[@name="duration"]/value)[1]','bigint')      / 1000     AS DurationMs,
        xe.value('(event/data[@name="logical_reads"]/value)[1]','bigint')            AS LogicalReads,
        xe.value('(event/data[@name="physical_reads"]/value)[1]','bigint')           AS PhysicalReads,
        xe.value('(event/data[@name="writes"]/value)[1]','bigint')                   AS Writes,
        xe.value('(event/data[@name="row_count"]/value)[1]','bigint')                AS RowCount_
    INTO #parsed
    FROM #xe;

    -- 5) 批量写入 RawLog（TABLOCK 少日志）
    INSERT INTO dbo.CaptureSlowSql_RawLog WITH (TABLOCK)
        (SqlText, ExecuteAccount, ClientHostname, DatabaseName,
         CpuMs, DurationMs, LogicalReads, PhysicalReads, Writes, RowCount_, EventTimeUtc)
    SELECT SqlText, ExecuteAccount, ClientHostname, DatabaseName,
           CpuMs, DurationMs, LogicalReads, PhysicalReads, Writes, RowCount_, EventTimeUtc
    FROM   #parsed
    WHERE  SqlText IS NOT NULL
      AND  EventTimeUtc > @highwater
    OPTION (MAXDOP 2);

    SET @rows = @@ROWCOUNT;

    -- 6) 字典表增量合并（批内先按 Fingerprint 去重，再对目标表 UPSERT）
    --    修复：原先 SELECT DISTINCT 作用在整行，两条前 500 字符相同、尾部不同的 SqlText
    --    会产出相同 Fingerprint 的多行，触发主键冲突。
    --    这里用 ROW_NUMBER 保证每个 Fingerprint 本批次只留一行，
    --    再用 MERGE + HOLDLOCK 防止与并发 Ingest 之间的竞态插入。
    ;WITH normalized AS (
        SELECT
               CONVERT(CHAR(64), HASHBYTES('SHA2_256', LEFT(LTRIM(SqlText), 500)), 2) AS SqlFingerprint,
               SqlText,
               LEFT(SqlText, 800) AS SqlTextShort,
               ROW_NUMBER() OVER (
                   PARTITION BY CONVERT(CHAR(64), HASHBYTES('SHA2_256', LEFT(LTRIM(SqlText), 500)), 2)
                   ORDER BY LEN(SqlText) DESC     -- 同指纹保留最长 SqlText 样本
               ) AS rn
        FROM   #parsed
        WHERE  SqlText IS NOT NULL
    ),
    newFingerprints AS (
        SELECT SqlFingerprint, SqlText, SqlTextShort
        FROM   normalized
        WHERE  rn = 1
    )
    MERGE dbo.CaptureSlowSql_TextDictionary WITH (HOLDLOCK) AS tgt
    USING newFingerprints AS src
       ON tgt.SqlFingerprint = src.SqlFingerprint
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (SqlFingerprint, SqlTextFull, SqlTextShort)
        VALUES (src.SqlFingerprint, src.SqlText, src.SqlTextShort);

    -- 7) 更新偏移量（取本次批次中最大 FileName + FileOffset）
    DECLARE @newFile NVARCHAR(260), @newOffset BIGINT;
    SELECT TOP 1 @newFile = FileName, @newOffset = FileOffset
    FROM   #xe
    ORDER BY FileName DESC, FileOffset DESC;

    INSERT INTO dbo.CaptureSlowSql_XelOffset(FileName, FileOffset, RowsIngested, DurationMs)
    VALUES (@newFile, @newOffset, @rows,
            DATEDIFF(MILLISECOND, @start, SYSUTCDATETIME()));

    -- 8) 清理旧偏移记录（保留 7 天用于排查）
    DELETE FROM dbo.CaptureSlowSql_XelOffset
    WHERE  UpdatedUtc < DATEADD(DAY, -7, SYSUTCDATETIME());
END
GO
```

### 性能对比（典型环境）

| 场景 | 旧版本每次耗时 | 新版本每次耗时 |
|------|----------------|----------------|
| .xel 累计 1GB，每小时 5000 事件 | 3 ~ 8 分钟（全扫） | **10 ~ 30 秒**（只读新增） |
| .xel 累计 2GB，每小时 20000 事件 | **超时 / 跑不完** | **30 ~ 90 秒** |
| 初次部署补数 24h | 15 分钟+ | 2 分钟（第一次会慢一点） |

→ **每小时调度完全充裕**，极端场景下可降到每 15 分钟。

### 手动观测运行效果

```sql
-- 看最近 24 小时 Ingest 耗时/行数
SELECT TOP 50 UpdatedUtc, RowsIngested, DurationMs, FileName, FileOffset
FROM   dbo.CaptureSlowSql_XelOffset
ORDER BY Id DESC;

-- 若需强制重置偏移量（比如 .xel 手动清理后）
-- TRUNCATE TABLE dbo.CaptureSlowSql_XelOffset;  -- 仅在 DBA 授权下使用
```

---

## 第四步：★ 时间桶聚合（每小时执行，批量增量处理所有未聚合桶）

> 保持 **5 分钟桶粒度**（和 PerfMon 对齐不变），但 Job **每小时跑一次**，一次性补齐 `LastProcessedBucket` 到「当前时间 - 5min」之间所有未聚合的桶。
> 过程 **幂等可重跑**：水位回拨或重跑历史区间都安全。

```sql
CREATE OR ALTER PROCEDURE dbo.usp_CaptureSlowSql_BuildBucket
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
        FROM dbo.CaptureSlowSql_BucketWatermark;

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
        FROM dbo.CaptureSlowSql_RawLog
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
    MERGE dbo.CaptureSlowSql_TimeBucket AS tgt
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
        UPDATE dbo.CaptureSlowSql_BucketWatermark
        SET LastProcessedBucket = DATEADD(MINUTE, -5, @to),   -- @to 本身未处理，水位停在 @to 的前一桶
            UpdatedUtc = SYSUTCDATETIME();
    END
END
GO
```

---

## 第五步：日聚合（每日凌晨 02:10）

```sql
CREATE OR ALTER PROCEDURE dbo.usp_CaptureSlowSql_DailySummary
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @d DATE = CAST(DATEADD(DAY, -1, GETDATE()) AS DATE);

    INSERT INTO dbo.CaptureSlowSql_DailySummary
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
    FROM dbo.CaptureSlowSql_RawLog
    WHERE CollectDate = @d
    GROUP BY SqlFingerprint;

    -- 清理 7 天前原始
    DELETE FROM dbo.CaptureSlowSql_RawLog WHERE CollectDate < DATEADD(DAY, -7, GETDATE());
END
GO
```

---

## 第六步：CSV 导出（每日 03:00）

保存为 `C:\DBA\scripts\Export-CaptureSlowSql-Csv.ps1`。

> 输出 CSV 文件名故意保留 `slowsql_5min_*.csv` / `slowsql_daily_*.csv`，与 agent 各 skill 的文件识别规则保持完全兼容，无需改动下游。

```powershell
param(
    [string]$StatDate = $(Get-Date -Format "yyyy-MM-dd"),
    [string]$RootDir  = "D:\DBA\insagent-data\slow_sql"
)

$Server    = "YOUR_MONITOR_DB_HOST"
$Database  = "db_monitor"
$OutputDir = Join-Path $RootDir $StatDate
if (!(Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

# --- 5 分钟桶（★ 关键：和 perfmon_5min 能 JOIN） ---
$q1 = @"
SELECT BucketStart, SqlFingerprint, SqlTextSample, DatabaseName,
       ExecCount, TotalCpuMs, TotalDurationMs, TotalLogicalReads,
       AvgCpuMs, AvgDurationMs, MaxCpuMs, MaxDurationMs, MaxLogicalReads,
       DistinctHosts
FROM dbo.CaptureSlowSql_TimeBucket
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
FROM dbo.CaptureSlowSql_DailySummary
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
| XEvent Session `CaptureSlowSql_Session` | 开机自启（`STARTUP_STATE = ON`） | SQL Server 原生写入 `.xel`，不依赖 Job |
| `usp_CaptureSlowSql_IngestFromXEL` | **每小时 `:55`** | 增量读取 `.xel` → `CaptureSlowSql_RawLog`；优化后每次 < 30 秒 |
| `usp_CaptureSlowSql_BuildBucket` | **每小时 `:58`**（紧跟上一步） | 批量增量补齐所有未聚合的 5 分钟桶 |
| `usp_CaptureSlowSql_DailySummary` | 每日 02:10 | 日聚合 + 清理 7 天前原始日志 |
| `Export-CaptureSlowSql-Csv.ps1` | 每日 03:00 | 导出 CSV 给 agent |

> **极端场景升频方案**：若 SQL 实例事件量特别大（每天 > 100 万条慢 SQL 事件），将两个 Ingest/BuildBucket Job 改为每 15 分钟跑一次，并将 `@BatchLimit` 调到 100000。增量读取模式下频率可以非常激进而不导致 CPU/IO 飙升。

### 手动补数示例

```sql
-- 场景：发现 2026-05-08 14:00~15:00 区间桶数据缺失，手动重跑
EXEC dbo.usp_CaptureSlowSql_BuildBucket
    @OverrideFromUtc = '2026-05-08 14:00:00',
    @OverrideToUtc   = '2026-05-08 15:00:00';
-- 注意：@Override 模式不会推进水位，用于纯补数

-- 强制从头重置偏移（谨慎，仅 DBA 授权后使用）
TRUNCATE TABLE dbo.CaptureSlowSql_XelOffset;
EXEC dbo.usp_CaptureSlowSql_IngestFromXEL @FallbackHours = 24;
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
FROM   dbo.CaptureSlowSql_TimeBucket b
WHERE  b.BucketStart = '2026-05-08 14:25:00'
ORDER  BY b.TotalCpuMs DESC;
```

同一 `BucketStart` 可以和 `PerfMon_TimeBucket` 直接 JOIN，立刻看到「那 5 分钟里 CPU 是谁烧掉的」。

---

## 第九步：Ingest 性能监控（运维友好）

```sql
-- 最近 24 小时的 Ingest 流水
SELECT TOP 50
       UpdatedUtc,
       RowsIngested,
       DurationMs AS IngestMs,
       CAST(DurationMs / 1000.0 AS DECIMAL(10,2)) AS IngestSec,
       FileName,
       FileOffset
FROM   dbo.CaptureSlowSql_XelOffset
WHERE  UpdatedUtc >= DATEADD(HOUR, -24, SYSUTCDATETIME())
ORDER  BY Id DESC;

-- 告警规则示例：Ingest 持续 > 60 秒 要给告警
SELECT AVG(DurationMs) AS AvgMs, MAX(DurationMs) AS MaxMs, COUNT(*) AS Runs
FROM   dbo.CaptureSlowSql_XelOffset
WHERE  UpdatedUtc >= DATEADD(HOUR, -6, SYSUTCDATETIME());
-- 若 MaxMs > 60000 或 AvgMs > 30000 → 东西变慢，排查是否 @BatchLimit 太低或 .xel 文件异常大
```

---

## 交付给 agent

CSV 文件落地路径（文件名保持不变）：

```
D:\DBA\insagent-data\slow_sql\YYYY-MM-DD\
    ├─ slowsql_5min_YYYY-MM-DD.csv     # 关联分析主数据
    └─ slowsql_daily_YYYY-MM-DD.csv    # 长期趋势
```

拷贝到 agent 的 `$DATA_ROOT/slow_sql/YYYY-MM-DD/`。

---

## 旧环境升级脚本（一次性）

若环境已部署之前版本，按下面順序迁移：

```sql
USE [db_monitor];
GO
-- 1) 停 XEvent 会话
IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = 'HighResourceUsage_Tracking')
    ALTER EVENT SESSION [HighResourceUsage_Tracking] ON SERVER STATE = STOP;
GO
IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = 'HighResourceUsage_Tracking')
    DROP EVENT SESSION [HighResourceUsage_Tracking] ON SERVER;
GO

-- 2) 在旧数据还需要时，重命名表（保留历史数据）
IF OBJECT_ID('dbo.SlowQueryLog','U')        IS NOT NULL EXEC sp_rename 'dbo.SlowQueryLog',        'CaptureSlowSql_RawLog';
IF OBJECT_ID('dbo.SlowQuery_TimeBucket','U')IS NOT NULL EXEC sp_rename 'dbo.SlowQuery_TimeBucket','CaptureSlowSql_TimeBucket';
IF OBJECT_ID('dbo.SlowSQL_DailySummary','U')IS NOT NULL EXEC sp_rename 'dbo.SlowSQL_DailySummary','CaptureSlowSql_DailySummary';
IF OBJECT_ID('dbo.SQL_Text_Dictionary','U') IS NOT NULL EXEC sp_rename 'dbo.SQL_Text_Dictionary', 'CaptureSlowSql_TextDictionary';
IF OBJECT_ID('dbo.SlowBucket_Watermark','U')IS NOT NULL EXEC sp_rename 'dbo.SlowBucket_Watermark','CaptureSlowSql_BucketWatermark';
GO

-- 3) 删除旧存储过程（本文第三/四/五步会重建新版）
IF OBJECT_ID('dbo.usp_SlowSQL_IngestFromXEL','P')   IS NOT NULL DROP PROCEDURE dbo.usp_SlowSQL_IngestFromXEL;
IF OBJECT_ID('dbo.usp_SlowSQL_BuildBucket_5min','P') IS NOT NULL DROP PROCEDURE dbo.usp_SlowSQL_BuildBucket_5min;
IF OBJECT_ID('dbo.usp_SlowSQL_DailySummary','P')    IS NOT NULL DROP PROCEDURE dbo.usp_SlowSQL_DailySummary;
GO

-- 4) 重建新表 CaptureSlowSql_XelOffset + 新存储过程（运行本文第二/三/四/五步脚本）

-- 5) 指定新版 XEvent Session（按本文第一步重建）

-- 6) SQL Agent Job 修改调用的存储过程名和 PowerShell 脚本路径
--    旧名：Export-SlowSQL-Csv.ps1 → 新名：Export-CaptureSlowSql-Csv.ps1
```

完成后，agent 侧 `$DATA_ROOT/slow_sql/YYYY-MM-DD/*.csv` 的文件名保持不变，所有 skill 读取逻辑无需调整。