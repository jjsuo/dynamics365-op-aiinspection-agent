# 01 - SQL Server 性能指标采集（PerfMon）

> **目的**：在每台 SQL / 应用服务器上持续采集 CPU / 内存 / IO / SQL 内核计数器，落地到监控库。
> **最终产物**：按日导出 `D:\DBA\insagent-data\server_per_sql\YYYY-MM-DD\*.csv`，由人工拷回 agent 的 `$DATA_ROOT/server_per_sql/YYYY-MM-DD/`。
> **关键优化点（本次重写）**：
> - 新增 **5 分钟时间桶** 聚合表 `PerfMon_TimeBucket`，方便与慢 SQL / 阻塞 / IIS 做时间轴对齐
> - 采样间隔从 60 秒降到 **30 秒**（生产依然安全），保证 5 分钟桶内 ≥ 10 个样本
> - CSV 导出脚本按日期分文件夹，命名与 agent 端目录约定一致
> - 明确区分：**Raw（原始点）** / **Bucket（5 分钟聚合）** / **DailySummary（日聚合）**

---

## 第一步：建表（监控库执行一次）

```sql
USE [db_monitor]
GO

-- 1) 原始点：60 秒级细节（保留 7 天滚动）
IF OBJECT_ID('dbo.PerfMon_Raw','U') IS NULL
CREATE TABLE dbo.PerfMon_Raw(
    Id           BIGINT IDENTITY(1,1) PRIMARY KEY,
    ServerName   NVARCHAR(128) NOT NULL,
    CounterPath  NVARCHAR(512) NOT NULL,
    CounterValue FLOAT         NULL,
    CollectTime  DATETIME2     NOT NULL,
    INDEX IX_PerfMon_Raw_Time NONCLUSTERED (CollectTime, ServerName)
);
GO

-- 2) 5 分钟时间桶：用于跨源关联分析（PerfMon × 慢SQL × 阻塞）
IF OBJECT_ID('dbo.PerfMon_TimeBucket','U') IS NULL
CREATE TABLE dbo.PerfMon_TimeBucket(
    Id           BIGINT IDENTITY(1,1) PRIMARY KEY,
    ServerName   NVARCHAR(128) NOT NULL,
    CounterPath  NVARCHAR(512) NOT NULL,
    BucketStart  DATETIME2     NOT NULL,   -- 桶起点（整 5 分钟）
    AvgValue     FLOAT         NULL,
    MaxValue     FLOAT         NULL,
    MinValue     FLOAT         NULL,
    P95Value     FLOAT         NULL,
    Samples      INT           NULL,
    CollectDate  AS CAST(BucketStart AS DATE) PERSISTED,
    INDEX IX_PerfMon_Bucket NONCLUSTERED (CollectDate, ServerName, CounterPath, BucketStart)
);
GO

-- 3) 日聚合（保留长期趋势）
IF OBJECT_ID('dbo.PerfMon_DailySummary','U') IS NULL
CREATE TABLE dbo.PerfMon_DailySummary(
    Id            BIGINT IDENTITY(1,1) PRIMARY KEY,
    ServerName    NVARCHAR(128),
    CounterPath   NVARCHAR(512),
    AvgValue      FLOAT,
    MaxValue      FLOAT,
    MinValue      FLOAT,
    MaxTime       DATETIME2,
    SummaryDate   DATE,
    TotalSamples  INT
);
GO
```

---

## 第二步：服务器上启动采集器（PowerShell，后台常驻）

保存为 `C:\DBA\scripts\PerfMonCollector.ps1`，用任务计划程序开机启动或 SQL Agent Job 调用。

```powershell
# ============================================================
# PerfMonCollector.ps1   v2 (Time-bucket aware)
# ------------------------------------------------------------
# 变化：
# - 采样 30s（原 60s）
# - 批量 Insert（SqlBulkCopy 替代单行 Insert，降低负载）
# - stop.flag 优雅停机
# ============================================================

$serverName       = $env:COMPUTERNAME
$sqlServer        = "YOUR_MONITOR_DB_HOST"   # 监控库所在实例
$database         = "db_monitor"
$connectionString = "Server=$sqlServer;Database=$database;Trusted_Connection=True;"
$intervalSeconds  = 30
$logFolder        = "C:\DBA\logs"
$logFile          = "$logFolder\PerfMonCollector.log"
$stopFile         = "$logFolder\stop.flag"

if (!(Test-Path $logFolder)) { New-Item -Path $logFolder -ItemType Directory | Out-Null }

function Write-Log($msg) {
    $t = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "$t $msg"
}

Write-Log "=== START v2 ==="

# --- 自动识别 SQL 实例前缀 ---
$sqlPrefix = "SQLServer"
try {
    $svc = Get-Service | Where-Object { $_.Name -like "MSSQL$*" -or $_.Name -eq "MSSQLSERVER" } | Select-Object -First 1
    if ($svc -and $svc.Name -ne "MSSQLSERVER") { $sqlPrefix = $svc.Name }
} catch { }
Write-Log "SqlPrefix=$sqlPrefix"

# --- 计数器清单（与 v1 保持一致，略精简）---
$counters = @(
 "\Memory\Available MBytes","\Memory\Pages/sec","\Memory\Page Reads/sec","\Memory\Page Writes/sec",
 "\Processor(_Total)\% Processor Time","\Processor(_Total)\% User Time","\Processor(_Total)\% Privileged Time",
 "\System\Processor Queue Length","\System\Context Switches/sec",
 "\PhysicalDisk(_Total)\Avg. Disk sec/Read","\PhysicalDisk(_Total)\Avg. Disk sec/Write",
 "\PhysicalDisk(_Total)\Avg. Disk Queue Length",
 "\PhysicalDisk(_Total)\Disk Read Bytes/sec","\PhysicalDisk(_Total)\Disk Write Bytes/sec",
 "\Network Interface(*)\Bytes Received/sec","\Network Interface(*)\Bytes Sent/sec",
 "\$sqlPrefix`:Buffer Manager\Page life expectancy",
 "\$sqlPrefix`:Buffer Manager\Page reads/sec","\$sqlPrefix`:Buffer Manager\Page writes/sec",
 "\$sqlPrefix`:Buffer Manager\Checkpoint pages/sec","\$sqlPrefix`:Buffer Manager\Lazy writes/sec",
 "\$sqlPrefix`:Access Methods\Full Scans/sec","\$sqlPrefix`:Access Methods\Index Searches/sec",
 "\$sqlPrefix`:Access Methods\Page Splits/sec","\$sqlPrefix`:Access Methods\Worktables Created/sec",
 "\$sqlPrefix`:General Statistics\User Connections","\$sqlPrefix`:General Statistics\Logins/sec",
 "\$sqlPrefix`:SQL Statistics\Batch Requests/sec","\$sqlPrefix`:SQL Statistics\SQL Compilations/sec",
 "\$sqlPrefix`:SQL Statistics\SQL Re-Compilations/sec",
 "\$sqlPrefix`:Locks(_Total)\Lock Requests/sec","\$sqlPrefix`:Locks(_Total)\Lock Waits/sec",
 "\$sqlPrefix`:Locks(_Total)\Lock Wait Time (ms)","\$sqlPrefix`:Locks(_Total)\Number of Deadlocks/sec",
 "\$sqlPrefix`:Latches\Latch Waits/sec","\$sqlPrefix`:Latches\Average Latch Wait Time (ms)",
 "\$sqlPrefix`:Memory Manager\Total Server Memory (KB)",
 "\$sqlPrefix`:Memory Manager\Target Server Memory (KB)",
 "\$sqlPrefix`:Memory Manager\Memory Grants Pending",
 "\$sqlPrefix`:Databases(_Total)\Transactions/sec",
 "\$sqlPrefix`:Databases(_Total)\Log Flush Wait Time",
 "\$sqlPrefix`:Databases(_Total)\Percent Log Used"
)

# --- 校验计数器 ---
$valid = @()
foreach ($c in $counters) {
    try { Get-Counter -Counter $c -ErrorAction Stop | Out-Null; $valid += $c }
    catch { Write-Log "Invalid: $c" }
}
Write-Log "Valid=$($valid.Count)"

# --- 构建 DataTable，SqlBulkCopy 批量写入 ---
function New-DataTable {
    $dt = New-Object System.Data.DataTable
    [void]$dt.Columns.Add("ServerName",   [string])
    [void]$dt.Columns.Add("CounterPath",  [string])
    [void]$dt.Columns.Add("CounterValue", [double])
    [void]$dt.Columns.Add("CollectTime",  [datetime])
    return $dt
}
function Write-Bulk($dt) {
    try {
        $cn = New-Object System.Data.SqlClient.SqlConnection $connectionString
        $cn.Open()
        $bc = New-Object System.Data.SqlClient.SqlBulkCopy $cn
        $bc.DestinationTableName = "dbo.PerfMon_Raw"
        $bc.BatchSize = 500
        $bc.WriteToServer($dt)
        $cn.Close()
    } catch { Write-Log "Bulk Insert Failed: $($_.Exception.Message)" }
}

# --- 主循环 ---
while ($true) {
    if (Test-Path $stopFile) { Write-Log "Stop detected"; Remove-Item $stopFile -Force; break }
    try {
        $now = Get-Date
        $s = Get-Counter -Counter $valid -ErrorAction Stop
        $dt = New-DataTable
        foreach ($x in $s.CounterSamples) {
            [void]$dt.Rows.Add($serverName, $x.Path, [double]$x.CookedValue, $now)
        }
        if ($dt.Rows.Count -gt 0) { Write-Bulk $dt; Write-Log "OK $($dt.Rows.Count)" }
    } catch { Write-Log "Collect Fail: $($_.Exception.Message)" }
    Start-Sleep -Seconds $intervalSeconds
}
Write-Log "=== END ==="
```

---

## 第三步：5 分钟时间桶聚合存储过程（SQL Agent Job，每 5 分钟跑一次）

```sql
USE [db_monitor]
GO
CREATE OR ALTER PROCEDURE dbo.usp_PerfMon_BuildBucket_5min
AS
BEGIN
    SET NOCOUNT ON;

    -- 仅处理上一完整 5 分钟桶，避免重复
    -- 注意：采集侧 PerfMonCollector.ps1 的 CollectTime 写入的是本地时间（Get-Date）
    --       所以这里的桶边界必须用 SYSDATETIME()（本地），不能用 SYSUTCDATETIME()
    DECLARE @now DATETIME2 = SYSDATETIME();
    DECLARE @bucketStart DATETIME2 =
        DATEADD(MINUTE, (DATEDIFF(MINUTE, 0, @now) / 5) * 5, CAST(0 AS DATETIME2));
    SET @bucketStart = DATEADD(MINUTE, -5, @bucketStart);
    DECLARE @bucketEnd   DATETIME2 = DATEADD(MINUTE, 5, @bucketStart);

    ;WITH agg AS (
        SELECT
            ServerName,
            CounterPath,
            @bucketStart                AS BucketStart,
            AVG(CounterValue)           AS AvgValue,
            MAX(CounterValue)           AS MaxValue,
            MIN(CounterValue)           AS MinValue,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY CounterValue)
                OVER (PARTITION BY ServerName, CounterPath) AS P95Value,
            COUNT(*)                    AS Samples,
            ROW_NUMBER() OVER (PARTITION BY ServerName, CounterPath ORDER BY (SELECT 1)) AS rn
        FROM dbo.PerfMon_Raw
        WHERE CollectTime >= @bucketStart AND CollectTime < @bucketEnd
        GROUP BY ServerName, CounterPath, CounterValue
    )
    INSERT INTO dbo.PerfMon_TimeBucket
        (ServerName, CounterPath, BucketStart, AvgValue, MaxValue, MinValue, P95Value, Samples)
    SELECT ServerName, CounterPath, BucketStart,
           AVG(AvgValue), MAX(MaxValue), MIN(MinValue), MAX(P95Value), SUM(Samples)
    FROM agg
    GROUP BY ServerName, CounterPath, BucketStart;
END
GO
```

> **为什么是 5 分钟桶**：和慢 SQL、阻塞脚本统一时间粒度，做「CPU 尖峰发生时正在跑哪些 SQL」时能 `JOIN ON bucket_start`。

---

## 第四步：日聚合（保留长期基线，每日凌晨 02:00 执行）

```sql
CREATE OR ALTER PROCEDURE dbo.usp_PerfMon_DailySummary
AS
BEGIN
    INSERT INTO dbo.PerfMon_DailySummary
        (ServerName, CounterPath, AvgValue, MaxValue, MinValue, MaxTime, SummaryDate, TotalSamples)
    SELECT
        ServerName, CounterPath,
        AVG(CounterValue), MAX(CounterValue), MIN(CounterValue),
        MAX(CollectTime), CAST(CollectTime AS DATE), COUNT(*)
    FROM dbo.PerfMon_Raw
    WHERE CAST(CollectTime AS DATE) = CAST(DATEADD(DAY, -1, GETDATE()) AS DATE)
    GROUP BY ServerName, CounterPath, CAST(CollectTime AS DATE);

    -- 清理 7 天前的原始点（CollectTime 是本地时间，用 SYSDATETIME）
    DELETE FROM dbo.PerfMon_Raw
    WHERE CollectTime < DATEADD(DAY, -7, SYSDATETIME());
END
GO
```

---

## 第五步：CSV 导出（每日 03:00，供 agent 侧分析）

保存为 `C:\DBA\scripts\Export-PerfMon-Csv.ps1`。

```powershell
param(
    [string]$StatDate = $(Get-Date -Format "yyyy-MM-dd"),
    [string]$RootDir  = "D:\DBA\insagent-data\server_per_sql"
)

$Server   = "YOUR_MONITOR_DB_HOST"
$Database = "db_monitor"
$OutputDir = Join-Path $RootDir $StatDate
if (!(Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

# --- 5 分钟桶（核心数据，给关联分析用） ---
$q1 = @"
SELECT ServerName, CounterPath, BucketStart, AvgValue, MaxValue, MinValue, P95Value, Samples
FROM dbo.PerfMon_TimeBucket
WHERE CollectDate = '$StatDate'
ORDER BY ServerName, CounterPath, BucketStart
"@
Invoke-Sqlcmd -ServerInstance $Server -Database $Database -Query $q1 -QueryTimeout 0 |
    Export-Csv -Path (Join-Path $OutputDir "perfmon_5min_$($StatDate).csv") -NoTypeInformation -Encoding UTF8

# --- 日汇总（长期趋势） ---
$q2 = @"
SELECT ServerName, CounterPath, AvgValue, MaxValue, MinValue, MaxTime, SummaryDate, TotalSamples
FROM dbo.PerfMon_DailySummary
WHERE SummaryDate = '$StatDate'
"@
Invoke-Sqlcmd -ServerInstance $Server -Database $Database -Query $q2 -QueryTimeout 0 |
    Export-Csv -Path (Join-Path $OutputDir "perfmon_daily_$($StatDate).csv") -NoTypeInformation -Encoding UTF8

Write-Host "DONE -> $OutputDir"
```

---

## 第六步：调度建议

| Job | 频率 | 说明 |
|---|---|---|
| PerfMonCollector.ps1 | 开机常驻 | 通过任务计划程序「系统启动时运行」 |
| usp_PerfMon_BuildBucket_5min | 每 5 分钟 | SQL Agent Job |
| usp_PerfMon_DailySummary | 每日 02:00 | SQL Agent Job |
| Export-PerfMon-Csv.ps1 | 每日 03:00 | 任务计划程序 |

---

## 第七步：部署调度（SQL Agent Job + 任务计划）

### 7.1 SQL Agent Job 注册（在监控库实例执行，一次性）

```sql
USE msdb;
GO

-- ---------- Job 1: usp_PerfMon_BuildBucket_5min → 每 5 分钟 ----------
IF EXISTS (SELECT 1 FROM dbo.sysjobs WHERE name = 'InsAgent_PerfMon_BuildBucket_5min')
    EXEC dbo.sp_delete_job @job_name = N'InsAgent_PerfMon_BuildBucket_5min';
EXEC dbo.sp_add_job
    @job_name = N'InsAgent_PerfMon_BuildBucket_5min',
    @enabled  = 1,
    @description = N'Aggregate PerfMon_Raw into 5-min time buckets';
EXEC dbo.sp_add_jobserver @job_name = N'InsAgent_PerfMon_BuildBucket_5min';
EXEC dbo.sp_add_jobstep
    @job_name = N'InsAgent_PerfMon_BuildBucket_5min',
    @step_name= N'Run proc',
    @subsystem= N'TSQL',
    @database_name = N'db_monitor',
    @command       = N'EXEC dbo.usp_PerfMon_BuildBucket_5min;';
EXEC dbo.sp_add_schedule
    @schedule_name = N'Every5Min_PerfMonBucket',
    @freq_type     = 4,        -- daily
    @freq_interval = 1,
    @freq_subday_type     = 4, -- minute
    @freq_subday_interval = 5,
    @active_start_time    = 000000;
EXEC dbo.sp_attach_schedule
    @job_name      = N'InsAgent_PerfMon_BuildBucket_5min',
    @schedule_name = N'Every5Min_PerfMonBucket';

-- ---------- Job 2: usp_PerfMon_DailySummary → 每日 02:00 ----------
IF EXISTS (SELECT 1 FROM dbo.sysjobs WHERE name = 'InsAgent_PerfMon_DailySummary')
    EXEC dbo.sp_delete_job @job_name = N'InsAgent_PerfMon_DailySummary';
EXEC dbo.sp_add_job           @job_name = N'InsAgent_PerfMon_DailySummary', @enabled = 1;
EXEC dbo.sp_add_jobserver     @job_name = N'InsAgent_PerfMon_DailySummary';
EXEC dbo.sp_add_jobstep
    @job_name = N'InsAgent_PerfMon_DailySummary',
    @step_name= N'Run proc',
    @subsystem= N'TSQL',
    @database_name = N'db_monitor',
    @command       = N'EXEC dbo.usp_PerfMon_DailySummary;';
EXEC dbo.sp_add_schedule
    @schedule_name = N'Daily_0200_PerfMonDaily',
    @freq_type     = 4, @freq_interval = 1,
    @active_start_time = 020000;
EXEC dbo.sp_attach_schedule
    @job_name = N'InsAgent_PerfMon_DailySummary',
    @schedule_name = N'Daily_0200_PerfMonDaily';
GO
```

### 7.2 Windows 任务计划注册（在各服务器管理员 PowerShell 执行）

```powershell
# ---------- Task 1: PerfMonCollector.ps1 → 开机自启 ----------
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\DBA\scripts\PerfMonCollector.ps1'
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 3
Register-ScheduledTask -TaskName 'InsAgent_PerfMonCollector' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force

# ---------- Task 2: Export-PerfMon-Csv.ps1 → 每日 03:00 ----------
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\DBA\scripts\Export-PerfMon-Csv.ps1'
$trigger = New-ScheduledTaskTrigger -Daily -At 3:00AM
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'InsAgent_Export_PerfMon' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
```

### 7.3 数据保留

- 数据库内 `PerfMon_Raw` 由 `usp_PerfMon_DailySummary` 自动清理 7 天前点；`PerfMon_TimeBucket` 保留 90 天（按需手动 DELETE）
- CSV 文件由 **README 统一的 `Clean-OldData.ps1`** 清理，默认 `server_per_sql` 保留 **30 天**。如需改天数，修改 Clean-OldData.ps1 的 `$RetentionDays` 或执行时传参：
  ```powershell
  powershell -File C:\DBA\scripts\Clean-OldData.ps1 -RetentionDays @{server_per_sql=7}
  ```

---

## 第八步：交付给 agent

CSV 文件落地路径：

```
D:\DBA\insagent-data\server_per_sql\YYYY-MM-DD\
    ├─ perfmon_5min_YYYY-MM-DD.csv
    └─ perfmon_daily_YYYY-MM-DD.csv
```

把该日期文件夹拷贝/同步到 agent 主机：

```
$DATA_ROOT/server_per_sql/YYYY-MM-DD/
```

agent 侧会自动识别（见 `skills/sql_perf_analyzer/SKILL.md`）。
