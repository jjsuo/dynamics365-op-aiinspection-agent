# D365-HealthGuard

> Dynamics 365 On-Premises 健康巡检分析 Agent（小龙虾 🦞 Agent）

基于「结构化输入 + 本地环境缓存 + 历史数据 + Skill 体系」的 Dynamics 365 On-Premises AIOps 分析引擎。
对 SQL Server、IIS、Windows Server、Plugin 代码等进行性能、稳定性与架构分析。

运行环境：Windows Server 上的 **WSL**。

---

## 核心设计

- **一个数据根目录**（`DATA_ROOT`）集中存放所有采集数据，按 `类别 / 日期` 分层。
- **一个统一读取脚本**（`tools/data_reader.py`）负责纯 I/O，不做任何分析、聚合、阈值判断。
- **所有分析逻辑交给 Skill**（`skills/<name>/SKILL.md`），每个 Skill 消费对应 category 的数据。
- **Agent 运行规则**（`AGENTS.md`）+ 启动流程（`BOOTSTRAP.md`）+ 工具契约（`TOOLS.md`）+ 环境基准（`memory/environment.json`）构成完整的 Agent 上下文。

---

## 目录结构

```
.
├── AGENTS.md                 # Agent 角色、路由规则、输出规范（强约束）
├── BOOTSTRAP.md              # 启动流程
├── TOOLS.md                  # 数据目录规范与统一读取工具契约
├── IDENTITY.md               # Agent 身份与专业能力
├── SOUL.md                   # 分析方法论
├── HEARTBEAT.md              # 定期自检规则
├── USER.md                   # 用户输入指南
├── tools/
│   └── data_reader.py        # 统一数据读取脚本（纯 I/O，不做分析）
├── skills/                   # 分析 Skill 集合
│   ├── health_report/        # 系统综合巡检报告（聚合）
│   ├── sql_block_analysis/   # SQL 阻塞分析
│   ├── slow_sql_analyst/     # 慢 SQL 分析
│   ├── sql_index_optimizer/  # 索引健康分析
│   ├── sql_storage_analysis/ # 大表 / 存储分析
│   ├── sql_perf_analyzer/    # SQL Server + Windows 性能基线
│   ├── iis_analysis/         # IIS 健康
│   ├── windows_health/       # Windows Server 健康
│   └── plugin_scanner/       # Plugin 代码扫描
├── memory/
│   ├── environment.json      # 环境基准（最高优先级）
│   ├── data_catalog.json     # 数据目录索引
│   ├── risk_profile.json     # 风险画像
│   └── servers.csv           # 服务器清单
└── data/                     # 默认 DATA_ROOT（被 .gitignore 忽略，不进仓库）
    ├── sql_blocking/YYYY-MM-DD/*.csv
    ├── slow_sql/YYYY-MM-DD/*.csv
    ├── sql_index/
    │   ├── index_existing.csv      # 静态文件（category 根目录）
    │   └── YYYY-MM-DD/*.csv
    ├── server_per_sql/YYYY-MM-DD/*
    ├── iis_logs/YYYY-MM-DD/*
    └── windows_health/YYYY-MM-DD/*
```

---

## 数据根目录（DATA_ROOT）

`DATA_ROOT` 解析优先级（从高到低）：

1. `--data-root <path>` CLI 参数
2. `$DATA_ROOT` 环境变量
3. 项目默认：`<repo>/data`

### WSL 推荐配置

Windows 侧采集脚本把数据写到 `D:\insagent-data\`，WSL 侧 agent 通过 `/mnt/d/insagent-data/` 读取同一份数据，无需复制：

```bash
echo 'export DATA_ROOT=/mnt/d/insagent-data' >> ~/.bashrc
source ~/.bashrc
```

---

## 统一数据读取工具

```bash
# 列出所有可用 category
python3 tools/data_reader.py --list-categories

# 列出某 category 下的可用日期
python3 tools/data_reader.py --category slow_sql --list-dates

# 按时间维度读取（多种语法）
python3 tools/data_reader.py --category slow_sql --today
python3 tools/data_reader.py --category slow_sql --yesterday
python3 tools/data_reader.py --category slow_sql --last-3
python3 tools/data_reader.py --category slow_sql --last-7
python3 tools/data_reader.py --category slow_sql --last-30
python3 tools/data_reader.py --category slow_sql --date 2026-04-29
python3 tools/data_reader.py --category slow_sql --start 2026-04-20 --end 2026-04-29

# 临时指定 DATA_ROOT
python3 tools/data_reader.py --category sql_index --today --data-root /mnt/d/insagent-data
```

### 返回结构（示例）

```json
{
  "status": "ok",
  "data_root": "/mnt/d/insagent-data",
  "category": "slow_sql",
  "date_range": "2026-04-29 ~ 2026-04-29",
  "loaded":  ["2026-04-29/slowsql0429.csv"],
  "missing": [],
  "files": [
    {
      "file": "slowsql0429.csv",
      "date": "2026-04-29",
      "path": "slow_sql/2026-04-29/slowsql0429.csv",
      "kind": "csv",
      "total_rows": 1234,
      "columns": ["..."],
      "data": [ {"...": "...", "_date": "2026-04-29", "_file": "slowsql0429.csv"} ]
    }
  ],
  "static_files": []
}
```

脚本只做格式解析（CSV / W3C `.log` / JSON），**不做任何数据分析、聚合、阈值判断**。所有分析逻辑在 Skill 中完成。

---

## Skill 列表

| Skill | 触发语义 | 对应 category |
|-------|----------|---------------|
| sql_block_analysis    | 今天阻塞 / 最近阻塞 / 阻塞趋势           | `sql_blocking` |
| slow_sql_analyst      | 慢 SQL / 慢查询 / SQL 性能报告           | `slow_sql` |
| sql_index_optimizer   | 索引使用率 / 碎片 / 缺失索引 / 表数据量  | `sql_index` |
| sql_storage_analysis  | 大表 / 表膨胀 / 存储分析                 | `sql_index`（复用 `table_size*.csv`） |
| sql_perf_analyzer     | PerfMon / 服务器压力 / CPU内存磁盘瓶颈   | `server_per_sql` |
| iis_analysis          | IIS 健康 / 应用池 / 请求队列             | `iis_logs` |
| windows_health        | Windows 健康 / 系统事件 / 服务状态       | `windows_health` |
| plugin_scanner        | Plugin 代码扫描 / 插件性能问题           | （源码，不走 data_reader） |
| health_report         | 系统巡检 / 整体健康 / 跨层关联分析       | （聚合以上全部） |

---

## 快速开始

### 1. 依赖

```bash
python3 --version   # 3.8+
python3 -m pip install --user --break-system-packages pandas
```

### 2. 准备数据

在 `$DATA_ROOT` 下按约定目录结构放入采集 CSV，或直接把真实数据放到 `data/` 走默认路径。

### 3. 验证读取

```bash
python3 tools/data_reader.py --list-categories
python3 tools/data_reader.py --category slow_sql --list-dates
```

### 4. 在 Agent 中触发分析

直接用自然语言：
- `系统巡检` → 输出完整健康报告
- `今天有没有阻塞` → SQL 阻塞分析
- `最近7天慢SQL` → 慢 SQL 分析
- `索引健康怎么样` → 索引分析

或使用标签：`#SQL_BLOCK` / `#SLOW_SQL` / `#INDEX` / `#PERF` / `#IIS` / `#WINDOWS` / `#PLUGIN` / `#TABLE_SIZE`。

---

## 扩展：新增数据类别

1. 在 `$DATA_ROOT` 下建立 `<新类别>/YYYY-MM-DD/` 目录并放入 CSV。
2. 新建 `skills/<新类别>/SKILL.md` 描述分析规则。
3. 在 `AGENTS.md` 路由表里登记触发语义 → category 映射。

**不需要改 `data_reader.py`**。

---

## 约束（强制）

- 不允许凭空猜测；数据不足时必须提示缺少什么
- 不允许忽略 `environment.json` 中的服务器拓扑与配置
- 不允许只做单点分析，必须结合历史趋势
- 所有 SQL 脚本必须包含 `GO` 分隔符和注释
- 读取脚本永远不做业务分析

---

## License

内部项目，暂未设置开源 License。
