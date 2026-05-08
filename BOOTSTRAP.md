# BOOTSTRAP

Agent 启动时按顺序执行以下步骤。

---

## Step 1：读取服务器清单

读取文件：memory/servers.csv

解析字段：
- hostname（主机名）
- ip（IP地址）
- role（角色：SQL/IIS/ASYNC/AD/ADFS/MQ/Portal）
- cpu_cores（CPU核数）
- memory_gb（内存GB）
- os（操作系统版本）
- sql_version（如适用）
- tags（如：primary / secondary / critical）

若文件不存在：提示用户先填写服务器清单，并提供 servers.csv 模板格式。

---

## Step 2：读取或初始化 environment.json

优先读取：memory/environment.json

若文件已存在：直接加载，作为本次分析的环境基准。

若文件不存在：根据 servers.csv 自动生成初始版本（见 Step 3）。

---

## Step 3：生成 / 更新 environment.json

根据服务器清单和已知信息，生成以下结构（修复原版重复 key 问题）：

```json
{
  "project": {
    "name": "Dynamics Production",
    "customer": "客户名称",
    "environment": "PROD",
    "crm_version": "Dynamics 365 9.x",
    "last_updated": "YYYY-MM-DD",
    "last_inspected": "YYYY-MM-DD"
  },
  "summary": {
    "sql_servers": 2,
    "app_servers": 2,
    "portal_servers": 1,
    "async_servers": 1,
    "mq_servers": 0,
    "ad_servers": 2,
    "adfs_servers": 1,
    "alwayson_enabled": true,
    "load_balancer_enabled": true,
    "total_servers": 9
  },
  "servers": [
    {
      "name": "SQL01",
      "role": ["SQL", "Primary"],
      "ip": "10.1.1.10",
      "os": "Windows Server 2019",
      "cpu_cores": 16,
      "memory_gb": 64,
      "disk": [
        {"drive": "C", "size_gb": 100, "usage": "OS"},
        {"drive": "D", "size_gb": 500, "usage": "Data"},
        {"drive": "L", "size_gb": 200, "usage": "Log"},
        {"drive": "T", "size_gb": 100, "usage": "TempDB"}
      ],
      "sql_version": "SQL Server 2019",
      "sql_edition": "Enterprise",
      "instance": "MSSQLSERVER",
      "max_server_memory_gb": 56,
      "tags": ["critical", "primary"]
    },
    {
      "name": "SQL02",
      "role": ["SQL", "Secondary"],
      "ip": "10.1.1.11",
      "os": "Windows Server 2019",
      "cpu_cores": 16,
      "memory_gb": 64,
      "sql_version": "SQL Server 2019",
      "sql_edition": "Enterprise",
      "tags": ["secondary"]
    },
    {
      "name": "APP01",
      "role": ["IIS", "CRM Frontend"],
      "ip": "10.1.1.20",
      "os": "Windows Server 2019",
      "cpu_cores": 8,
      "memory_gb": 32,
      "crm_website": true,
      "iis_version": "10.0",
      "tags": ["critical"]
    },
    {
      "name": "APP02",
      "role": ["IIS", "CRM Frontend"],
      "ip": "10.1.1.21",
      "os": "Windows Server 2019",
      "cpu_cores": 8,
      "memory_gb": 32,
      "crm_website": true,
      "iis_version": "10.0",
      "tags": []
    },
    {
      "name": "ASYNC01",
      "role": ["AsyncService"],
      "ip": "10.1.1.30",
      "os": "Windows Server 2019",
      "cpu_cores": 8,
      "memory_gb": 16,
      "tags": ["single-point-of-failure"]
    }
  ],
  "topology": {
    "load_balancer": {
      "enabled": true,
      "type": "NLB",
      "targets": ["APP01", "APP02"]
    },
    "sql_alwayson": {
      "enabled": true,
      "primary": "SQL01",
      "secondary": ["SQL02"],
      "sync_mode": "Synchronous",
      "auto_failover": true
    },
    "dependencies": [
      {"from": "APP01", "to": "SQL01", "type": "SQL"},
      {"from": "APP02", "to": "SQL01", "type": "SQL"},
      {"from": "ASYNC01", "to": "SQL01", "type": "SQL"}
    ]
  },
  "d365_config": {
    "crm_database": "MSCRM",
    "organization_count": 1,
    "plugin_profiler_enabled": false,
    "audit_enabled": true,
    "rcsi_enabled": true
  },
  "known_risks": [
    {
      "id": "RISK-001",
      "description": "AuditBase 历史数据持续增长，无归档策略",
      "severity": "P2",
      "first_seen": "YYYY-MM-DD"
    },
    {
      "id": "RISK-002",
      "description": "PrincipalObjectAccess 表数据量大，影响权限查询性能",
      "severity": "P2",
      "first_seen": "YYYY-MM-DD"
    },
    {
      "id": "RISK-003",
      "description": "ASYNC01 为单点，无冗余",
      "severity": "P2",
      "first_seen": "YYYY-MM-DD"
    }
  ],
  "performance_baseline": {
    "last_updated": "YYYY-MM-DD",
    "sql_ple_normal": 3000,
    "sql_cpu_avg_pct": 0,
    "sql_io_avg_ms": 0,
    "iis_avg_response_ms": 0
  },
  "inspection_history": []
}
```

---

## Step 4：读取架构图（如存在）

尝试读取以下文件：
- memory/architecture.png
- memory/architecture.pdf
- memory/architecture.vsdx

识别内容：
- SQL 集群拓扑
- IIS 层和负载均衡
- 网络分区
- 依赖关系

将识别结果补充到 environment.json topology 字段。

---

## Step 5：加载历史数据索引

通过 `tools/data_reader.py --list-categories` 和 `--list-dates` 扫描 `DATA_ROOT`
（默认 `<项目根>/data`，可由 `$DATA_ROOT` 或 `--data-root` 覆盖），
为每个 category 建立可用日期索引：

- `sql_blocking`    → 阻塞历史
- `slow_sql`        → 慢 SQL 历史
- `sql_index`       → 索引分析数据（含根目录静态 `index_existing.csv`）
- `server_per_sql`  → PerfMon 性能数据
- `iis_logs`        → IIS 日志
- `windows_health`  → Windows 健康数据

将索引信息写入 `memory/data_catalog.json`，供后续分析快速定位。

---

## Step 6：启动完成提示

输出启动摘要，例如：

```
D365-HealthGuard 启动完成

环境：[客户名] - PROD
服务器：SQL×2 / APP×2 / ASYNC×1
AlwaysOn：已启用（SQL01 主节点）
已知风险：3 项（P2×3）

可用历史数据：
  - 索引数据：最近 7 天
  - 性能数据：最近 5 天
  - 阻塞数据：最近 14 天
  - 慢SQL数据：最近 7 天

输入 "系统巡检" 获取完整健康报告
输入 "#SQL_BLOCK 分析今天阻塞" 进行专项分析
```
