# 07 - Plugin 采集（Dynamics 365 OnPrem）

> **目的**：从 CRM 组织库（`*_MSCRM`）导出 Plugin 元数据（Assembly / Type / Step / Image），并把 Plugin 程序集对应的 `.dll` 文件打包，供 agent 的 `plugin_scanner` skill 做静态代码扫描 + 设计合规检查。
> **最终产物**：`D:\DBA\insagent-data\plugin_scan\YYYY-MM-DD\{plugin_assemblies.csv, plugin_types.csv, plugin_steps.csv, plugin_images.csv, plugin_dlls.zip}`。
>
> **注意**：这是目前唯一不走 `data_reader.py` 的 category（源码级扫描由 `plugin_scanner` skill 内置扫描器处理）。

---

## 一、SQL：导出 Plugin 元数据

保存为 `C:\DBA\scripts\Collect-PluginMetadata.sql`，**在 CRM 组织库执行**（例如 `hcrm_MSCRM`）。

```sql
-- 1) Assemblies（程序集，含二进制）
SELECT
    a.PluginAssemblyId,
    a.[Name]                AS AssemblyName,
    a.Culture,
    a.Version,
    a.PublicKeyToken,
    a.[IsolationMode]       AS IsolationMode,          -- 2=Sandbox, 1=None, 3=External
    a.SourceType,                                      -- 0=Database, 1=Disk, 2=Normal
    a.[Path],
    DATALENGTH(a.Content)   AS ContentSizeBytes,       -- 不取内容，仅看大小
    a.CreatedOn,
    a.ModifiedOn,
    a.CreatedBy,
    a.ModifiedBy
FROM dbo.PluginAssemblyBase a
WHERE a.IsManaged = 0 OR a.IsManaged IS NULL          -- 只看自定义（非系统托管）
ORDER BY a.ModifiedOn DESC;

-- 2) Types（插件类）
-- NOTE: 以单独的批次输出；PowerShell 端按顺序取结果集
SELECT
    t.PluginTypeId,
    t.PluginAssemblyId,
    t.[TypeName],
    t.[FriendlyName],
    t.[Name],
    t.[Description],
    t.IsWorkflowActivity,
    t.WorkflowActivityGroupName,
    t.CreatedOn,
    t.ModifiedOn
FROM dbo.PluginTypeBase t
ORDER BY t.PluginAssemblyId;

-- 3) Steps（注册步骤）
SELECT
    s.SdkMessageProcessingStepId AS StepId,
    s.[Name]                     AS StepName,
    s.PluginTypeId,
    s.SdkMessageId,
    m.[Name]                     AS MessageName,      -- Create / Update / Delete / …
    s.SdkMessageFilterId,
    f.PrimaryObjectTypeCode      AS PrimaryEntity,
    s.[Mode]                     AS [ExecMode],       -- 0=Sync, 1=Async
    s.[Stage]                    AS StageCode,        -- 10=PreValidation, 20=Pre, 40=Post
    s.[Rank],
    s.SupportedDeployment,                            -- 0=Server, 1=Online, 2=Both
    s.StateCode,                                       -- 0=Enabled
    s.[FilteringAttributes],
    s.Configuration,
    s.AsyncAutoDelete,
    s.CreatedOn,
    s.ModifiedOn
FROM dbo.SdkMessageProcessingStepBase s
LEFT JOIN dbo.SdkMessageBase       m ON m.SdkMessageId       = s.SdkMessageId
LEFT JOIN dbo.SdkMessageFilterBase f ON f.SdkMessageFilterId = s.SdkMessageFilterId
ORDER BY s.ModifiedOn DESC;

-- 4) Images（Pre/Post 镜像）
SELECT
    i.SdkMessageProcessingStepImageId AS ImageId,
    i.SdkMessageProcessingStepId      AS StepId,
    i.[Name]                          AS ImageName,
    i.EntityAlias,
    i.ImageType,                                      -- 0=Pre, 1=Post, 2=Both
    i.MessagePropertyName,
    i.[Attributes]                    AS ImageAttributes,
    i.CreatedOn,
    i.ModifiedOn
FROM dbo.SdkMessageProcessingStepImageBase i
ORDER BY i.SdkMessageProcessingStepId;
```

---

## 二、PowerShell：一键导出 + 打包 DLL

保存为 `C:\DBA\scripts\Collect-Plugin.ps1`。

```powershell
param(
    [string]$Server       = "localhost",
    [string]$OrgDatabase  = "hcrm_MSCRM",
    [string]$StatDate     = $(Get-Date -Format "yyyy-MM-dd"),
    [string]$RootDir      = "D:\DBA\insagent-data\plugin_scan",
    [string]$CrmServerBinDir = "C:\Program Files\Microsoft Dynamics CRM\Server\bin\assembly",
    # Plugin DLL 从 Disk 安装时的物理目录
    [switch]$DumpBinaryFromDB = $false      # 是否从 DB 中 dump Content 字段（大，慢）
)

$OutputDir = Join-Path $RootDir $StatDate
if (!(Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

# -------------------------------------------------------
# 1) 元数据 CSV
# -------------------------------------------------------
function Export-Rs($query, $outFile) {
    $rows = Invoke-Sqlcmd -ServerInstance $Server -Database $OrgDatabase `
                          -Query $query -QueryTimeout 0
    if ($rows) { $rows | Export-Csv -Path $outFile -NoTypeInformation -Encoding UTF8 }
    Write-Host "   -> $outFile ($(($rows|Measure).Count) rows)"
}

Write-Host "[1/3] Plugin metadata ..."
Export-Rs @"
SELECT PluginAssemblyId, Name AS AssemblyName, Culture, Version, PublicKeyToken,
       IsolationMode, SourceType, Path, DATALENGTH(Content) AS ContentSizeBytes,
       CreatedOn, ModifiedOn, CreatedBy, ModifiedBy
FROM dbo.PluginAssemblyBase
WHERE IsManaged = 0 OR IsManaged IS NULL
"@ (Join-Path $OutputDir "plugin_assemblies.csv")

Export-Rs @"
SELECT PluginTypeId, PluginAssemblyId, TypeName, FriendlyName, Name, Description,
       IsWorkflowActivity, WorkflowActivityGroupName, CreatedOn, ModifiedOn
FROM dbo.PluginTypeBase
"@ (Join-Path $OutputDir "plugin_types.csv")

Export-Rs @"
SELECT s.SdkMessageProcessingStepId AS StepId, s.Name AS StepName, s.PluginTypeId,
       m.Name AS MessageName, f.PrimaryObjectTypeCode AS PrimaryEntity,
       s.Mode AS ExecMode, s.Stage AS StageCode, s.[Rank], s.SupportedDeployment,
       s.StateCode, s.FilteringAttributes, s.Configuration, s.AsyncAutoDelete,
       s.CreatedOn, s.ModifiedOn
FROM dbo.SdkMessageProcessingStepBase s
LEFT JOIN dbo.SdkMessageBase       m ON m.SdkMessageId       = s.SdkMessageId
LEFT JOIN dbo.SdkMessageFilterBase f ON f.SdkMessageFilterId = s.SdkMessageFilterId
"@ (Join-Path $OutputDir "plugin_steps.csv")

Export-Rs @"
SELECT SdkMessageProcessingStepImageId AS ImageId, SdkMessageProcessingStepId AS StepId,
       Name AS ImageName, EntityAlias, ImageType, MessagePropertyName,
       Attributes AS ImageAttributes, CreatedOn, ModifiedOn
FROM dbo.SdkMessageProcessingStepImageBase
"@ (Join-Path $OutputDir "plugin_images.csv")

# -------------------------------------------------------
# 2) 打包自定义 DLL
# -------------------------------------------------------
Write-Host "[2/3] Package DLLs ..."
$dllStaging = Join-Path $OutputDir "_dll_staging"
New-Item -ItemType Directory -Path $dllStaging -Force | Out-Null

# 2a) 从 CRM Server 的 assembly 目录拷贝（IsolationMode = 1 / Disk 模式才有文件）
if (Test-Path $CrmServerBinDir) {
    Get-ChildItem -Path $CrmServerBinDir -Filter "*.dll" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch "^Microsoft\." -and $_.Name -notmatch "^System\." } |
        ForEach-Object {
            Copy-Item $_.FullName -Destination $dllStaging -Force
        }
}

# 2b) 从数据库中 dump Content 字段（Database 模式）—— 可选
if ($DumpBinaryFromDB) {
    Write-Host "   Dumping from DB (may be slow) ..."
    $asm = Invoke-Sqlcmd -ServerInstance $Server -Database $OrgDatabase -QueryTimeout 0 -Query @"
SELECT PluginAssemblyId, Name, Content
FROM dbo.PluginAssemblyBase
WHERE (IsManaged = 0 OR IsManaged IS NULL) AND SourceType = 0 AND Content IS NOT NULL
"@
    foreach ($a in $asm) {
        $f = Join-Path $dllStaging ("db_" + $a.Name + ".dll")
        [IO.File]::WriteAllBytes($f, [byte[]]$a.Content)
    }
}

# 2c) 打包 ZIP
$zip = Join-Path $OutputDir "plugin_dlls.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $dllStaging "*") -DestinationPath $zip -Force
Remove-Item $dllStaging -Recurse -Force
Write-Host "   -> $zip"

# -------------------------------------------------------
# 3) 版本记录
# -------------------------------------------------------
[PSCustomObject]@{
    CollectDate  = $StatDate
    Server       = $env:COMPUTERNAME
    OrgDatabase  = $OrgDatabase
    CrmBinDir    = $CrmServerBinDir
    DumpedFromDB = [bool]$DumpBinaryFromDB
    ZipSizeMB    = [math]::Round((Get-Item $zip).Length / 1MB, 2)
} | Export-Csv -Path (Join-Path $OutputDir "plugin_collect_info.csv") `
               -NoTypeInformation -Encoding UTF8

Write-Host "DONE -> $OutputDir"
```

---

## 三、调度建议

| 任务 | 频率 | 说明 |
|---|---|---|
| `Collect-Plugin.ps1` | **每月一次** 或 **变更后** | Plugin 变更低频，不必每天采集 |
| 发布新 Plugin 后立即触发一次 | 手动 | 确保最新 DLL 进入 zip |

---

## 四、部署调度（任务计划程序）

在 CRM Server / SQL Server 管理员 PowerShell 执行：

```powershell
# 每月 1 号 02:00 采集 Plugin 元数据 + 打包 DLL
# 任务计划没有原生「每月 N 日」选项，这里用 schtasks 创建（更简洁）
schtasks /create /F /TN 'InsAgent_Collect_Plugin' `
  /TR 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\DBA\scripts\Collect-Plugin.ps1' `
  /SC MONTHLY /D 1 /ST 02:00 /RU SYSTEM /RL HIGHEST
```

> `SYSTEM` 默认可 Trusted 连接本机 SQL；如 CRM 库在另一托管，修改 `Collect-Plugin.ps1` 的 `-Server` 或换带专用服务账户的任务。
> 如果需要从 DB 导出 Content 字段的 DLL（例如 `SourceType = 0`），添加参数 `-DumpBinaryFromDB`。

### 手工触发（发布后立即抓一次）

```powershell
Start-ScheduledTask -TaskName 'InsAgent_Collect_Plugin'
# 或带数据库 Content dump
pwsh C:\DBA\scripts\Collect-Plugin.ps1 -DumpBinaryFromDB
```

### 数据保留

- 日期子目录和 zip 由 **README 统一的 `Clean-OldData.ps1`** 清理，默认 `plugin_scan` 保留 **180 天**（约 6 个月，用于 Plugin 发布史比对）。
- Plugin DLL zip 如果较大（几百 MB）可调短：
  ```powershell
  powershell -File C:\DBA\scripts\Clean-OldData.ps1 -RetentionDays @{plugin_scan=90}
  ```
- 历史基线如果需要更长期保留，建议另外复制到持久化存档存储，而不依赖采集目录。

---

## 五、交付给 agent

```
D:\DBA\insagent-data\plugin_scan\YYYY-MM-DD\
    ├─ plugin_assemblies.csv
    ├─ plugin_types.csv
    ├─ plugin_steps.csv
    ├─ plugin_images.csv
    ├─ plugin_collect_info.csv
    └─ plugin_dlls.zip        # 解压后喂给 plugin_scanner
```

拷贝到 agent 的 `$DATA_ROOT/plugin_scan/YYYY-MM-DD/`。
`plugin_scanner` skill 触发时会：
1. 解压 `plugin_dlls.zip` 到临时目录
2. 反编译 / 静态扫描（查找：`RetrieveMultiple` 无条件、`IOrganizationService` 滥用、循环调用、未设 `FilteringAttributes` 的 Update 注册等反模式）
3. 结合 `plugin_steps.csv` 判断同步/异步/Stage 配置合理性
