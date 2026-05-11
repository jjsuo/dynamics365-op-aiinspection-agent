# SKILL: sql_perf_analyzer

---

## 定位

SQL Server + Windows Server 性能基线分析专家（D365 On-Premises 专项优化）。

分析范围：PerfMon 多维度计数器、等待统计、性能基线评分、瓶颈定位。

---

## 输入触发

标签：#PERF / #PERFMON
语义：分析性能数据 / 服务器压力 / SQL Server 性能报告 / 内存CPU磁盘瓶颈 / PerfMon分析 / 性能基线

---

## 数据来源

### 模式 A：用户上传文件
- `perfmon_5min_*.csv` / `perfmon_daily_*.csv`（新采集脚本产物）
- PerfMon CSV / BLG 文件（原始格式，向后兼容）
- server_per_sql_stats.csv（等待统计）

### 模式 B：本地目录自动读取（默认）

路径：`<DATA_ROOT>/server_per_sql/YYYY-MM-DD/`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

新采集脚本产出格式（`ServerCollectionScript/01_SQLServer性能指标采集.md`）：
```
├── perfmon_5min_YYYY-MM-DD.csv    ← ★ 主分析数据（5 分钟桶聚合，跨源 JOIN 键）
└── perfmon_daily_YYYY-MM-DD.csv   ← 日汇总（长期趋势 / 基线对比）
```

老格式（向后兼容，若存在仍支持）：
```
├── perf_counter_*.csv / serverper.csv   （PerfMon 原始计数器采样）
├── server_per_sql_stats.csv             （SQL 等待统计）
└── server_per_sql_baseline.json         （性能基线摘要）
```

调用统一数据读取工具（只读取，不分析）：
```bash
python3 tools/data_reader.py --category server_per_sql --today
python3 tools/data_reader.py --category server_per_sql --yesterday
python3 tools/data_reader.py --category server_per_sql --last-3
python3 tools/data_reader.py --category server_per_sql --last-7
python3 tools/data_reader.py --category server_per_sql --date 2026-04-28
python3 tools/data_reader.py --category server_per_sql --start 2026-04-20 --end 2026-04-28
python3 tools/data_reader.py --category server_per_sql --list-dates   # 查看可用日期
```

脚本返回结构（详见 TOOLS.md）：
- `files`：当日目录下所有 CSV / JSON；本 Skill 按文件名识别：
  - `perfmon_5min_*.csv` → ★ 主分析数据（5 分钟桶聚合）
  - `perfmon_daily_*.csv` → 日汇总（趋势 / 基线）
  - `perf_counter_*.csv` / `serverper*.csv` → 老格式原始采样（兼容）
  - `*stats*.csv` / `*wait*.csv` → SQL 等待统计
  - `*baseline*.json` → 性能基线摘要
- 新格式下聚合已在服务器侧完成（Avg / Max / Min / P95 / Samples），本 Skill 直接在聚合值上做阈值判断 + 评分。仅老格式原始采样需本 Skill 再按 CounterPath 再聚合。

**数据读取后由本 Skill / AI 负责全部分析逻辑**，包括：计数器维度分类、聚合、阈值判断、评分、瓶颈定位。

---

## ★ 跨源关联数据（强制加载）

**sql_perf_analyzer 是跨源分析的发起点**：本 Skill 不仅独立评分，还必须在识别到尖峰后反向引用 slowsql / blocking / iis 数据，给出「谁导致了这个尖峰」的根因解释。

### 必加载清单

| 文件 | 用途 | 触发规则 |
|------|------|----------|
| memory/environment.json | 服务器基线（cpu_cores / memory_gb / disk 类型） | 评估 Queue Length / PLE / Disk Latency 的基准 |
| memory/sql_config.json | SQL 实际配置快照（max_server_memory / MAXDOP / tempdb 文件数 / Trace Flags） | 结合实际值判断配置是否合理，而非只看计数器 |
| memory/thresholds.json | 所有阈值集中来源 | 本文下方所有阈值表**只作为默认参考**，实际生效以 `thresholds.sql_performance` + `thresholds.slow_sql` + `thresholds.sql_blocking` 为准 |
| memory/business_context.json | peak_hours / maintenance_windows / batch_jobs | 判断尖峰是「真异常」还是「业务高峰/维护窗口内预期行为」 |
| memory/risk_profile.json | watch_list / known_risks | 命中历史高风险服务器 → 升级 |

### 跨源数据（识别尖峰后必加载）

当发现 `%Processor Time P95 > 阈值` / `PLE < 阈值` / `Disk sec/Read > 阈值` / `Memory Grants Pending > 0` 等任一异常时，**必须**按该尖峰的 `BucketStart` 回查：

```bash
# 1. 同一 5 分钟桶的 TOP CPU/IO 慢 SQL
python3 tools/data_reader.py --category slow_sql --date <YYYY-MM-DD>
  → 过滤 slowsql_5min_*.csv WHERE BucketStart = <尖峰桶>
  → ORDER BY TotalWorkerTime DESC LIMIT 10

# 2. 同一时段的阻塞事件
python3 tools/data_reader.py --category sql_blocking --date <YYYY-MM-DD>
  → 过滤 blocking_HHmm.csv WHERE BlockingStartTime 落在 [BucketStart, BucketStart+5min]

# 3. 同一时段的 IIS 慢请求（若存在）
python3 tools/data_reader.py --category iis_logs --date <YYYY-MM-DD>
  → W3C 日志按 FLOOR(time_min/5)*5 归桶
  → 过滤该桶的 time-taken > SLA 请求
```

> 跨源证据链是根因定位的必选项。尖峰没有对应的 SQL/阻塞/请求证据 → 标注「仅 PerfMon 观测到，根因不明，需在下一次尖峰时开启 Profiler/XEvent 抓取」。

---

## Step 1：数据获取与服务器配置加载

### 1.1 加载服务器物理配置（environment.json）

| 字段 | 用途 |
|------|------|
| cpu_cores | 评估 CPU 压力阈值（Queue Length 基准 = cores×2） |
| memory_gb | 评估 SQL Buffer Pool 配置是否合理 |
| disk（类型） | 评估磁盘延迟是否正常（HDD vs SSD 用不同阈值） |
| sql_version | 判断已知 bug / 特性支持 |
| max_server_memory_gb | 与 Target Server Memory 对比 |

### 1.2 加载 SQL 实际配置（sql_config.json）★ 新增

从 `sql_config.json` 读取当前运行值，与推荐值比对：

| 配置项 | 推荐值（D365） | 在哪里检查 |
|--------|----------------|------------|
| max server memory (MB) | OS 内存 × 80%（保留给 OS 至少 4GB） | configurations.max_server_memory_mb |
| MAXDOP | 1（D365 官方推荐） | configurations.max_degree_of_parallelism |
| cost threshold for parallelism | 50 | configurations.cost_threshold_for_parallelism |
| TempDB 文件数 | = 逻辑 CPU 数，最多 8，且大小均衡 | tempdb.files / tempdb.balanced |
| RCSI | ON（D365 组织库必须） | databases[].is_read_committed_snapshot_on |

若 sql_config.json 缺失或字段为空 → 提示用户执行 `ServerCollectionScript/08_环境上下文采集.md`。

### 1.3 加载阈值（thresholds.json）★ 新增

所有计数器阈值**必须**从 `thresholds.sql_performance` 读取。本文下方的阈值表格仅是默认模板，实际生效以 JSON 为准。若 thresholds.json 缺失 → 使用下表默认值并在报告中标注 `[DEFAULT-THRESHOLD]`。

### 1.4 加载业务上下文（business_context.json）★ 新增

- `peak_hours` → 尖峰时间落在此区间 → 判定为 **业务高峰期异常**（严重）
- `off_peak_hours` → 尖峰落在此区间 → 判定为 **非高峰期异常**（更需警惕，说明是非业务驱动）
- `maintenance_windows` → 尖峰落在此区间 → 判定为 **维护窗口预期行为**（降级或抑制告警）
- `batch_jobs` → 尖峰时间匹配某 batch job 的 schedule → 标注 `[BATCH-JOB: <名称>]`，降级

若配置缺失，在报告末尾列出"需补充的配置信息"。

---

## 新格式 CSV 字段说明（★ 优先使用）

### perfmon_5min_*.csv（5 分钟桶）

| 字段 | 说明 |
|------|------|
| ServerName    | 服务器名（多机时通过此字段分流） |
| CounterPath   | 计数器名称（如 `\Processor(_Total)\% Processor Time`） |
| BucketStart   | 整 5 分钟时间戳，★ 跨源 JOIN 键（与 `slowsql_5min.BucketStart` / `blocking_HHmm` / IIS 小时桋对齐） |
| AvgValue      | 该桶内平均值 |
| MaxValue      | 该桶内峰值 |
| MinValue      | 该桶内最小值 |
| P95Value      | 该桶内 P95 |
| Samples       | 该桶内采样数（服务器侧采样间隔 30s，5 分钟桶通常 = 10） |

### perfmon_daily_*.csv（日汇总）
```
ServerName, CounterPath, AvgValue, MaxValue, MinValue, MaxTime, SummaryDate, TotalSamples
```
`MaxTime` = 当日峰值出现时间，用于快速定位问题时刻。

---

## Step 2：指标解析与阈值判断

**新格式（5min 桶）工作流**：按 `(ServerName, CounterPath)` 分组，直接在服务器侧已算好的 AvgValue / P95Value / MaxValue 上做阈值判断：
- 均值 ≈ AVG(AvgValue)
- P95  ≈ MAX(P95Value)（稳健近似）或 AVG(P95Value)
- 峰值 = MAX(MaxValue)
- 超阈桶占比 = `COUNT(AvgValue > 阈值) / COUNT(*)`
- **尖峰时刻** = `ARGMAX(MaxValue)` 对应的 BucketStart（★ 用于与慢 SQL / 阻塞数据对齐）

**老格式**：对每个计数器计算：**均值 / P95 / 峰值 / 超阈值占比(%)**

### 2.1 内存（Memory）

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Available MBytes | > 1024 MB | 512–1024 MB | < 512 MB |
| Pages/sec | < 20 | 20–100 | > 100 |
| Page Reads/sec | < 5 | 5–20 | > 20 |
| Page Writes/sec | < 5 | 5–20 | > 20 |

诊断逻辑：Pages/sec 高 + Available MBytes 低 → 物理内存不足或 SQL max memory 设置过高，OS 被压缩。

### 2.2 CPU（Processor / System）

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| % Processor Time | < 70% | 70–85% | > 85% |
| % Privileged Time | < 15% | 15–25% | > 25% |
| Processor Queue Length | < 核数×2 | 核数×2~4 | > 核数×4 |
| Context Switches/sec | < 5000/核 | 5000–10000/核 | > 10000/核 |

诊断逻辑：Privileged Time 高 → 内核态开销大，常见于大量磁盘 I/O 或网络中断；Queue Length 持续 > 2×核数 → CPU 是瓶颈。

### 2.3 磁盘（PhysicalDisk）

| 计数器 | HDD 正常 | SSD 正常 | 危险（通用） |
|--------|----------|----------|--------------|
| Avg. Disk sec/Read | < 20ms | < 2ms | > 50ms |
| Avg. Disk sec/Write | < 20ms | < 2ms | > 50ms |
| Avg. Disk Queue Length | < 2 | < 2 | > 4 |
| Disk Read Bytes/sec | < 带宽×70% | < 带宽×70% | 持续达到上限 |
| Disk Write Bytes/sec | < 带宽×70% | < 带宽×70% | 持续达到上限 |

诊断逻辑：Avg. Disk sec/Read > 50ms 且 Queue Length > 4 → 磁盘 IO 瓶颈，检查是否存在大量全表扫描或 TempDB 争用。

### 2.4 SQL Buffer Manager

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Page life expectancy (PLE) | > 300s（推荐 > 1000s） | 100–300s | < 100s |
| Page reads/sec | < 90 | 90–200 | > 200 |
| Lazy writes/sec | ≈ 0 | 1–20 | > 20 |
| Checkpoint pages/sec | 低且平稳 | 偶发峰值 | 持续 > 1000 |

PLE 公式参考：理想值 = (Buffer Pool GB × 1000) / 4。例如 64GB Buffer Pool → 基准 PLE = 16000s。

诊断逻辑：PLE 低 + Lazy writes 高 → Buffer Pool 频繁换页，SQL Server 内存不足。

### 2.5 SQL Access Methods

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Full Scans/sec | < 1 | 1–10 | > 10 |
| Index Searches/sec | 高（Full Scans 比值大） | — | Full Scans 占比 > 5% |
| Page Splits/sec | < 20 | 20–100 | > 100 |
| Worktables Created/sec | < 10 | 10–50 | > 50 |

诊断逻辑：Full Scans 高 → 缺失索引；Page Splits 高 → FILLFACTOR 过高；Worktables 高 → 大量排序/哈希，需 TempDB 调优。

### 2.6 SQL General Statistics

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| User Connections | < max×70% | 70–90% | > 90% |
| Logins/sec | < 2 | 2–10 | > 10 |
| Active Temp Tables | < 100 | 100–500 | > 500 |

### 2.7 SQL Statistics（编译与重编译）

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| SQL Compilations/sec | < Batch×10% | 10–20% | > 20% |
| SQL Re-Compilations/sec | < Compilations×10% | 10–20% | > 20% |

诊断逻辑：Re-Compilations 高 → 统计信息过期或 SET 选项不一致。

### 2.8 SQL Locks

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Lock Waits/sec | < 1 | 1–10 | > 10 |
| Lock Wait Time (ms) | < 500ms avg | 500–2000ms | > 2000ms |
| Number of Deadlocks/sec | 0 | 偶发 < 0.1 | > 0.1 |

诊断逻辑：死锁 > 0 → 启用 Trace Flag 1222 或 Extended Events 抓取死锁图。

### 2.9 SQL Latches

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Latch Waits/sec | < 100 | 100–500 | > 500 |
| Average Latch Wait Time (ms) | < 10ms | 10–50ms | > 50ms |

诊断逻辑：Latch Wait 高通常伴随 Page Splits 高，指向 TempDB 或热点页争用。检查 TempDB 文件数（推荐 = 逻辑 CPU 数，最多 8）。

### 2.10 SQL Memory Manager

| 计数器 | 正常 | 说明 |
|--------|------|------|
| Total Server Memory (KB) | 接近 Target | 实际使用量 |
| Target Server Memory (KB) | 接近 max server memory | 目标上限 |
| Memory Grants Pending | 0 | > 0 持续出现 → 大查询内存排队 |

### 2.11 SQL Databases

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Log Flush Wait Time | < 5ms | 5–15ms | > 15ms |
| Percent Log Used | < 70% | 70–85% | > 85% |

诊断逻辑：Log Flush Wait 高 → 事务日志 IO 瓶颈，日志文件应放在独立高速磁盘。

### 2.12 TempDB

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Free Space in tempdb (KB) | > 总空间10% | 5–10% | < 5% |
| Longest Transaction Running Time | < 60s | 60–300s | > 300s |

诊断逻辑：D365 默认开启 RCSI，TempDB 版本存储开销远高于普通 SQL 应用，需特别关注。

### 2.13 网络（Network Interface）

| 计数器 | 正常 | 警告 | 危险 |
|--------|------|------|------|
| Bytes Received/sec | < 网卡带宽×70% | 70–85% | > 85% |
| Bytes Sent/sec | < 网卡带宽×70% | 70–85% | > 85% |

---

## Step 3：综合评分

维度权重（D365 场景）：

| 维度 | 权重 |
|------|------|
| 内存（OS + SQL） | 25% |
| CPU | 20% |
| 磁盘 IO | 25% |
| SQL 锁/并发 | 15% |
| SQL 编译/缓存 | 10% |
| TempDB / 网络 | 5% |

评分标准：
- 90–100 = 🟢 健康
- 70–89  = 🟡 需关注
- 50–69  = 🟠 有风险
- < 50   = 🔴 需立即处理

---

## Step 4：输出报告结构

### 1. 基本信息
- 采集时间范围
- 服务器配置摘要（来自 environment.json）
- 分析工具/数据来源

### 2. 健康评分卡

| 维度 | 均值 | P95 | 峰值 | 评分 | 状态 |
|------|------|-----|------|------|------|
| 内存 | ... | ... | ... | XX/100 | 🟢 |
| CPU | ... | ... | ... | XX/100 | 🟡 |
| ... | | | | | |
| **综合** | | | | **XX/100** | |

### 3. 问题清单（按严重程度排序）

每个问题格式：
```
[P1] 问题标题
- 现象：具体计数器数值（均值 X，P95 Y，峰值 Z）
- 根因：结合阈值和服务器配置的推断
- 影响：对 D365 业务操作的潜在影响
- 建议：具体可执行步骤（含 T-SQL 或配置命令）
```

### 4. 关键指标趋势说明
- 峰值时段识别
- 规律性高峰（夜间批处理等）
- 是否持续恶化

### 5. 优化建议（优先级排序）

| 优先级 | 建议 | 预期效果 | 实施难度 |
|--------|------|----------|----------|
| P1 - 立即 | ... | ... | 低/中/高 |
| P2 - 本周 | ... | ... | ... |
| P3 - 本月 | ... | ... | ... |

### 6. 需要补充的信息
列出无法判断的项目及原因。

---

## Step 5：★ 跨源关联反向解释（强制）

本步骤是 sql_perf_analyzer 的**核心差异化价值**：不只说「CPU 92%」，而是说「CPU 92% 是由 XX SQL + YY 阻塞链 导致的」。

### 5.1 对每个异常计数器必须输出

```
[异常项] CPU（% Processor Time）

📈 现象：2026-05-08 14:25~14:40 P95=92% 峰值95%（阈值85%）
⏰ 时间判定：命中 business_context.peak_hours（工作日 14:00-17:00） → 真业务高峰 → 真异常
🖥 物理基线：SQL01 16 核 / 128GB RAM → CPU Queue Length 基准 32

🔍 同期证据链（BucketStart=2026-05-08 14:25:00）：
  ┌─ 慢 SQL（slowsql_5min）：
  │   · Fingerprint: 0xABC... SELECT ... FROM POA WHERE ...
  │     执行 1240 次 / 总 CPU 820 秒 / 涉及表 PrincipalObjectAccess [BIG-TABLE]
  │   · Fingerprint: 0xDEF... UPDATE ContactBase ...
  │     执行 850 次 / 总 CPU 310 秒
  │
  ├─ 阻塞事件（blocking_1425）：
  │   · 3 条阻塞链，最长 18s，Head Blocker = SPID 85 运行上述 POA SQL
  │
  ├─ IIS 慢请求（iis_logs，若有）：
  │   · /api/data/v9.1/principalobjectaccesses 超过 SLA(2s) 的 48 次
  │
  └─ Plugin（若可关联）：
      · CRMAsyncService 账户 SPID 占总 CPU 42% → 异步作业风暴嫌疑

🎯 根因结论：PrincipalObjectAccess 缺失覆盖索引 + 业务高峰叠加异步作业 → CPU 与锁等待双瓶颈
🛠 联动建议：交付给 sql_index_optimizer 为 POA 建覆盖索引 + 交付给 sql_storage_analysis 评估是否归档
```

### 5.2 若找不到同期证据

必须诚实标注，不能编造：
```
⚠ 14:25 CPU 尖峰 92%，但同期 slowsql_5min 无超阈 SQL、blocking 无事件
   可能原因：
   - 慢 SQL 采集阈值偏高（当前 cpu_ms=500ms）漏掉短且密集 SQL
   - 非 SQL 进程占 CPU（需 Windows 侧 Top Process 佐证）
   - 计数器本身采样漂移
   建议：下次尖峰时开启 XEvent sql_batch_completed 抓取全量批量语句
```

### 5.3 服务器级联动

多服务器场景（SQL01 + SQL02 AlwaysOn），必须对 `ServerName` 分组独立评分，并在报告中标注：
- 主副本 vs 辅助副本负载分布是否失衡
- 哪台节点是持续问题服务器（命中 risk_profile.watch_list 自动升级）

---

## D365 特殊说明

1. D365 默认开启 RCSI，TempDB 版本存储开销远高于普通 SQL 应用
2. 若命名实例（如 MSSQL$CRM），CounterPath 前缀不是 SQLServer 而是 MSSQL$CRM
3. 采集间隔 > 30s 时，峰值可能被平滑，报告中需注明
4. 建议提供业务高峰期和非高峰期两段数据便于对比

## D365 常见性能问题速查

| 症状 | 首先检查 | 常见根因 |
|------|----------|----------|
| 用户系统卡顿 | CPU Queue + Lock Wait | 并发锁竞争或 CPU 不足 |
| 报表/视图超时 | Full Scans + Disk Latency | 缺失索引或磁盘 IO 瓶颈 |
| 夜间批处理慢 | Checkpoint + Log Flush | 日志 IO 或检查点风暴 |
| 内存持续增长 | PLE + Lazy Writes | Buffer Pool 压力 |
| 死锁频发 | Deadlocks/sec + Active Connections | 缺少行级索引或事务粒度过粗 |
| TempDB 爆满 | Free Space + Active Temp Tables | RCSI 版本存储 + 大排序溢出 |
