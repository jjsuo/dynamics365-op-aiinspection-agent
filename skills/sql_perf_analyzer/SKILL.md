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
- PerfMon CSV / BLG 文件
- server_per_sql_stats.csv（等待统计）
- server_per_sql_baseline.json（性能基线摘要）

### 模式 B：本地目录自动读取（默认）

路径：`<DATA_ROOT>/server_per_sql/YYYY-MM-DD/`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

```
├── perf_counter_*.csv           （PerfMon 计数器数据）
├── server_per_sql_stats.csv     （SQL 等待统计）
├── serverper.csv                （PerfMon 汇总 / 等价别名）
└── server_per_sql_baseline.json （性能基线摘要，可选）
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
  - `perf*.csv` / `perf_counter_*.csv` / `serverper*.csv` → PerfMon 计数器原始数据
  - `*stats*.csv` / `*wait*.csv` → SQL 等待统计
  - `*baseline*.json` → 性能基线摘要
- 所有 PerfMon 聚合（按 CounterPath 求 Avg/Max/Min/P95）、阈值判断、评分，均由本 Skill 在分析阶段完成。读取脚本不做任何聚合。

**数据读取后由本 Skill / AI 负责全部分析逻辑**，包括：计数器维度分类、聚合、阈值判断、评分、瓶颈定位。

---

## Step 1：数据获取与服务器配置加载

读取 memory/environment.json 中的服务器配置（SQL01）：

| 字段 | 用途 |
|------|------|
| cpu_cores | 评估 CPU 压力阈值（Queue Length 基准） |
| memory_gb | 评估 SQL Buffer Pool 配置是否合理 |
| disk（类型） | 评估磁盘延迟是否正常（HDD vs SSD） |
| sql_version | 判断已知 bug / 特性支持 |
| max_server_memory_gb | 与 Target Server Memory 对比 |

若配置缺失，在报告末尾列出"需补充的配置信息"。

---

## Step 2：指标解析与阈值判断

对每个计数器计算：**均值 / P95 / 峰值 / 超阈值占比(%)**

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
