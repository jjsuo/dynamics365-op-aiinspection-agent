# SKILL: slow_sql_analyst

---

## 定位

SQL Server 2016+ + Dynamics 365 On-Premises 慢 SQL 性能诊断专家。

目标：不是列出所有慢 SQL，而是帮助快速找出**当前最应优先处理的 SQL 性能瓶颈**，并支持**跨源关联（CPU 尖峰那 5 分钟是谁在烧？）**。

本 Skill 消费的是服务器侧 `ServerCollectionScript/02_慢SQL采集` 产出的**时间桶聚合数据**（5 分钟 × SQL 指纹），而不是逐条 SQL 快照。

---

## 输入触发

标签：#SLOW_SQL
语义：分析慢SQL / 看看今天慢查询 / SQL性能报告 / 哪些SQL最慢 / 最近慢SQL集中在哪里 / 帮我出优化报告

---

## 数据来源

### 模式 A：用户上传文件
支持：slowsql_5min_*.csv / slowsql_daily_*.csv / slow_query_*.csv（UTF-8 with BOM）

### 模式 B：本地目录自动读取（默认）
路径：`<DATA_ROOT>/slow_sql/YYYY-MM-DD/*.csv`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

目录结构（新采集脚本产出）：
```
<DATA_ROOT>/slow_sql/YYYY-MM-DD/
├── slowsql_5min_YYYY-MM-DD.csv    ← ★ 主数据：5 分钟桶 × SQL 指纹聚合（用于跨源关联分析）
└── slowsql_daily_YYYY-MM-DD.csv   ← 当日汇总（用于长期趋势）
```

调用统一数据读取工具（只读取，不分析）：
```bash
python3 tools/data_reader.py --category slow_sql --today
python3 tools/data_reader.py --category slow_sql --yesterday
python3 tools/data_reader.py --category slow_sql --last-3
python3 tools/data_reader.py --category slow_sql --last-7
python3 tools/data_reader.py --category slow_sql --last-30
python3 tools/data_reader.py --category slow_sql --date 2026-04-28
python3 tools/data_reader.py --category slow_sql --start 2026-04-20 --end 2026-04-28
python3 tools/data_reader.py --category slow_sql --list-dates   # 查看可用日期
```

脚本返回结构（详见 TOOLS.md）：
- `status` / `data_root` / `category` / `date_range`
- `loaded`：成功读取的文件列表
- `missing`：缺失日期列表
- `files`：每个文件的 `{file, date, path, total_rows, columns, data}`；多日数据在 `files` 中按日期并列

本 Skill 按文件名识别：
- `slowsql_5min_*.csv`  → ★ 主分析数据（时间桶 × 指纹）
- `slowsql_daily_*.csv` → 日汇总（趋势）
- `slow_query_*.csv` / 其他  → 向后兼容的老格式（按单条 SQL 聚合）

若读取失败（`status=error`），提示用户检查：路径是否存在 / 日期是否有数据 / CSV 编码是否为 UTF-8

**数据读取后由本 Skill / AI 负责全部分析逻辑。**

---

## 时间理解规则

| 用户输入 | 调用参数 |
|----------|----------|
| 今天 | --today |
| 昨天 | --yesterday |
| 最近3天 | --last-3 |
| 最近7天 | --last-7 |
| 最近30天 | --last-30 |
| 2026-04-24 | --date 2026-04-24 |
| 4月20号到4月24号 | --start 2026-04-20 --end 2026-04-24 |

---

## CSV 字段约定

### slowsql_5min_*.csv（★ 主数据）

唯一键：`(BucketStart, SqlFingerprint)`，同一 5 分钟窗口内同一 SQL 指纹只会有一行。

| 字段 | 说明 |
|------|------|
| BucketStart         | 整 5 分钟时间戳（`2026-05-08 14:25:00`），★ 跨源 JOIN 键 |
| SqlFingerprint      | SHA2_256(SQL 前 500 字符) |
| SqlTextSample       | SQL 文本样本（截断 800 字符） |
| DatabaseName        | 数据库名 |
| ExecCount           | 该 5 分钟内执行次数 ★ |
| TotalCpuMs          | 该 5 分钟内总 CPU 消耗（ms）★ |
| TotalDurationMs     | 该 5 分钟内总耗时（ms）★ |
| TotalLogicalReads   | 该 5 分钟内总逻辑读 |
| AvgCpuMs            | 平均 CPU（`TotalCpuMs / ExecCount`） |
| AvgDurationMs       | 平均耗时 |
| MaxCpuMs / MaxDurationMs / MaxLogicalReads | 该桶内单次最高值 |
| DistinctHosts       | 涉及客户端主机数 |
| TopHostname         | 执行最频繁的客户端 |

### slowsql_daily_*.csv（日汇总）
类似结构但聚合粒度为整日，用于日间趋势对比。

### 老格式（slow_query_*.csv）向后兼容字段
```
sql_fingerprint / sql_text / execute_count /
total_logical_reads / avg_logical_reads /
total_cpu_ms / avg_cpu_ms /
total_elapsed_ms / avg_elapsed_ms /
execute_account / client_hostname / last_execution_time
```

字段缺失时：基于现有字段继续分析，输出中标注哪些维度因字段缺失无法评估。

---

## 分析流程

### Step 1：数据验证
- 确认 data 字段非空
- 识别实际可用字段（以 columns 为准）
- 判断数据类型（5min 桶 / daily / 老格式），优先用 5min 桶做分析
- 识别日期范围和数据天数
- 判断单天 / 多天模式

### Step 2：资源集中度分析（基于日汇总或 5min 桶回汇总）

**新格式（5min 桶）**：按 SqlFingerprint 在整个分析时段内聚合：
```
SUM(TotalCpuMs) AS CpuAll, SUM(TotalDurationMs) AS DurAll,
SUM(ExecCount) AS ExecAll, SUM(TotalLogicalReads) AS ReadsAll
GROUP BY SqlFingerprint
```

**老格式**：直接用 total_cpu_ms / total_logical_reads。

按 **TotalCpuMs（新格式）或 total_logical_reads（老格式）** 降序计算：

| 统计 | 输出 |
|------|------|
| TOP 3 占总 CPU 比 | 如：TOP3 占全部 CPU 的 71% |
| TOP 10 占比 | |
| TOP 20 占比 | |

集中度判断：
- **高度集中型**：TOP3 占比 > 60% → 少量 SQL 消耗大量资源，优先优化 TOP3
- **分散型**：TOP10 占比 < 40% → 大量 SQL 同时偏慢，需系统性优化（统计信息/索引策略）

SQL 行为模式分类：
- **高频低效型**：ExecCount 高 + AvgDurationMs 高 → 高优先级
- **低频超重型**：ExecCount 低 + MaxCpuMs 极高 → 定期报表/批处理类
- **尖峰聚集型**：某几个 BucketStart 桶 TotalCpuMs 异常突出 → 尖峰集中在特定时段

来源分析：
- TopHostname TOP3（哪个客户端发起最多慢 SQL）
- DistinctHosts 高的 SQL（跨多客户端广泛触发）

### Step 3：TOP 慢 SQL 清单（核心输出）

取 TotalCpuMs（或老格式 total_logical_reads）TOP 10（不足则全部输出）

每条输出：
1. 排名与危急等级（P1~P5）
2. SQL 类型识别
3. D365 关联判断（涉及哪个 D365 对象/功能）
4. **时间桶命中分布**：该指纹主要出现在哪几个 5 分钟桶（新格式独有）
5. 根因说明
6. 优化建议
7. 预估收益
8. 索引建议脚本

### Step 3.5：★ 时间窗口关联分析（新格式独有，跨源核心能力）

当用户问「14:25 那段时间谁在烧 CPU」时，直接按 `BucketStart` 过滤：

```
filter: BucketStart == '2026-05-08 14:25:00'
order by TotalCpuMs desc
top 10
```

输出表：

| 排名 | SQL 指纹 | 执行次数 | 总 CPU | 总耗时 | 平均耗时 | TopHostname |
|-----|---------|---------|-------|-------|---------|-------------|

若同时有 `server_per_sql` 数据，应提示 health_report 做跨源 JOIN：
> 在 `perfmon_5min_*.csv` 的同一 `BucketStart` 可以直接看到 CPU / PLE / Disk sec/Read 的数值。

### SQL 类型识别规则
- 全表扫描（无 WHERE 索引覆盖）
- 缺索引过滤（有 WHERE 但无对应索引）
- 大排序（ORDER BY 未走索引）
- Hash Join 过大（JOIN 两端数据量大）
- Key Lookup 高频（非覆盖索引导致回表）
- N+1 查询（循环中相同 SQL 多次执行）
- 参数嗅探（执行计划固定但参数变化大）
- 宽表扫描（SELECT * 或 SELECT 过多列）
- 无分页读取（RetrieveMultiple 未限制数量）

### D365 专项识别

重点表与常见问题：

| 表名 | 常见慢 SQL 原因 |
|------|----------------|
| ActivityPointerBase | 无分页查询活动记录 |
| AsyncOperationBase | 状态查询无覆盖索引 |
| PrincipalObjectAccess | 权限过滤全表扫描 |
| AuditBase | 无日期范围过滤的查询 |
| WorkflowLogBase | 关联查询无索引 |
| SystemUserBase | 登录/权限查询频繁 |
| ContactBase / AccountBase | FetchXML 翻译低效 |

D365 行为判断：
- 同步 Plugin 触发的高频 SQL（execute_account = CRM 服务账号）
- FetchXML 翻译低效（SQL 中包含大量 EXISTS 子查询）
- 批量 Upsert 接口压力（大量 MERGE 语句）
- 无分页 RetrieveMultiple（TOP 缺失）

### 危急等级标准

**新格式（5min 桶）阈值**（按整个分析时段聚合后判断）：

| 等级 | 条件（TotalCpuMs 为汇总值） |
|------|------|
| P1 🔴 | TotalCpuMs > 600,000 (10分钟CPU) 或 MaxDurationMs > 30,000 (单次>30s) |
| P2 🟠 | TotalCpuMs > 120,000 或 (ExecCount > 10,000 且 AvgDurationMs > 1,000) |
| P3 🟡 | TotalCpuMs > 30,000 或 AvgDurationMs > 500 |
| P4 🔵 | AvgDurationMs > 200 |
| P5 ⚪ | 其他 |

**老格式阈值**（兼容）：

| 等级 | 条件 |
|------|------|
| P1 🔴 | avg_logical_reads > 500,000 或 total_logical_reads > 50,000,000 |
| P2 🟠 | avg_logical_reads > 100,000 或 (execute_count > 1000 且 avg_logical_reads > 10,000) |
| P3 🟡 | avg_logical_reads > 20,000 或 total_cpu_ms > 300,000 |
| P4 🔵 | avg_logical_reads > 5,000 |
| P5 ⚪ | 其他 |

### Step 4：趋势分析（多天数据时）

- 最近 N 天 TOP SQL 变化（新增 / 消失 / 恶化 / 改善）
- 日均 CPU / 耗时总量趋势（上升 / 稳定 / 下降）
- **小时级高峰识别**（新格式独有）：按 `HOUR(BucketStart)` 统计 SUM(TotalCpuMs)，直接画出 24 小时 CPU 热力分布
- 是否有新出现的 P1 SQL（与前一天对比）
- 是否有已优化 SQL 出现反弹
- 高风险桶检测：连续 N 个 5 分钟桶 TotalCpuMs 持续偏高

### Step 5：索引建议汇总脚本

为每条 TOP SQL 生成索引建议：

标注类型：
- [NEW] 建议创建新索引
- [SKIP] 索引已存在（与 index_existing.csv 比对）
- [REUSE] 已有索引可复用
- [REBUILD] 建议重建现有索引（碎片高）

```sql
-- =============================================
-- 慢 SQL 优化索引脚本
-- 生成时间：YYYY-MM-DD
-- 环境：PROD - SQL01
-- 注意：建议在业务低峰期执行，ONLINE=ON 减少锁影响
-- =============================================

-- [P1] sql_fingerprint: abc123
-- 表：ContactBase，优化 ownerid + statecode 过滤
-- 预估收益：avg_logical_reads 82万 → 预计降至 500 以下
CREATE NONCLUSTERED INDEX [IX_ContactBase_OwnerId_StateCode_Incl]
ON [dbo].[ContactBase] ([OwnerId], [StateCode])
INCLUDE ([FullName], [EMailAddress1], [ModifiedOn])
WITH (ONLINE = ON, FILLFACTOR = 80, SORT_IN_TEMPDB = ON);
GO

-- [P2] sql_fingerprint: def456
-- 表：PrincipalObjectAccess
-- 预估收益：权限查询性能提升 60%
CREATE NONCLUSTERED INDEX [IX_POA_ObjectId_Principal]
ON [dbo].[PrincipalObjectAccess] ([ObjectId], [PrincipalId])
INCLUDE ([AccessRightsMask])
WITH (ONLINE = ON, FILLFACTOR = 80);
GO
```

### Step 6：优先级行动计划

```
## 行动计划

### 立即执行（今天）
- [ ] 执行 P1 索引创建脚本（ContactBase）
- [ ] 检查 PrincipalObjectAccess 权限查询是否可以缓存

### 本周执行
- [ ] 执行 P2 索引脚本
- [ ] 联系开发团队检查 Plugin 中的无分页查询
- [ ] 检查 FetchXML 是否有 N+1 问题

### 长期优化
- [ ] 评估 PrincipalObjectAccess 表归档方案
- [ ] 推动开发规范：RetrieveMultiple 必须设置 TopCount/PageInfo
```

---

## 输出结构

```
## 慢 SQL 分析报告

### 基本信息
分析日期范围：YYYY-MM-DD ~ YYYY-MM-DD
数据条数：X 条 SQL 指纹
服务器：SQL01（来自 environment.json）

### 资源集中度摘要
TOP 3 SQL 占总逻辑读：71%（高度集中型）
主要执行账号：CRM服务账号（Plugin触发）/ 报表账号
主要来源服务器：APP01（65%）

### TOP 10 慢 SQL 清单
[排名 + 等级 + 分析 + 建议]

### 趋势分析（多天时）
[趋势图表文本描述]

### 索引优化脚本
[完整可执行 T-SQL]

### 行动计划
[分优先级清单]
```
