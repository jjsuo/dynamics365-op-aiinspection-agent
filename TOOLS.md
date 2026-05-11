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
│       ├── index_fragment.csv       ← 新文件名（代替老的 fragmentation.csv）
│       ├── missing_index.csv
│       └── table_size.csv
├── server_per_sql/
│   └── YYYY-MM-DD/
│       ├── perfmon_5min_YYYY-MM-DD.csv   ← ★ 服务器端 5 分钟时间桶聚合
│       ├── perfmon_daily_YYYY-MM-DD.csv  ← ★ 当日汇总
│       ├── slowsql_5min_YYYY-MM-DD.csv   ← ★ 藏在 server_per_sql？slow_sql 下设计选一
│       └── server_per_sql_baseline.json（可选）
├── slow_sql/
│   └── YYYY-MM-DD/
│       ├── slowsql_5min_YYYY-MM-DD.csv   ← ★ (BucketStart, SqlFingerprint) 时间桶
│       └── slowsql_daily_YYYY-MM-DD.csv  ← ★ 当日汇总
├── sql_blocking/
│   └── YYYY-MM-DD/
│       └── blocking_HHmm.csv            ← ★ 23 列，每 5 分钟一个快照
├── iis_logs/
│   └── YYYY-MM-DD/
│       ├── u_exYYMMDD*.log              ← ★ IIS W3C 日志原始文件
│       ├── apppool_status.csv           ← ★ 应用池状态快照
│       └── iis_worker_processes.csv     ← ★ w3wp 进程资源
├── windows_health/
│   └── YYYY-MM-DD/
│       ├── event_logs.csv               ← ★ System + Application 合并
│       ├── services.csv                 ← ★ 服务状态快照
│       ├── disks.csv                    ← ★ 磁盘空间
│       ├── hotfixes.csv                 ← ★ 补丁历史
│       ├── system_info.csv              ← ★ 系统基本信息
│       └── logon_sessions.csv           ← ★ 登录会话
└── plugin_scan/                         ← ★ 新增（每月 / 变更后采集）
    └── YYYY-MM-DD/
        ├── plugin_assemblies.csv
        ├── plugin_types.csv
        ├── plugin_steps.csv
        ├── plugin_images.csv
        ├── plugin_collect_info.csv
        └── plugin_dlls.zip              ← ★ data_reader 仅列内容，由 plugin_scanner 解压
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
- `plugin_scan`（新增，包含 ZIP）

任何新增 Skill 只需新增一个同名子目录即可自动生效，`data_reader.py` 无需改动。

### 其他缓存文件

- `memory/environment.json`（环境基准，最高优先级）
- `memory/data_catalog.json`（数据目录索引）
- `memory/servers.csv`（服务器清单）
- `memory/risk_profile.json`（风险画像）
- `memory/architecture.png / .pdf / .vsdx`（架构图，可选）

### 用户直接输入

- SQL 查询结果粘贴 / PowerShell 输出 / 手工整理数据
- Plugin `.cs` / `.dll` / `.zip` 文件（两种通道：（1）用户直接上传；（2）采集脚本落盘到 `plugin_scan/YYYY-MM-DD/plugin_dlls.zip`，data_reader 仅返回清单，由 `plugin_scanner` Skill 自行解压处理）

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
      "kind": "csv | w3c_log | json | zip",
      "total_rows": 1234,
      "columns": ["..."],
      "data": [ {"col": "val", "_date": "YYYY-MM-DD", "_file": "xxx.csv"}, "..." ]
    },
    {
      "file": "plugin_dlls.zip",
      "kind": "zip",
      "abs_path": "/abs/path/to/plugin_dlls.zip",
      "total_entries": 12,
      "total_size": 3456789,
      "entries": [{"name": "Contoso.Plugins.dll", "size": 123456}],
      "note": "archive listed but not extracted; Skill should extract if needed"
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
| Plugin扫描 / 插件代码检查 / 插件性能问题 | plugin_scan |

### 文件名约定（Skill 内识别）

`data_reader.py` 不解释文件语义；各 Skill 在收到 `files[]` 后按文件名关键字识别：

| category | 文件名关键字 | 数据类型 |
|----------|--------------|----------|
| sql_blocking | `blocking_*` / `blocking_HHmm*` | 阻塞快照（23 列） |
| slow_sql | `slowsql_5min*` | 5 分钟时间桶 × 指纹 |
| slow_sql | `slowsql_daily*` | 当日汇总 |
| sql_index | `index_usage*` | 索引使用率 |
| sql_index | `index_fragment*` / `fragment*` / `physical*` | 碎片率（新文件名） |
| sql_index | `missing_index*` / `missing*` | 缺失索引 |
| sql_index | `table_size*` / `rowcount*` | 表大小 |
| sql_index | `index_existing*`（静态） | 现有索引清单 |
| server_per_sql | `perfmon_5min*` | PerfMon 5 分钟聚合（新格式） |
| server_per_sql | `perfmon_daily*` | PerfMon 当日汇总 |
| server_per_sql | `perf*` / `serverper*` | PerfMon 老格式（兼容） |
| server_per_sql | `*stats*` / `*wait*` | 等待统计 |
| server_per_sql | `*baseline*.json` | 性能基线 |
| iis_logs | `*.log` / `u_ex*` / `iis_access*` | 访问日志（W3C） |
| iis_logs | `apppool_status*` | 应用池状态快照 |
| iis_logs | `iis_worker_processes*` | w3wp 进程资源 |
| iis_logs | `iis_summary*` / `app_pool*`（老格式） | 汇总 / 应用池事件 |
| windows_health | `event_logs*` | 事件日志（System+Application） |
| windows_health | `services*` | 服务状态 |
| windows_health | `disks*` | 磁盘空间 |
| windows_health | `hotfixes*` | 补丁历史 |
| windows_health | `system_info*` | 系统信息 |
| windows_health | `logon_sessions*` | 登录会话 |
| plugin_scan | `plugin_assemblies*` | 程序集元数据 |
| plugin_scan | `plugin_types*` | 插件类 |
| plugin_scan | `plugin_steps*` | 注册步骤 |
| plugin_scan | `plugin_images*` | Pre/Post 镜像 |
| plugin_scan | `plugin_collect_info*` | 采集元信息 |
| plugin_scan | `plugin_dlls.zip` | DLL 包（仅列表） |

---

## 四、Plugin 扫描（两步流程）

Plugin 扫描分两条通道：

1. **元数据 CSV 通道**：通过 `data_reader.py --category plugin_scan --today` 读取 `plugin_assemblies / plugin_types / plugin_steps / plugin_images` 等 CSV，分析注册配置合理性（同步/异步、Stage、FilteringAttributes）。
2. **DLL 静态扫描通道**：同当日目录下的 `plugin_dlls.zip`，data_reader 仅返回压缩包内文件清单（`kind=zip`），包含 `abs_path` 字段；`plugin_scanner` Skill 自行解压后进行反编译 / 规则匹配。

用户也可直接上传 `.cs` / `.dll` / `.zip`，此时不必经由 `data_reader`，Skill 直接处理文件路径。

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
