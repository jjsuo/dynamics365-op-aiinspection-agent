# 03 - SQL 阻塞采集

> **目的**：定期快照当前 SQL Server 上的阻塞会话链，落盘为 CSV。
> **最终产物**：`D:\DBA\insagent-data\sql_blocking\YYYY-MM-DD\blocking_HHmm.csv`，拷贝到 agent 的 `$DATA_ROOT/sql_blocking/YYYY-MM-DD/`。
> **列对齐**：完全对齐你提供的 `blocking_01.csv` 样例（23 列），agent 侧的 `sql_block_analysis` 可直接解析。

---

## 一、CSV 列约定（与 blocking_01.csv 保持一致）

| # | 列名 | 说明 |
|---|------|------|
| 1  | ßßß（或 SnapshotId） | 本次快照时间戳标识 |
| 2  | blocktypes       | 阻塞层级（1=叶节点，2/3=中间，0=根） |
| 3  | spid             | 当前会话 SPID |
| 4  | blocked          | 被谁阻塞（0 = 不被阻塞，是根阻塞者） |
| 5  | waittype         | 等待类型（原始字节） |
| 6  | waittime         | 等待时长（毫秒） |
| 7  | lastwaittype     | 上一次等待类型（文本，如 LCK_M_IX） |
| 8  | waitresource     | 等待资源（TAB / PAG / KEY / RID） |
| 9  | dbname           | 数据库名 |
| 10 | username         | 当前数据库用户 |
| 11 | cpu              | 累计 CPU（ms） |
| 12 | physical_io      | 累计物理 IO |
| 13 | memusage         | 占用内存页（8KB） |
| 14 | login_time       | 登录时间戳（毫秒） |
| 15 | last_batch       | 最后一次 batch 时间戳（毫秒） |
| 16 | open_tran        | 打开事务数 |
| 17 | processstatus    | 进程状态（running/suspended/sleeping） |
| 18 | hostname         | 客户端主机名 |
| 19 | program_name     | 客户端程序名 |
| 20 | cmd              | 正在执行的命令类型（INSERT/DELETE/…） |
| 21 | net_library      | 网络协议（TCP/IP / Named Pipe） |
| 22 | loginame         | 登录账号 |
| 23 | executing_sql_text | 当前正在执行的 SQL 文本 |

---

## 二、采集脚本（SQL Server 实例本地）

保存为 `C:\DBA\scripts\Collect-Blocking.sql`。

```sql
-- ============================================================
-- Collect-Blocking.sql
-- 输出列严格对齐 blocking_01.csv
-- ============================================================
SET NOCOUNT ON;

DECLARE @snap BIGINT = DATEDIFF_BIG(MILLISECOND, '1970-01-01', GETUTCDATE());

;WITH chain AS (
    -- 根节点：被阻塞但不阻塞别人 → 先找所有 blocked != 0 涉及的链
    SELECT p.spid, p.blocked, 1 AS lvl
    FROM sys.sysprocesses p
    WHERE p.blocked <> 0 OR p.spid IN (SELECT blocked FROM sys.sysprocesses WHERE blocked <> 0)
),
lvl AS (
    -- 层级编号：被阻塞者 lvl=1，其阻塞者为 2，以此类推
    SELECT c.spid, c.blocked, 1 AS blocktypes
    FROM chain c
    WHERE c.blocked <> 0
    UNION ALL
    SELECT p.spid, p.blocked, l.blocktypes + 1
    FROM sys.sysprocesses p
    INNER JOIN lvl l ON l.blocked = p.spid AND p.blocked <> 0
),
ranked AS (
    SELECT spid, MAX(blocktypes) AS blocktypes
    FROM lvl
    GROUP BY spid
)
SELECT
    @snap                                                           AS [SnapshotId],
    ISNULL(r.blocktypes, 0)                                         AS [blocktypes],
    p.spid                                                          AS [spid],
    p.blocked                                                       AS [blocked],
    p.waittype                                                      AS [waittype],
    p.waittime                                                      AS [waittime],
    p.lastwaittype                                                  AS [lastwaittype],
    p.waitresource                                                  AS [waitresource],
    DB_NAME(p.dbid)                                                 AS [dbname],
    SUSER_SNAME(p.sid)                                              AS [username],
    p.cpu                                                           AS [cpu],
    p.physical_io                                                   AS [physical_io],
    p.memusage                                                      AS [memusage],
    DATEDIFF_BIG(MILLISECOND, '1970-01-01', p.login_time)           AS [login_time],
    DATEDIFF_BIG(MILLISECOND, '1970-01-01', p.last_batch)           AS [last_batch],
    p.open_tran                                                     AS [open_tran],
    p.status                                                        AS [processstatus],
    RTRIM(p.hostname)                                               AS [hostname],
    RTRIM(p.program_name)                                           AS [program_name],
    RTRIM(p.cmd)                                                    AS [cmd],
    RTRIM(p.net_library)                                            AS [net_library],
    RTRIM(p.loginame)                                               AS [loginame],
    REPLACE(REPLACE(st.text, CHAR(13), ' '), CHAR(10), ' ')         AS [executing_sql_text]
FROM sys.sysprocesses p
LEFT JOIN ranked r ON r.spid = p.spid
OUTER APPLY sys.dm_exec_sql_text(p.sql_handle) AS st
WHERE p.spid > 50                  -- 排除系统会话
  AND (p.blocked <> 0
       OR p.spid IN (SELECT blocked FROM sys.sysprocesses WHERE blocked <> 0))
ORDER BY r.blocktypes, p.spid;
```

---

## 三、PowerShell 导出（定时触发）

保存为 `C:\DBA\scripts\Export-Blocking-Csv.ps1`。

```powershell
param(
    [string]$Server   = "localhost",                      # 目标 SQL 实例
    [string]$RootDir  = "D:\DBA\insagent-data\sql_blocking"
)

$now       = Get-Date
$dateStr   = $now.ToString("yyyy-MM-dd")
$timeStr   = $now.ToString("HHmm")
$outputDir = Join-Path $RootDir $dateStr
if (!(Test-Path $outputDir)) { New-Item -ItemType Directory -Path $outputDir | Out-Null }

$outFile = Join-Path $outputDir "blocking_$timeStr.csv"

$sqlFile = "C:\DBA\scripts\Collect-Blocking.sql"

try {
    $rows = Invoke-Sqlcmd -ServerInstance $Server -InputFile $sqlFile -QueryTimeout 60
    if ($rows -eq $null -or $rows.Count -eq 0) {
        Write-Host "No blocking found at $dateStr $timeStr"
        # 可选：仍输出空文件，表示已采集
        "SnapshotId,blocktypes,spid,blocked,waittype,waittime,lastwaittype,waitresource,dbname,username,cpu,physical_io,memusage,login_time,last_batch,open_tran,processstatus,hostname,program_name,cmd,net_library,loginame,executing_sql_text" |
            Out-File -FilePath $outFile -Encoding UTF8
        exit 0
    }
    $rows | Export-Csv -Path $outFile -NoTypeInformation -Encoding UTF8
    Write-Host "Exported $($rows.Count) rows -> $outFile"
}
catch {
    Write-Host "Failed: $($_.Exception.Message)"
    exit 1
}
```

---

## 四、调度建议

| 触发方式 | 推荐频率 | 说明 |
|---|---|---|
| SQL Agent Job 或 Windows 任务计划 | **每 5 分钟**（正常） | 有阻塞就留档，没阻塞写空文件（便于事后检查是否确实没阻塞） |
| 手动触发 | 用户报异常时临时抓取 | `pwsh Export-Blocking-Csv.ps1` |

若阻塞频繁，可把频率调到 **每 1 分钟**；生产影响几乎忽略（查询 `sys.sysprocesses` 极轻）。

---

## 五、部署调度（任务计划程序）

在目标 SQL 服务器管理员 PowerShell 执行一次即可：

```powershell
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\DBA\scripts\Export-Blocking-Csv.ps1'

# 每 5 分钟循环，24 小时不限时长
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
           -RepetitionInterval (New-TimeSpan -Minutes 5) `
           -RepetitionDuration ([TimeSpan]::MaxValue)

$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 3)

Register-ScheduledTask -TaskName 'InsAgent_Export_Blocking' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
```

> 依赖：`SqlServer` PowerShell 模块已安装（`Install-Module SqlServer -AllowClobber -Force`）；系统账户默认可用 Trusted 连接，如非同机连请修改脚本的 `-ServerInstance`/认证参数。

### 手工触发（异常时）

```powershell
Start-ScheduledTask -TaskName 'InsAgent_Export_Blocking'
# 或直接跑脚本
pwsh C:\DBA\scripts\Export-Blocking-Csv.ps1
```

### 数据保留

- CSV 文件由 **README 统一的 `Clean-OldData.ps1`** 清理，默认 `sql_blocking` 保留 **30 天**。
- 按每 5 分钟一个文件估算，30 天 ≈ 8,640 个 CSV（单文件通常 KB 级），空间可忽略。
- 如需历史总览和明细分离，可为 `sql_blocking` 调短为 7 天：
  ```powershell
  powershell -File C:\DBA\scripts\Clean-OldData.ps1 -RetentionDays @{sql_blocking=7}
  ```

---

## 六、交付给 agent

```
D:\DBA\insagent-data\sql_blocking\YYYY-MM-DD\
    ├─ blocking_0900.csv
    ├─ blocking_0905.csv
    ├─ blocking_0910.csv
    └─ ...
```

拷贝到 agent 的 `$DATA_ROOT/sql_blocking/YYYY-MM-DD/`。
agent 的 `sql_block_analysis` skill 会读取当日所有 `blocking_*.csv` 并做阻塞链 / 重复 SQL / 根因分析。

---

## 七、排查辅助（可选）：开启系统健康 XEvent

SQL Server 默认的 `system_health` XE 会话会在发生死锁时自动记录 `xml_deadlock_report`。如果还想捕获 **死锁**（阻塞 ≠ 死锁），无需额外部署，直接按日导出即可：

```sql
-- 导出最近 24 小时的死锁 XML
SELECT
    xed.value('(@timestamp)[1]','datetime2') AS DeadlockTime,
    xed.query('.') AS DeadlockXml
FROM (
    SELECT CAST(target_data AS XML) AS td
    FROM sys.dm_xe_session_targets st
    JOIN sys.dm_xe_sessions s ON s.address = st.event_session_address
    WHERE s.name = 'system_health' AND st.target_name = 'ring_buffer'
) t
CROSS APPLY td.nodes('//RingBufferTarget/event[@name="xml_deadlock_report"]') AS X(xed)
WHERE xed.value('(@timestamp)[1]','datetime2') >= DATEADD(DAY, -1, SYSUTCDATETIME());
```

可增量另存为 `deadlock_YYYY-MM-DD.csv`，放到同一日期文件夹下供 `sql_block_analysis` 关联使用。
