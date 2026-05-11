# ServerCollectionScript —— 服务器端采集脚本集合

> D365-HealthGuard 的 agent **不直接连接生产服务器**。
> 所有数据由本目录下的脚本在服务器本机执行 → 产出 CSV / 日志 → **人工拷贝**到 agent 的 `$DATA_ROOT` → 由 agent 的 data_reader + skill 分析。

---

## 一、设计原则

1. **服务器端输出路径统一**：`D:\DBA\insagent-data\<category>\<YYYY-MM-DD>\*.csv`
2. **Category 名称与 agent 侧 100% 对齐**，拷贝时只需把日期文件夹整体搬走：

   | agent 侧 category | 服务器侧输出目录 |
   |---|---|
   | `server_per_sql` | `D:\DBA\insagent-data\server_per_sql\` |
   | `slow_sql`       | `D:\DBA\insagent-data\slow_sql\` |
   | `sql_blocking`   | `D:\DBA\insagent-data\sql_blocking\` |
   | `sql_index`      | `D:\DBA\insagent-data\sql_index\`（含根目录 `index_existing.csv` 静态） |
   | `iis_logs`       | `D:\DBA\insagent-data\iis_logs\` |
   | `windows_health` | `D:\DBA\insagent-data\windows_health\` |
   | `plugin_scan`    | `D:\DBA\insagent-data\plugin_scan\` |

3. **时间桶粒度统一为 5 分钟**（PerfMon、慢 SQL）：让「CPU 尖峰对齐慢 SQL Top 榜」这类跨源分析可以直接用 `JOIN ON BucketStart`。
4. **采集脚本不分析、不告警**，只负责落盘 CSV。分析交给 agent 的 skill。

---

## 二、文件索引

| # | 文件 | 覆盖的 agent skill | 输出 category |
|---|------|-------------------|----------------|
| 01 | [SQL Server 性能指标采集](./01_SQLServer性能指标采集.md) | `sql_perf_analyzer` | `server_per_sql` |
| 02 | [慢 SQL 采集](./02_慢SQL采集.md)（★时间桶聚合）| `slow_sql_analyst` | `slow_sql` |
| 03 | [SQL 阻塞采集](./03_SQL阻塞采集.md) | `sql_block_analysis` | `sql_blocking` |
| 04 | [索引与存储采集](./04_索引与存储采集.md) | `sql_index_optimizer` + `sql_storage_analysis` | `sql_index` |
| 05 | [IIS 日志采集](./05_IIS日志采集.md) | `iis_analysis` | `iis_logs` |
| 06 | [Windows 健康采集](./06_Windows健康采集.md) | `windows_health` | `windows_health` |
| 07 | [Plugin 采集](./07_Plugin采集.md) | `plugin_scanner` | `plugin_scan` |
| 08 | [环境上下文采集](./08_环境上下文采集.md)（★启动上下文）| 所有 skill 启动上下文 | 直接生成 `memory/*.json` |

---

## 三、部署流程（一次性）

1. 建立监控库 `db_monitor`（PerfMon / 慢 SQL 会建表）。
2. 创建服务器端目录：
   ```
   C:\DBA\scripts\         # 存放所有 PowerShell / SQL 脚本
   C:\DBA\xel\             # XEvent 文件输出目录
   C:\DBA\logs\            # 采集器日志
   D:\DBA\insagent-data\   # 采集产物根目录（按 category 自动建子目录）
   ```
3. 按各 .md 的「第一步/第二步…」逐项部署表 / 存储过程 / PowerShell。
4. 注册任务计划 / SQL Agent Job（见下表）。
5. 验证某一天的输出是否完整，再拷贝到 agent。

---

## 四、调度总览

### 常驻采集器（后台运行）
| 脚本 | 触发方式 |
|---|---|
| `PerfMonCollector.ps1` | 任务计划「系统启动时运行」 |
| XEvent Session `HighResourceUsage_Tracking` | `STARTUP_STATE = ON`，随实例启动 |

### 周期 Job

| 任务 | 频率 | 实现 | 注册脚本所在文档 |
|---|---|---|---|
| `usp_PerfMon_BuildBucket_5min`     | 每 5 分钟     | SQL Agent Job | 01 |
| `usp_PerfMon_DailySummary`         | 每日 02:00    | SQL Agent Job | 01 |
| `Export-PerfMon-Csv.ps1`           | 每日 03:00    | 任务计划 | 01 |
| `usp_SlowSQL_IngestFromXEL`        | 每小时 `:55` | SQL Agent Job | 02 |
| `usp_SlowSQL_BuildBucket_5min`     | 每小时 `:58` | SQL Agent Job | 02 |
| `usp_SlowSQL_DailySummary`         | 每日 02:10    | SQL Agent Job | 02 |
| `Export-SlowSQL-Csv.ps1`           | 每日 03:00    | 任务计划 | 02 |
| `Export-Blocking-Csv.ps1`          | 每 5 分钟     | 任务计划 | 03 |
| `Export-IndexStorage-Csv.ps1`      | 每周日 02:00  | 任务计划 | 04 |
| `Collect-IIS.ps1`                  | 每日 03:30    | 任务计划 | 05 |
| `Collect-WindowsHealth.ps1`        | 每日 03:00    | 任务计划 | 06 |
| `Collect-Plugin.ps1`               | 每月 1 日 02:00 | 任务计划 | 07 |
| `Collect-SqlConfig.ps1`            | 每月 1 日 01:30 | 任务计划 | 08 |
| `Clean-OldData.ps1`                | 每日 04:00    | 任务计划 | README (统一) |

> **说明**：每个采集脚本文档内的「部署调度」小节都已内置具体的 `Register-ScheduledTask` / `sp_add_job` 注册命令，直接复制运行即可。

---

## 五、拷贝到 agent（两种方案）

### 方案 A：手动拷贝（适合早期验证）

1. 压缩当日数据：
   ```powershell
   Compress-Archive -Path "D:\DBA\insagent-data\*\2026-05-08" `
                    -DestinationPath "D:\DBA\transfer\insagent_20260508.zip"
   ```
2. 上传到 agent 主机（OneDrive / SMB / SCP / 邮件皆可）。
3. 在 agent（WSL）侧解压到 `$DATA_ROOT`：
   ```bash
   unzip insagent_20260508.zip -d $DATA_ROOT/
   ```

### 方案 B：SMB 共享自动同步（推荐生产）

1. agent 主机挂载 SMB 共享：
   ```bash
   sudo mkdir -p /mnt/insagent
   sudo mount -t drvfs '\\\\SERVER01\\insagent-data' /mnt/insagent   # WSL
   export DATA_ROOT=/mnt/insagent
   ```
2. 采集端 `D:\DBA\insagent-data\` 设为共享目录 `\\SERVER01\insagent-data`（只读 + 指定账号）。
3. agent 直接读取，零拷贝。

---

## 六、数据保留策略（服务器侧）

### 数据库内部表（由各自 `usp_*_DailySummary` 自动清理）
- `PerfMon_Raw` / `SlowQueryLog` / `XEL 文件`：保留 **7 天**
- `PerfMon_TimeBucket` / `SlowQuery_TimeBucket`：保留 **90 天**（按需手动 DELETE 或分区）

### CSV / 日志文件（统一清理脚本）

保存为 `C:\DBA\scripts\Clean-OldData.ps1`，所有 category 共用，按天数清理日期子目录：

```powershell
param(
    [string]$RootDir   = "D:\DBA\insagent-data",
    # 按 category 单独设定保留天数（默认 30 天，特殊类别已加长）
    [hashtable]$RetentionDays = @{
        server_per_sql = 30
        slow_sql       = 30
        sql_blocking   = 30
        iis_logs       = 30
        windows_health = 30
        sql_index      = 90    # 周级数据，保留长一点
        plugin_scan    = 180   # 月级数据
    },
    [int]$DefaultDays = 30,
    [string]$LogFile = "C:\DBA\logs\Clean-OldData.log"
)

if (!(Test-Path (Split-Path $LogFile))) { New-Item -ItemType Directory -Path (Split-Path $LogFile) | Out-Null }
function Log($m){ "$((Get-Date).ToString('s')) $m" | Add-Content -Path $LogFile -Encoding UTF8; Write-Host $m }

Log "=== Clean-OldData START ==="
if (!(Test-Path $RootDir)) { Log "RootDir not exist: $RootDir"; return }

foreach ($catDir in Get-ChildItem $RootDir -Directory) {
    $cat  = $catDir.Name
    $days = if ($RetentionDays.ContainsKey($cat)) { $RetentionDays[$cat] } else { $DefaultDays }
    $cutoff = (Get-Date).AddDays(-$days)
    Log "[$cat] retention=$days days, cutoff=$($cutoff.ToString('yyyy-MM-dd'))"

    # 清理日期子目录（形如 2026-05-08）
    Get-ChildItem $catDir.FullName -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' } |
        ForEach-Object {
            try {
                $d = [datetime]::ParseExact($_.Name, 'yyyy-MM-dd', $null)
                if ($d -lt $cutoff) {
                    Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop
                    Log "  removed $($_.FullName)"
                }
            } catch { Log "  skip $($_.Name): $($_.Exception.Message)" }
        }
}

# 额外清理 C:\DBA\xel\ 下的 .xel（SQL 内置滚动，此处只兼收底）
$xelDir = 'C:\DBA\xel'
if (Test-Path $xelDir) {
    Get-ChildItem $xelDir -Filter '*.xel' |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

Log "=== Clean-OldData END ==="
```

### 注册为每日任务计划（只需执行一次）

```powershell
# 管理员 PowerShell 执行
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\DBA\scripts\Clean-OldData.ps1'
$trigger = New-ScheduledTaskTrigger -Daily -At 4:00AM
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'InsAgent_CleanOldData' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
```

### 迷你保留策略总览（默认值）

| Category | 天数 | 理由 |
|---|---|---|
| `server_per_sql` / `slow_sql` / `sql_blocking` / `iis_logs` / `windows_health` | **30 天** | 每日产出，足够复盘近一个月 |
| `sql_index` | **90 天** | 周级采集，保留三个月对比索引使用变化 |
| `plugin_scan` | **180 天** | 月级采集，保留半年够做 Plugin 发布史比对 |

> 如需同一次清理更严格，例如所有 category 统一 7 天，执行：
> `powershell -File C:\DBA\scripts\Clean-OldData.ps1 -DefaultDays 7 -RetentionDays @{}`

---

## 七、本次重构要点（相对旧脚本）

| 变化 | 原因 |
|---|---|
| PerfMon 采样 60s → **30s** | 保证 5 分钟桶内样本 ≥ 10，中位数/P95 有意义 |
| 新增 `PerfMon_TimeBucket` | 与慢 SQL 时间桶对齐，做「CPU 尖峰 × Top SQL」关联 |
| `SqlBulkCopy` 替换单行 INSERT | 采集器本身负载下降 ~80% |
| 慢 SQL XEvent 补 `duration` 字段 | 原脚本只记 CPU 不记耗时 |
| 新增 `SlowQuery_TimeBucket`（★ 本次核心） | **同一 SQL 指纹在同一 5 分钟窗口内的执行次数 / 总 CPU / 总耗时一次到位**，直接回答「服务器卡顿那 5 分钟谁在烧 CPU」 |
| 阻塞 SQL 查询列严格对齐 `blocking_01.csv` | agent 侧 reader 零改动 |
| 所有 category 的服务器侧输出路径统一到 `D:\DBA\insagent-data\` | 和 agent 的 `$DATA_ROOT` 一一映射，可共享 SMB |

---

## 八、问题排查

- **CSV 中文乱码**：所有 `Export-Csv` 都带 `-Encoding UTF8`，若 Windows 7/2008R2 默认还是 GB2312，升级 PowerShell 5+。
- **`Invoke-Sqlcmd` 不存在**：安装 `SqlServer` 模块 `Install-Module SqlServer -AllowClobber -Force`。
- **PerfMon 采集器占 CPU 高**：检查计数器清单是否包含了 `(*)` 通配符展开过多实例，按需收敛。
- **XEL 文件堆积**：`max_rollover_files = 20` 会自动滚动，但 `C:\DBA\xel\` 仍需定期清理（脚本 `usp_SlowSQL_DailySummary` 末尾可加 `DELETE` 文件逻辑，或单独 Job）。
