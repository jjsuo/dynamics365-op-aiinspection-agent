# 06 - Windows 健康采集

> **目的**：采集 Windows Server 的系统/应用事件日志（Error/Warning）、服务状态、磁盘空间、补丁状态、已登录会话，供 agent 的 `windows_health` skill 做系统层健康评估。
> **最终产物**：`D:\DBA\insagent-data\windows_health\YYYY-MM-DD\*.csv`，拷贝到 agent 的 `$DATA_ROOT/windows_health/YYYY-MM-DD/`。

---

## 一、PowerShell 采集脚本

保存为 `C:\DBA\scripts\Collect-WindowsHealth.ps1`。

```powershell
param(
    [string]$StatDate = $(Get-Date -Format "yyyy-MM-dd"),
    [string]$RootDir  = "D:\DBA\insagent-data\windows_health",
    [int]$EventDays   = 1      # 事件日志回看天数
)

$OutputDir = Join-Path $RootDir $StatDate
if (!(Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

$Server = $env:COMPUTERNAME
$After  = (Get-Date).AddDays(-$EventDays)

# -------------------------------------------------------
# 1) 事件日志（System + Application，Error + Warning）
# -------------------------------------------------------
Write-Host "[1/6] Event logs ..."
$logs = @("System","Application")
$events = foreach ($log in $logs) {
    try {
        Get-WinEvent -FilterHashtable @{
            LogName   = $log
            Level     = 2,3              # 2=Error, 3=Warning
            StartTime = $After
        } -ErrorAction Stop |
            Select-Object @{n='Server';e={$Server}}, LogName,
                          TimeCreated, Id, LevelDisplayName,
                          ProviderName, MachineName,
                          @{n='Message';e={ $_.Message -replace "[\r\n`t]+"," " | ForEach-Object { if ($_.Length -gt 1000) { $_.Substring(0,1000) } else { $_ } } }}
    } catch { }
}
$events | Export-Csv -Path (Join-Path $OutputDir "event_logs.csv") `
                     -NoTypeInformation -Encoding UTF8
Write-Host "   -> event_logs.csv ($(($events|Measure).Count) rows)"

# -------------------------------------------------------
# 2) 服务状态（所有 Automatic 启动的服务）
# -------------------------------------------------------
Write-Host "[2/6] Services ..."
$svcs = Get-CimInstance -ClassName Win32_Service |
    Where-Object { $_.StartMode -eq "Auto" } |
    Select-Object @{n='Server';e={$Server}},
                  Name, DisplayName, State, StartMode,
                  StartName, PathName, ProcessId,
                  @{n='DelayedAutoStart';e={$_.DelayedAutoStart}}
$svcs | Export-Csv -Path (Join-Path $OutputDir "services.csv") `
                   -NoTypeInformation -Encoding UTF8
Write-Host "   -> services.csv ($($svcs.Count) rows)"

# -------------------------------------------------------
# 3) 磁盘空间
# -------------------------------------------------------
Write-Host "[3/6] Disks ..."
$disks = Get-CimInstance -ClassName Win32_LogicalDisk |
    Where-Object { $_.DriveType -eq 3 } |
    Select-Object @{n='Server';e={$Server}},
                  DeviceID, VolumeName, FileSystem,
                  @{n='SizeGB';      e={[math]::Round($_.Size/1GB,2)}},
                  @{n='FreeGB';      e={[math]::Round($_.FreeSpace/1GB,2)}},
                  @{n='UsedGB';      e={[math]::Round(($_.Size-$_.FreeSpace)/1GB,2)}},
                  @{n='UsedPercent'; e={[math]::Round((1-$_.FreeSpace/$_.Size)*100,1)}}
$disks | Export-Csv -Path (Join-Path $OutputDir "disks.csv") `
                    -NoTypeInformation -Encoding UTF8
Write-Host "   -> disks.csv ($($disks.Count) rows)"

# -------------------------------------------------------
# 4) 补丁历史（最近 90 天）
# -------------------------------------------------------
Write-Host "[4/6] Hotfixes ..."
$hotfixes = Get-HotFix |
    Where-Object { $_.InstalledOn -gt (Get-Date).AddDays(-90) } |
    Select-Object @{n='Server';e={$Server}}, HotFixID, Description, InstalledOn, InstalledBy
$hotfixes | Export-Csv -Path (Join-Path $OutputDir "hotfixes.csv") `
                       -NoTypeInformation -Encoding UTF8
Write-Host "   -> hotfixes.csv ($($hotfixes.Count) rows)"

# -------------------------------------------------------
# 5) 内存 / CPU 快照
# -------------------------------------------------------
Write-Host "[5/6] System info ..."
$os  = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$sys = [PSCustomObject]@{
    Server            = $Server
    OSCaption         = $os.Caption
    OSVersion         = $os.Version
    LastBootUpTime    = $os.LastBootUpTime
    UptimeHours       = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours,1)
    TotalMemoryGB     = [math]::Round($os.TotalVisibleMemorySize/1MB,2)
    FreeMemoryGB      = [math]::Round($os.FreePhysicalMemory    /1MB,2)
    UsedMemoryPercent = [math]::Round((1-$os.FreePhysicalMemory/$os.TotalVisibleMemorySize)*100,1)
    CPUName           = $cpu.Name
    CPULogicalCores   = $cpu.NumberOfLogicalProcessors
    CPULoadPct        = $cpu.LoadPercentage
    CollectTime       = Get-Date
}
$sys | Export-Csv -Path (Join-Path $OutputDir "system_info.csv") `
                  -NoTypeInformation -Encoding UTF8
Write-Host "   -> system_info.csv"

# -------------------------------------------------------
# 6) 登录会话（帮助排查远程连接异常）
# -------------------------------------------------------
Write-Host "[6/6] Logon sessions ..."
try {
    $sessions = & quser.exe 2>$null | Select-Object -Skip 1 | ForEach-Object {
        $parts = ($_ -replace '\s{2,}', '|').Trim('|').Split('|')
        if ($parts.Count -ge 3) {
            [PSCustomObject]@{
                Server    = $Server
                Username  = $parts[0]
                SessionId = $parts[1]
                State     = $parts[2]
                LogonTime = $parts[-2]
                IdleTime  = $parts[-3]
            }
        }
    }
    if ($sessions) {
        $sessions | Export-Csv -Path (Join-Path $OutputDir "logon_sessions.csv") `
                               -NoTypeInformation -Encoding UTF8
        Write-Host "   -> logon_sessions.csv ($(($sessions|Measure).Count) rows)"
    }
} catch { Write-Host "   (quser not available)" }

Write-Host "DONE -> $OutputDir"
```

---

## 二、调度建议

| 任务 | 频率 | 说明 |
|---|---|---|
| `Collect-WindowsHealth.ps1` | **每日 03:00** | 默认回看 1 天事件；巡检周报可传 `-EventDays 7` |
| 异常临时抓取 | 随时 | 系统报警 / 服务器卡顿时立即执行 |

---

## 三、部署调度（任务计划程序）

在每台需采集的服务器管理员 PowerShell 执行：

```powershell
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\DBA\scripts\Collect-WindowsHealth.ps1'
$trigger = New-ScheduledTaskTrigger -Daily -At 3:00AM
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName 'InsAgent_Collect_WindowsHealth' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
```

> `Get-WinEvent` 需要对 Security 日志的额外权限，但默认脚本只读 System / Application，`SYSTEM` 账户即可。

### 手工触发 / 周报模式

```powershell
# 立即跑一次（默认今日、回看 1 天）
Start-ScheduledTask -TaskName 'InsAgent_Collect_WindowsHealth'

# 周报：拉近 7 天事件
pwsh C:\DBA\scripts\Collect-WindowsHealth.ps1 -EventDays 7
```

### 数据保留

- CSV 文件由 **README 统一的 `Clean-OldData.ps1`** 清理，默认 `windows_health` 保留 **30 天**。
- 自定义为 7 天：
  ```powershell
  powershell -File C:\DBA\scripts\Clean-OldData.ps1 -RetentionDays @{windows_health=7}
  ```

---

## 四、交付给 agent

```
D:\DBA\insagent-data\windows_health\YYYY-MM-DD\
    ├─ event_logs.csv
    ├─ services.csv
    ├─ disks.csv
    ├─ hotfixes.csv
    ├─ system_info.csv
    └─ logon_sessions.csv
```

拷贝到 agent 的 `$DATA_ROOT/windows_health/YYYY-MM-DD/`。

> 多台服务器并存时，每台机器在自己的输出目录独立采集，拷回 agent 时文件名带有 `Server` 列自动区分。可在 agent 侧按 `Server` 字段分机器分析。
