# SKILL: windows_health

---

## 定位

Windows Server + Dynamics 365 基础设施健康检查专家。

分析范围：Windows 服务状态、系统事件日志、资源使用、D365 相关服务健康。

---

## 输入触发

标签：#WINDOWS
语义：Windows健康 / 系统事件 / 服务状态 / 系统日志 / Windows错误 / 服务器健康

---

## 数据来源

### 模式 A：用户粘贴数据
- Get-Service 输出
- Get-EventLog 输出
- Windows 事件查看器导出

### 模式 B：本地文件自动读取
默认路径：`<DATA_ROOT>/windows_health/YYYY-MM-DD/`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

```
├── event_log_system.csv      （系统事件日志）
├── event_log_application.csv  （应用程序事件日志）
├── service_status.csv         （服务状态）
└── windows_perf_summary.csv   （系统资源摘要）
```

调用统一数据读取工具（只读取，不分析）：
```bash
python3 tools/data_reader.py --category windows_health --today
python3 tools/data_reader.py --category windows_health --date 2026-04-28
python3 tools/data_reader.py --category windows_health --last-7
python3 tools/data_reader.py --category windows_health --list-dates
```

本 Skill 按文件名关键字识别类型：
- `event_log_system*`       → 系统事件
- `event_log_application*`  → 应用程序事件
- `service_status*`         → 服务状态
- `windows_perf*` / `perf_summary*` → 资源摘要
- `disk*`                   → 磁盘空间

**数据读取后由本 Skill / AI 负责全部分析逻辑。**

---

## 检查维度

### 1. D365 关键服务状态

必须检查以下服务是否 Running：

**SQL Server 相关（SQL01 / SQL02）**
| 服务名 | 说明 |
|--------|------|
| MSSQLSERVER | SQL Server 主服务 |
| SQLSERVERAGENT | SQL Agent（作业调度） |
| MSSQLServerOLAPService | SSAS（如部署） |
| SQLBrowser | SQL Browser |

**Dynamics 365 相关（APP01 / APP02）**
| 服务名 | 说明 |
|--------|------|
| W3SVC | IIS Web 服务 |
| WAS | Windows Process Activation |
| MSCRMSandboxService | Sandbox 沙箱服务 |
| MSCRMAsyncService | Async 异步服务（ASYNC01） |
| MSCRMEmail | 邮件路由服务 |

**基础设施（AD / ADFS）**
| 服务名 | 说明 |
|--------|------|
| NTDS | Active Directory DS |
| KDC | Kerberos KDC |
| adfssrv | ADFS 服务 |
| DFS | 分布式文件系统（如使用） |

### 2. 系统事件日志检查

**P1 级别事件（立即告警）**
| EventID | 来源 | 含义 |
|---------|------|------|
| 41 | Kernel-Power | 非正常重启（蓝屏/断电） |
| 1001 | BugCheck | 系统崩溃（蓝屏） |
| 7034 | Service Control | 服务意外终止 |
| 7000 | Service Control | 服务无法启动 |
| 51 | Disk | 磁盘 IO 错误 |
| 11 | Disk | 磁盘控制器错误 |
| 4 | K57 | 磁盘 SCSI 错误 |

**P2 级别事件（需关注）**
| EventID | 来源 | 含义 |
|---------|------|------|
| 1000 | Application Error | 应用程序崩溃 |
| 1026 | .NET Runtime | .NET 运行时错误（Plugin 崩溃） |
| 6008 | EventLog | 非正常关机记录 |
| 5858 | WinRM | WinRM 连接错误 |
| 36888 | SChannel | SSL/TLS 错误（影响 ADFS / HTTPS） |

**D365 特定事件**
| EventID | 来源 | 含义 |
|---------|------|------|
| 任意 | MSCRMAsyncService | Async 服务错误 |
| 任意 | MSCRMSandboxService | Sandbox 崩溃（Plugin 错误） |
| 任意 | W3SVC | IIS 服务错误 |

### 3. 系统资源检查

**磁盘空间**
| 驱动器 | 警告阈值 | 危险阈值 |
|--------|----------|----------|
| C:（系统盘） | < 10GB | < 5GB |
| D:（数据盘） | < 50GB | < 20GB |
| L:（日志盘） | < 30GB | < 10GB |
| T:（TempDB盘） | < 20GB | < 5GB |

**系统内存**
- 可用内存 < 2GB → P1（可能导致 SQL Buffer Pool 收缩）
- 可用内存 < 4GB → P2

**系统 CPU**
- 非 SQL 进程 CPU > 20% → P2（检查是否有异常进程）
- 系统总 CPU 持续 > 90% → P1

### 4. Windows 更新与安全状态
- 待安装补丁数量
- 上次更新日期（> 90天未更新为 P3）
- 防火墙状态
- 时间同步状态（Kerberos 依赖时间同步，偏差 > 5分钟会导致认证失败）

---

## 分析流程

### Step 1：服务状态扫描
- 列出所有 D365 关键服务状态
- 标记 Stopped / StartPending 状态的服务
- 检查服务恢复策略配置

### Step 2：事件日志分析
- 扫描最近 24 小时的 P1/P2 事件
- 对频繁出现的事件进行聚合统计
- 关联 D365 服务崩溃与应用池重启时间

### Step 3：资源状态检查
- 各服务器磁盘空间（结合 environment.json 磁盘配置）
- 内存可用量
- 异常进程 CPU 占用

### Step 4：跨服务器关联
- 结合 environment.json 拓扑，检查各服务器是否同步出现问题
- 例如：ADFS 错误 → 影响所有 APP 服务器认证

---

## 输出结构

```
## Windows Server 健康报告

### 服务器概况（来自 environment.json）
SQL01 (10.1.1.10) / SQL02 (10.1.1.11)
APP01 (10.1.1.20) / APP02 (10.1.1.21)
ASYNC01 (10.1.1.30)

### 健康评分
| 服务器 | 服务状态 | 事件日志 | 磁盘空间 | 综合 |
|--------|----------|----------|----------|------|
| SQL01 | ✅ | ⚠️ | ✅ | 85 |
| APP01 | 🔴 | ⚠️ | ✅ | 55 |

### 关键问题

[P1] APP01 - MSCRMSandboxService 已停止
  发现时间：2026-04-30 09:14
  影响：所有同步 Plugin 无法执行，用户操作报错
  建议：立即启动服务，检查 Windows 事件日志确认停止原因
  命令：Start-Service MSCRMSandboxService

[P1] SQL01 - 磁盘 L:（日志盘）仅剩 8GB（危险阈值 10GB）
  影响：SQL Server 事务日志无法增长，可能导致数据库不可写
  建议：立即清理旧日志备份文件或扩容日志盘

[P2] APP02 - EventID 1026 出现 3 次（.NET 运行时错误）
  时间：09:15 / 09:32 / 10:04
  相关 Plugin：ContactPlugin v2.1.0
  建议：检查 Plugin 代码异常处理，开启 Plugin Trace 日志定位

### 优化建议
P1：...
P2：...
P3：...
```

---

## 辅助 PowerShell 脚本

```powershell
# 检查 D365 关键服务状态
$d365Services = @(
    'MSSQLSERVER','SQLSERVERAGENT',
    'W3SVC','WAS',
    'MSCRMSandboxService','MSCRMAsyncService'
)
Get-Service -Name $d365Services | Select-Object Name, Status, StartType

# 检查近24小时错误事件
Get-EventLog -LogName Application -EntryType Error -Newest 50 |
    Select-Object TimeGenerated, Source, EventID, Message |
    Export-Csv event_log_application.csv -Encoding UTF8

# 检查磁盘空间
Get-PSDrive -PSProvider FileSystem |
    Select-Object Name,
        @{N='Used_GB';E={[math]::Round($_.Used/1GB,2)}},
        @{N='Free_GB';E={[math]::Round($_.Free/1GB,2)}}
```
