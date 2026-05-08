# TOOLS

---

## 一、输入标签体系

Agent 接受以下结构化标签输入：

### SQL 相关
- #SQL_BLOCK     → 阻塞分析
- #TABLE_SIZE    → 大表/存储分析
- #INDEX_FRAGMENT / #INDEX → 索引分析
- #SLOW_SQL      → 慢 SQL 分析

### 性能相关
- #PERF / #PERFMON → SQL Server + Windows 性能分析

### 应用层
- #IIS            → IIS 健康检查
- #WINDOWS        → Windows Server 健康检查

### 代码分析
- #PLUGIN         → Plugin 代码扫描

### 综合
- 系统巡检 / 整体健康 / 系统现在有什么问题 → 全量巡检报告

---

## 二、数据目录规范（DATA_ROOT）

### 统一数据根目录

所有采集数据集中存放在一个根目录（`DATA_ROOT`）下，按「类别 / 日期」分层：

```
<DATA_ROOT>/
├── sql_blocking/
│   └── YYYY-MM-DD/
│       └── *.csv
├── slow_sql/
│   └── YYYY-MM-DD/
│       └── *.csv
├── sql_index/
│   ├── index_existing.csv           ← 静态文件（category 根目录，手动维护）
│   └── YYYY-MM-DD/
│       ├── index_usage.csv
│       ├── fragmentation.csv
│       ├── missing_index.csv
│       └── table_size.csv
├── server_per_sql/
│   └── YYYY-MM-DD/
│       ├── perf_counter_*.csv / serverper.csv
│       ├── server_per_sql_stats.csv
│       └── server_per_sql_baseline.json（可选）
├── iis_logs/
│   └── YYYY-MM-DD/
│       ├── iis_access_*.log
│       ├── iis_summary.csv
│       └── app_pool_events.csv
└── windows_health/
    └── YYYY-MM-DD/
        ├── event_log_system.csv
        ├── event_log_application.csv
        ├── service_status.csv
        └── windows_perf_summary.csv
```

### DATA_ROOT 解析顺序（优先级从高到低）

1. `--data-root <path>` CLI 参数
2. `$DATA_ROOT` 环境变量
3. 项目默认：`<项目根>/data`（即 `tools/` 的父目录下的 `data/`）

这一设计使脚本在 macOS、WSL、Windows 上均可直接运行；采集侧只需将数据落到同一根目录即可。

### 预置类别（category）

- `sql_blocking`
- `slow_sql`
- `sql_index`
- `server_per_sql`
- `iis_logs`
- `windows_health`

任何新增 Skill 只需新增一个同名子目录即可自动生效，`data_reader.py` 无需改动。

### 其他缓存文件

- `memory/environment.json`（环境基准，最高优先级）
- `memory/data_catalog.json`（数据目录索引）
- `memory/servers.csv`（服务器清单）
- `memory/risk_profile.json`（风险画像）
- `memory/architecture.png / .pdf / .vsdx`（架构图，可选）

### 用户直接输入

- SQL 查询结果粘贴 / PowerShell 输出 / 手工整理数据
- Plugin `.cs` / `.dll` / `.zip` 文件（由 `plugin_scanner` Skill 处理，不经由 `data_reader`）

---

## 三、统一数据读取工具

### 脚本

**`tools/data_reader.py`** —— 唯一的数据读取入口。
职责：纯读取（CSV / W3C `.log` / JSON 格式解析），**不做任何数据分析、聚合、阈值判断**。

### 命令

```bash
# 列出 DATA_ROOT 下所有已有的 category
python3 tools/data_reader.py --list-categories

# 列出某 category 下可用日期
python3 tools/data_reader.py --category <name> --list-dates

# 按时间维度读取
python3 tools/data_reader.py --category <name> --today
python3 tools/data_reader.py --category <name> --yesterday
python3 tools/data_reader.py --category <name> --last-3
python3 tools/data_reader.py --category <name> --last-7
python3 tools/data_reader.py --category <name> --last-30
python3 tools/data_reader.py --category <name> --date 2026-04-29
python3 tools/data_reader.py --category <name> --start 2026-04-20 --end 2026-04-29

# 自定义 DATA_ROOT
python3 tools/data_reader.py --category <name> --today --data-root /mnt/d/insagent-data
DATA_ROOT=/mnt/d/insagent-data python3 tools/data_reader.py --category <name> --today
```

### 返回结构

```json
{
  "status": "ok",
  "data_root": "/Users/.../data",
  "category": "slow_sql",
  "date_range": "YYYY-MM-DD ~ YYYY-MM-DD",
  "loaded":  ["YYYY-MM-DD/xxx.csv", "..."],
  "missing": ["YYYY-MM-DD", "..."],
  "files": [
    {
      "file": "xxx.csv",
      "date": "YYYY-MM-DD",
      "path": "slow_sql/YYYY-MM-DD/xxx.csv",
      "kind": "csv | w3c_log | json",
      "total_rows": 1234,
      "columns": ["..."],
      "data": [ {"col": "val", "_date": "YYYY-MM-DD", "_file": "xxx.csv"}, "..." ]
    }
  ],
  "static_files": [
    { "file": "index_existing.csv", "kind": "csv", "total_rows": 14080, "columns": ["..."], "data": ["..."] }
  ]
}
```

失败时返回：`{"status": "error", "error": "...", "available_dates": [...], "available_categories": [...]}`

### 语义 → category 路由

| 触发语义 | category |
|----------|----------|
| 今天阻塞 / 最近阻塞 / 阻塞趋势 / 某天 blocking | sql_blocking |
| 分析索引 / 索引使用率 / 索引碎片 / 缺失索引 / 索引健康检查 / 表数据量 | sql_index |
| 性能数据 / 服务器压力 / SQL Server 性能报告 / CPU内存磁盘瓶颈 / PerfMon | server_per_sql |
| 慢SQL / 慢查询 / SQL性能报告 | slow_sql |
| IIS健康 / 应用池状态 / 请求超时 / IIS日志 / 响应时间 | iis_logs |
| Windows健康 / 系统事件 / 服务状态 / 系统日志 | windows_health |

### 文件名约定（Skill 内识别）

`data_reader.py` 不解释文件语义；各 Skill 在收到 `files[]` 后按文件名关键字识别：

| category | 文件名关键字 | 数据类型 |
|----------|--------------|----------|
| sql_index | `index_usage*` | 索引使用率 |
| sql_index | `fragment*` / `physical*` | 碎片率 |
| sql_index | `missing*` | 缺失索引 |
| sql_index | `table_size*` / `rowcount*` | 表大小 |
| sql_index | `index_existing*`（静态） | 现有索引清单 |
| server_per_sql | `perf*` / `perf_counter_*` / `serverper*` | PerfMon 计数器 |
| server_per_sql | `*stats*` / `*wait*` | 等待统计 |
| server_per_sql | `*baseline*.json` | 性能基线 |
| iis_logs | `*.log` / `iis_access*` | 访问日志（W3C） |
| iis_logs | `iis_summary*` | 汇总 |
| iis_logs | `app_pool*` | 应用池事件 |
| windows_health | `event_log_system*` | 系统事件 |
| windows_health | `event_log_application*` | 应用事件 |
| windows_health | `service_status*` | 服务状态 |
| windows_health | `windows_perf*` / `perf_summary*` | 资源摘要 |

---

## 四、Plugin 扫描（独立）

Plugin 扫描分析源码而非采集 CSV，不经由 `data_reader.py`。由 `plugin_scanner` Skill 直接对用户上传的 `.cs` / `.zip` 文件或本地源码路径进行规则匹配。

---

## 五、缓存与记忆机制

### 初始化加载顺序

1. `memory/environment.json`（最高优先级）
2. `memory/data_catalog.json`（数据可用性索引）
3. `memory/servers.csv`（服务器清单）
4. 历史数据（按需 `data_reader.py` 加载）
5. 用户输入数据

### 缓存更新规则

分析结束后，以下情况必须更新 `environment.json`：

- 发现新风险 → 追加到 `known_risks`
- 服务器配置变化 → 更新 `servers` 字段
- 性能基线偏移 → 更新 `performance_baseline`
- 完整巡检 → 更新 `last_inspected` 和 `inspection_history`

### 数据优先级

`environment.json` > 历史数据 > 用户输入 > 推理补全

---

## 六、分析执行流程

```
用户输入
  → 识别标签或语义（见 AGENTS.md 路由表）
  → 加载 memory/environment.json
  → 加载 memory/data_catalog.json
  → 推断 category + 时间参数
  → python3 tools/data_reader.py --category <name> [时间参数]
  → 将 files[] / static_files[] 交给对应 Skill
  → Skill 负责分析、阈值、评分、关联
  → 输出结构化报告
  → 更新 environment.json（如有新发现）
```

---

## 七、错误处理

| 错误类型 | 处理方式 |
|----------|----------|
| `status=error` + `error=category 目录不存在` | 提示用户确认 DATA_ROOT 与 category 名；可用类别见 `available_categories` |
| `status=error` + `error=无可读数据` | 提示可用日期范围（来自返回的 `available_dates`） |
| CSV 字段缺失 | Skill 基于现有字段继续分析，标注缺失维度 |
| `environment.json` 不存在 | 执行 BOOTSTRAP，引导用户填写服务器信息 |
| 脚本执行失败 | 提示用户手动粘贴数据，切换到手动分析模式 |
