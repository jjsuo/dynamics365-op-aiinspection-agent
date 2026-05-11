# 05 - IIS 日志采集

> **目的**：从 IIS/CRM Web 服务器采集原始 W3C 日志 + 应用池运行状态快照，供 agent 的 `iis_analysis` skill 分析请求耗时、状态码、慢端点、应用池异常。
> **最终产物**：`D:\DBA\insagent-data\iis_logs\YYYY-MM-DD\{u_exYYMMDD.log, apppool_status.csv, iis_worker_processes.csv}`，拷贝到 agent 的 `$DATA_ROOT/iis_logs/YYYY-MM-DD/`。

---

## 一、前置：确认 IIS 日志格式

1. IIS 管理器 → 站点 → 「日志记录」 → 格式选 **W3C**。
2. 点「选择字段」，至少勾选：
   - date、time、s-sitename、s-computername、s-ip、cs-method、cs-uri-stem、cs-uri-query
   - s-port、cs-username、c-ip、cs(User-Agent)、cs(Referer)
   - **sc-status、sc-substatus、sc-win32-status**
   - **time-taken**、sc-bytes、cs-bytes
3. 滚动周期建议「每日」，目录默认 `%SystemDrive%\inetpub\logs\LogFiles\W3SVCn\`。

> `time-taken` 是关键字段；缺失将无法做慢请求分析。

---

## 二、PowerShell 采集脚本

保存为 `C:\DBA\scripts\Collect-IIS.ps1`。

```powershell
param(
    [string]$StatDate  = $(Get-Date -Format "yyyy-MM-dd"),
    [string]$RootDir   = "D:\DBA\insagent-data\iis_logs",
    [string]$LogsRoot  = "C:\inetpub\logs\LogFiles"      # 默认 IIS 日志根
)

$DateTag   = [datetime]::Parse($StatDate).ToString("yyMMdd")   # IIS 文件名是 yyMMdd
$OutputDir = Join-Path $RootDir $StatDate
if (!(Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

# -------------------------------------------------------
# 1) 拷贝当日 W3C 日志（所有站点 W3SVC*）
# -------------------------------------------------------
Write-Host "[1/3] Copy W3C logs ..."
$sites = Get-ChildItem $LogsRoot -Directory -Filter "W3SVC*" -ErrorAction SilentlyContinue
foreach ($s in $sites) {
    $src = Join-Path $s.FullName ("u_ex$DateTag.log")
    if (Test-Path $src) {
        # 文件名加上站点编号，避免多站点同名覆盖
        $dst = Join-Path $OutputDir ("u_ex$DateTag" + "_" + $s.Name + ".log")
        Copy-Item -Path $src -Destination $dst -Force
        Write-Host "   $src -> $dst"
    } else {
        Write-Host "   (skip) no log at $src"
    }
}

# -------------------------------------------------------
# 2) 应用池状态快照
# -------------------------------------------------------
Write-Host "[2/3] AppPool status ..."
Import-Module WebAdministration -ErrorAction SilentlyContinue
$apppools = Get-ChildItem IIS:\AppPools | ForEach-Object {
    $n = $_.Name
    [PSCustomObject]@{
        Name                     = $n
        State                    = $_.State
        AutoStart                = $_.autoStart
        ManagedRuntimeVersion    = $_.managedRuntimeVersion
        ManagedPipelineMode      = $_.managedPipelineMode
        IdentityType             = $_.processModel.identityType
        IdleTimeoutMinutes       = [int]$_.processModel.idleTimeout.TotalMinutes
        MaxProcesses             = $_.processModel.maxProcesses
        RecycleRequests          = $_.recycling.periodicRestart.requests
        RecycleTimeMinutes       = [int]$_.recycling.periodicRestart.time.TotalMinutes
        PrivateMemoryLimitKB     = $_.recycling.periodicRestart.privateMemory
        RapidFailEnabled         = $_.failure.rapidFailProtection
        RapidFailIntervalMin     = [int]$_.failure.rapidFailProtectionInterval.TotalMinutes
        RapidFailMaxCrashes      = $_.failure.rapidFailProtectionMaxCrashes
        CollectDate              = $StatDate
    }
}
$apppools | Export-Csv -Path (Join-Path $OutputDir "apppool_status.csv") `
                       -NoTypeInformation -Encoding UTF8
Write-Host "   -> apppool_status.csv ($($apppools.Count) pools)"

# -------------------------------------------------------
# 3) IIS 工作进程（w3wp.exe）内存/CPU 快照
# -------------------------------------------------------
Write-Host "[3/3] Worker processes ..."
$procs = @()
try {
    $wps = Get-ChildItem IIS:\AppPools | ForEach-Object {
        $poolName = $_.Name
        try {
            Get-ChildItem "IIS:\AppPools\$poolName\WorkerProcesses" -ErrorAction Stop |
                ForEach-Object {
                    $proc = Get-Process -Id $_.processId -ErrorAction SilentlyContinue
                    [PSCustomObject]@{
                        AppPool         = $poolName
                        PID             = $_.processId
                        State           = $_.State
                        StartTime       = if ($proc) { $proc.StartTime } else { $null }
                        WorkingSetMB    = if ($proc) { [math]::Round($proc.WorkingSet64/1MB,1) } else { 0 }
                        PrivateMemoryMB = if ($proc) { [math]::Round($proc.PrivateMemorySize64/1MB,1) } else { 0 }
                        ThreadCount     = if ($proc) { $proc.Threads.Count } else { 0 }
                        HandleCount     = if ($proc) { $proc.HandleCount } else { 0 }
                        CollectTime     = Get-Date
                    }
                }
        } catch { }
    }
    $procs += $wps
} catch { Write-Host "   (WebAdministration not available)" }

if ($procs.Count -gt 0) {
    $procs | Export-Csv -Path (Join-Path $OutputDir "iis_worker_processes.csv") `
                        -NoTypeInformation -Encoding UTF8
    Write-Host "   -> iis_worker_processes.csv ($($procs.Count) rows)"
}

Write-Host "DONE -> $OutputDir"
```

---

## 三、调度建议

| 任务 | 频率 | 备注 |
|---|---|---|
| `Collect-IIS.ps1`（默认参数）| 每日 **03:30** | IIS 日志已滚动，可拷贝前一天（传 `-StatDate (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")`）|
| 异常时手动 | 随时 | 问题重现时立即抓 `apppool_status.csv` + `iis_worker_processes.csv` |

> 如果 `time-taken` 阈值持续告警，建议配合开启 **Failed Request Tracing (FRT)**，tag 请求耗时 > N 秒，XML 另外输出到 `FRTLogs\` 拷回 agent 即可。

---

## 四、部署调度（任务计划程序）

在 IIS/CRM 服务器管理员 PowerShell 执行：

```powershell
# 每日 03:30 采集前一天的 IIS 日志，避免当日文件还在写
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument '-NoProfile -ExecutionPolicy Bypass -Command "& C:\DBA\scripts\Collect-IIS.ps1 -StatDate (Get-Date).AddDays(-1).ToString(''yyyy-MM-dd'')"'
$trigger = New-ScheduledTaskTrigger -Daily -At 3:30AM
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName 'InsAgent_Collect_IIS' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
```

> `WebAdministration` 模块依赖 IIS 角色；`SYSTEM` 账户在 IIS 服务器上默认有读权限。如需补抓当日日志，可再添加一个 `22:00` 或 `23:55` 的任务拷当日日志。

### 手工触发

```powershell
Start-ScheduledTask -TaskName 'InsAgent_Collect_IIS'
# 或内临时抓当日：
pwsh C:\DBA\scripts\Collect-IIS.ps1
```

### 数据保留

- 日志 + CSV 文件由 **README 统一的 `Clean-OldData.ps1`** 清理，默认 `iis_logs` 保留 **30 天**。
- IIS W3C 日志单文件可能较大（高流量站点每天 GB 级），如磁盘紧张可改为 7 天：
  ```powershell
  powershell -File C:\DBA\scripts\Clean-OldData.ps1 -RetentionDays @{iis_logs=7}
  ```
- 源端 `C:\inetpub\logs\LogFiles\W3SVC*\` 的原始日志另行设置 IIS 自身的日志清理（推荐 14 天），本脚本不动源日志。

---

## 五、交付给 agent

```
D:\DBA\insagent-data\iis_logs\YYYY-MM-DD\
    ├─ u_exYYMMDD_W3SVC1.log
    ├─ u_exYYMMDD_W3SVC2.log
    ├─ apppool_status.csv
    └─ iis_worker_processes.csv
```

拷贝到 agent 的 `$DATA_ROOT/iis_logs/YYYY-MM-DD/`。
agent 侧 `iis_analysis` skill 会解析 W3C 日志并结合应用池快照做健康评估。
