# SKILL: sql_index_optimizer

---

## 定位

SQL Server + Dynamics 365 On-Premises 索引健康分析专家。

分析范围：索引使用率、碎片率、缺失索引、冗余索引、重复索引、表数据量，支持单日/范围/趋势模式。

本 Skill 不负责：慢 SQL 分析（由 slow_sql_analyst 负责）、执行计划分析、参数嗅探。

---

## 输入触发

标签：#INDEX / #INDEX_FRAGMENT
语义：分析索引 / 索引使用率 / 索引碎片 / 缺失索引 / 索引健康检查 / 表数据量分析 / 索引优化

---

## 数据来源

### 模式 A：用户上传文件
用户直接粘贴 CSV 或上传到工作区任意位置后指定路径。

### 模式 B：本地目录自动读取（默认）

路径：`<DATA_ROOT>/sql_index/`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

目录结构：
```
<DATA_ROOT>/sql_index/
├── index_existing.csv          ← 全量现有索引清单（静态，手动更新，作为 static_files 返回）
├── 2026-04-28/
│   ├── index_usage.csv         （索引使用率）
│   ├── fragmentation.csv       （索引碎片率）
│   ├── missing_index.csv       （缺失索引）
│   └── table_size.csv          （表数据量）
└── 2026-04-29/
    └── ...
```

调用统一数据读取工具（只读取，不分析）：
```bash
python3 tools/data_reader.py --category sql_index --today
python3 tools/data_reader.py --category sql_index --yesterday
python3 tools/data_reader.py --category sql_index --last-3
python3 tools/data_reader.py --category sql_index --last-7
python3 tools/data_reader.py --category sql_index --date 2026-04-28
python3 tools/data_reader.py --category sql_index --start 2026-04-20 --end 2026-04-28
python3 tools/data_reader.py --category sql_index --list-dates   # 查看可用日期
```

脚本返回结构（详见 TOOLS.md）：
- `files`：当日目录下的 CSV，本 Skill 按文件名识别：
  - `index_usage*.csv`    → 索引使用率（★ 采集脚本固定文件名）
  - `index_fragment*.csv` / `fragment*.csv` / `physical*.csv` → 碎片率（★ 新文件名 `index_fragment.csv`）
  - `missing_index*.csv` / `missing*.csv`        → 缺失索引
  - `table_size*.csv` / `rowcount*.csv` → 表数据量
- `static_files`：category 根目录下的静态文件，本 Skill 使用 `index_existing.csv` 做重叠检测

**数据读取后由本 Skill / AI 负责全部分析逻辑**，包括：碎片判断、缺失索引评分、重叠检测、D365 表专项检查。

多库（多个 `DatabaseName`）采集会合并到同一个 CSV，分析时需先按 `DatabaseName + SchemaName + TableName` 分组，避免跨库同名表混淆。

---

## ★ 跨源关联数据（强制加载）

索引优化不能只看 DMV。每一条 **CREATE / DROP / REBUILD** 建议都必须用以下三类上下文做反向验证。

### 1) 慢 SQL 反向验证（回答「这条索引建议是否真能解决问题」）

| 数据源 | 用途 |
|---|---|
| `slow_sql/<日>/slowsql_5min_*.csv` | 缺失索引之表 + WHERE 列，是否出现在 TOP SQL 的 executing_sql_text；命中 → 标 `[CONFIRMED-BY-SLOW-SQL]` 并提高优先级；未命中 → 标 `[LOW-USAGE-PROOF]` 降级。 |
| `memory/thresholds.json.slow_sql` | 阈值来源，判断慢 SQL 什么算 P1 |

使用规则：
- **缺失索引被慢 SQL 直接确认** → 优先级自动升 P0，在输出的 Missing Index 项头标 `[CONFIRMED-BY-SLOW-SQL]`，提供具体 SqlFingerprint 偏证。
- **高 ImpactScore 但 slowsql 中无匹配指纹** → 标 `[DMV-ONLY]`，提醒用户这可能是低频查询的 DMV 假阳性。
- **建议 DROP 的索引所在表** → 检查近 7 天 slowsql 是否有该表的索引名（hint 或 执行计划中），地横命中 → 强制降级为 `[DROP-CANDIDATE]`，禁止自动 DROP。

### 2) 阻塞反向验证（回答「索引缺失是否已经引发阻塞」）

| 数据源 | 用途 |
|---|---|
| `sql_blocking/<日>/blocking_*.csv` | 被阻塞的 SQL 和表是否在 missing_index 结果里；命中 → 标 `[CAUSING-BLOCK]` 并进一步升优先级 |
| `memory/thresholds.json.sql_blocking` | 阻塞阈值来源 |

### 3) 维护时窗与业务约束（回答「什么时候执行 / 能不能 DROP」）

| 数据源 | 用途 |
|---|---|
| `memory/business_context.json.maintenance_windows` | REBUILD / DROP 执行窗口建议，输出脚本头部必须标注 |
| `memory/business_context.json.peak_hours` | 索引维护脚本禁止落在峰值时段 |
| `memory/sql_config.json.edition` | 非 Enterprise 时禁止输出 `ONLINE=ON`，改为离线执行告警 |
| `memory/sql_config.json.databases[].recovery_model` | FULL 模式下 REBUILD 产生大量日志，建议分批或切换 BULK_LOGGED |
| `memory/d365_custom.json.indexes.never_drop_patterns` | 命中的索引绝对禁 DROP |
| `memory/d365_custom.json.indexes.d365_system_index_prefixes` | `PK_ / ndx_ / fndx_ / AK_ / UQ_ / cndx_` 等前缀统一以此为源，不再硬编码 |
| `memory/thresholds.json.index` | `fragmentation_pct_reorganize/rebuild` 统一从此读取 |

### 4) 业务 SLA 与大表背景（回答「这条建议影响面多大」）

| 数据源 | 用途 |
|---|---|
| `memory/business_context.json.sla.iis_response_p95_ms` | 评估索引优化对 P95 的预期收益 |
| `memory/business_context.json.batch_job_fingerprints` | 若目标表被 batch job 高频写入 → CREATE INDEX 的 FILLFACTOR 建议下调到 70，Online 时间延长 |
| `memory/risk_profile.json.watch_list` | 运行期容易膨胀的大表（AuditBase / AsyncOperationBase / POA）；启用分区/归档建议时需交叉确认 |

---

## 日期识别规则

| 用户输入 | 调用参数 |
|----------|----------|
| 分析今天索引 | --today |
| 分析昨天索引 | --yesterday |
| 分析最近7天索引 | --last-7 |
| 未指定日期 | 默认取最新可用日期 |

---

## CSV 字段映射（与采集脚本 04 严格对齐）

### index_usage.csv
`DatabaseName` / `SchemaName` / `TableName` / `IndexName` / `IndexType` / `IsUnique` / `IsPrimaryKey` /
`UserSeeks` / `UserScans` / `UserLookups` / `UserUpdates` /
`LastUserSeek` / `LastUserScan` / `LastUserUpdate` /
`RowCount_` / `TotalSizeKB` / `UsedSizeKB` / `CollectDate`

说明：
- `IsPrimaryKey=1` 且命名以 `PK_` 开头 → 平台索引，禁止 DROP
- `LastUserSeek` 为 NULL 或非常旧（采集脚本里是 SQL Server 重启以来的累计） → 配合 UserSeeks/Scans 判断是否真无用
- `RowCount_` 字段名带下划线（规避 T-SQL 关键字），是单分区近似行数

### index_fragment.csv（新文件名，代替老的 fragmentation.csv）
`DatabaseName` / `SchemaName` / `TableName` / `IndexName` / `IndexType` /
`PartitionNumber` / `index_type_desc` /
`AvgFragmentPct` / `PageCount` / `RecordCount` / `CollectDate`

说明：
- 关键碎片字段是 `AvgFragmentPct`（不是旧的 FragmentationPct）
- 采集脚本已过滤 `page_count > 500`，小表不会出现在此文件
- 同一索引可能出现多行（分区表按 `PartitionNumber` 分别采集）

### missing_index.csv
`DatabaseName` / `SchemaName` / `TableName` /
`EqualityColumns` / `InequalityColumns` / `IncludedColumns` /
`UserSeeks` / `UserScans` / `AvgTotalUserCost` / `AvgUserImpactPct` / `ImpactScore` /
`LastUserSeek` / `CollectDate`

说明：
- `ImpactScore = AvgTotalUserCost × AvgUserImpactPct × (UserSeeks + UserScans)` 由服务器端直接算好
- 本 Skill 直接在 `ImpactScore` 上排序 + 分档，不要再自己乘一遍
- 建议新索引键列组合 = `EqualityColumns` + `InequalityColumns`，`INCLUDE` 列 = `IncludedColumns`

### table_size.csv
`DatabaseName` / `SchemaName` / `TableName` / `RowCount_` /
`ReservedKB` / `UsedKB` / `UsedMB` / `ReservedGB` /
`CreateDate` / `ModifyDate` / `CollectDate`

说明：
- `UsedMB` ≈ 用户数据 + 索引已使用空间；`ReservedGB` 包含未使用页
- 采集脚本未直接拆 Data vs Index，如需区分请另外用 `index_usage.UsedSizeKB` 聚合
- 老文档里的 `TotalSizeMB / DataSizeMB / IndexSizeMB` 字段**不存在**，请从 `UsedMB / ReservedGB` 推导

### index_existing.csv（重叠检测用，静态，category 根目录）
`DatabaseName` / `SchemaName` / `TableName` / `IndexName` / `IndexType` /
`is_unique` / `is_primary_key` / `is_disabled` /
`KeyColumns`（用 `, ` 拼接，含 ` DESC` 后缀）/ `IncludedColumns`（用 `, ` 拼接）

说明：
- 字段名是小写下划线（沿用 `sys.indexes` 原名）
- `IsCrmManaged` 字段**已移除**，平台索引判定改走命名前缀（`PK_ / ndx_ / fndx_ / AK_ / UQ_ / cndx_`）

---

## 现有索引重叠检测（所有 CREATE 建议输出前必须执行）

### 完全重复索引
建议键列与已有完全相同 → 输出 [SKIP] 索引已存在

### 前缀覆盖
建议 (ownerid)，已有 (ownerid, statecode) → 输出 [REUSE] 已有索引前缀可复用

### INCLUDE 覆盖
建议 (ownerid) INCLUDE(createdon)，已有 (ownerid) INCLUDE(createdon, modifiedon) → 输出 [REUSE]

### 同字段索引过多
同表相关索引 >= 3 个 → 输出 [WARN] 同类索引过多，建议治理

---

## Dynamics CRM 索引分类

### 平台管理索引（禁止 DROP）
命名前缀：PK_ / ndx_ / fndx_ / AK_ / UQ_ / cndx_
操作限制：只允许 REBUILD / REORGANIZE，禁止 DROP 或修改键列

### 用户管理索引（可优化）
非上述前缀的索引，允许 CREATE / DROP / REBUILD / REORGANIZE

---

## 分析规则

> **阈值来源统一：** 以下所有数值阈值优先从 `memory/thresholds.json` 读取（`index` / `table_size` 节点）；若缺失才回退到下面的硬编码默认值。

### 1. 无用索引（建议 DROP）

条件：
- UserSeeks = 0 且 UserScans = 0 且 UserLookups = 0
- UserUpdates > 0（有写入开销）
- PageCount > 100

**反向验证必须走：**
1. 命中 `d365_custom.json.indexes.never_drop_patterns` / `d365_system_index_prefixes` → 绝对禁 DROP，改输出 `[PROTECTED]`
2. 该表近 7 天 slowsql 中出现过该索引名 → 降级为 `[DROP-CANDIDATE-REVIEW]`，输出 DROP 语句同时附 `-- TODO: 人工确认`
3. 仅在第 1 、2 项都未命中时才输出正常 `DROP INDEX`。

仅对 User-Managed 索引输出 DROP 建议。

### 2. 高碎片索引

基于 `index_fragment.csv` 的 `AvgFragmentPct` + `PageCount`：

| AvgFragmentPct | PageCount | 建议操作 |
|----------------|-----------|----------|
| `>= thresholds.index.fragmentation_pct_rebuild`（默认 30） | >= 1000 | REBUILD |
| `[fragmentation_pct_reorganize, fragmentation_pct_rebuild)`（默认 10-30） | 任意 | REORGANIZE |
| > 80% | < 1000（小表） | 忽略 |

执行时间约束：
- 输出的 ALTER 脚本必须标注建议执行窗口（从 `business_context.json.maintenance_windows` 取最合适窗口）
- 若 `sql_config.json.edition` 非 Enterprise → 去掉 `ONLINE=ON`，添加 `-- WARNING: 非 Enterprise 需离线执行`

### 3. 缺失索引

条件：`AvgUserImpactPct > 50` 且 `(UserSeeks + UserScans) > 50`

优先级（基于服务器端已算好的 `ImpactScore`）：
- ImpactScore > `thresholds.index.missing_index_impact_score_warning`×10（默认 10000000，或 slow_sql 反向验证命中） → P0（立即创建）
- `missing_index_impact_score_warning` ×1～10 之间 → P1
- < `missing_index_impact_score_warning` → P2

**每条缺失索引输出必须携带跨源标签**（至少一个）：
- `[CONFIRMED-BY-SLOW-SQL]`：同日 slowsql_5min 中命中表+列组合，附具体 SqlFingerprint
- `[CAUSING-BLOCK]`：同日 blocking 中受缺索引影响表出现阻塞
- `[DMV-ONLY]`：DMV 信号存在但未在慢 SQL / 阻塞中得到印证

输出索引标签：[NEW] / [SKIP] / [REUSE] / [WARN]，与上面跨源标签可并列。

### 4. 冗余索引

同表中 A 索引键列是 B 索引键列的前缀，且 A 使用率低于 B → A 为冗余索引，建议删除 A。
同样反向验证：若 A 在 slowsql 中被引用（hint或执行计划名称命中）→ 降级为 `[DROP-CANDIDATE-REVIEW]`。

### 5. 索引体积异常

以 `index_usage.csv` 为数据源：
- 按 `DatabaseName + TableName` 聚合 `UsedSizeKB`：
  - `IndexSpaceKB = SUM(UsedSizeKB WHERE IndexType != 'CLUSTERED' AND IsPrimaryKey = 0)`
  - `DataSpaceKB  = SUM(UsedSizeKB WHERE IndexType  = 'CLUSTERED' OR IsPrimaryKey = 1)`
- `IndexSpaceKB > DataSpaceKB × 2` → 索引过多或存在大量冗余

### 6. 系统重复索引分析

从 index_existing.csv 识别：
- 同表 KeyColumns 完全相同的重复索引 → 保留使用率高的，删除低的
- 前缀冗余：IX_A(ownerid) 和 IX_B(ownerid,statecode)，若 IX_A 使用率低 → 删除 IX_A
- INCLUDE 冗余：IX_A 的 INCLUDE 是 IX_B INCLUDE 的子集 → 考虑删除 IX_A

任何 DROP 建议都必须先过 `d365_custom.json.indexes.never_drop_patterns` / `d365_system_index_prefixes` 过滤。

---

## CRM 高优先级表专项检查

以下表必须重点分析：

| 表名 | 行数预警 | 重点检查项 |
|------|----------|-----------|
| ContactBase | > 500万 | 缺失索引、碎片、OwnerId过滤 |
| AccountBase | > 500万 | 缺失索引、碎片 |
| ActivityPointerBase | > 800万 | 分页索引、状态过滤索引 |
| AsyncOperationBase | > 500万 | StatusCode 覆盖索引 |
| PrincipalObjectAccess | > 2000万 | ObjectId+PrincipalId 联合索引 |
| AuditBase | > 1000万 | CreatedOn 范围查询索引 |
| EmailBase | > 150万 | 缺失索引 |

---

## 趋势分析（多天数据）

若读取最近 N 天目录，输出：
- 最近 N 天碎片率趋势（逐渐升高 / 稳定 / 降低）
- 缺失索引变化（新增 / 消失）
- 增长最快的前 5 张表
- 索引膨胀趋势（IndexSizeMB / DataSizeMB 比值变化）
- 重复索引是否增加

---

## 输出结构

```
## 索引健康分析报告

### 分析概况
数据来源：本地目录 / 上传文件
分析日期：YYYY-MM-DD
跨源数据加载状态：
  - slowsql_5min_*.csv  ✓/✗  用于缺失索引反向验证
  - blocking_*.csv      ✓/✗  用于阻塞反向验证
  - business_context    ✓/✗  用于推荐维护窗口
  - sql_config          ✓/✗  用于 Edition / RCSI / recovery_model 判定
  - d365_custom         ✓/✗  用于 never_drop 保护

### 健康评分卡
| 维度 | 得分 |
|------|------|
| 缺失索引覆盖度 | XX/20 |
| 碎片控制 | XX/20 |
| 冗余索引治理 | XX/20 |
| 重复索引治理 | XX/20 |
| CRM 大表健康度 | XX/20 |
| 综合得分 | XX/100 |

🟢 >=80 健康 / 🟡 60-79 需关注 / 🔴 <60 需立即处理

### 执行摘要（TOP5 问题）
每条必带跨源标签（CONFIRMED-BY-SLOW-SQL / CAUSING-BLOCK / DMV-ONLY 至少一个）。

### 详细分析
#### 3.1 缺失索引（每条附跨源标签与 SqlFingerprint/阻塞偏证）
#### 3.2 高碎片索引（附建议执行窗口）
#### 3.3 无用索引（附 PROTECTED / DROP-CANDIDATE-REVIEW 状态）
#### 3.4 重复索引治理
#### 3.5 冗余索引治理
#### 3.6 大表分析

### T-SQL 优化脚本（脚本头部必须标注维护窗口与 Edition）

### 数据补充提示（若有缺失）
- 缺少 slowsql_5min → 所有 Missing Index 回退到 [DMV-ONLY]
- 缺少 blocking  → 无法标注 [CAUSING-BLOCK]
- 缺少 business_context → 执行窗口回退到「建议业务低峰期」泛称
- 缺少 sql_config → 不确定 Edition，ONLINE=ON 默认加上并在头部注明「需核实 Edition」
```

---

## SQL 脚本执行顺序

1. REBUILD / REORGANIZE 碎片索引
2. CREATE 缺失索引（[NEW] 标注）
3. DROP 重复索引（使用率低者）
4. DROP 冗余索引

```sql
-- =============================================
-- 索引优化脚本
-- 环境：PROD - SQL01  Edition：Enterprise (来自 sql_config.json)
-- 建议执行窗口：每周日 02:00-04:00（来自 business_context.maintenance_windows）
-- 校验来源：index_existing.csv + index_usage.csv + slowsql_5min + blocking + d365_custom
-- =============================================

-- Step 1: REBUILD 高碎片索引 (AvgFragmentPct=42% ≥ thresholds.rebuild 30)
ALTER INDEX [ndx_contact_ownerid]
ON [dbo].[ContactBase]
REBUILD WITH (
    ONLINE = ON,
    FILLFACTOR = 80,
    SORT_IN_TEMPDB = ON
);
GO

-- Step 2: CREATE 缺失索引（已确认无重叠）
-- [NEW][CONFIRMED-BY-SLOW-SQL] P0 - ContactBase OwnerId 过滤
-- slowsql 偏证：SqlFingerprint=abc123  TotalCpuMs=820000
CREATE NONCLUSTERED INDEX [IX_ContactBase_OwnerId_State_Incl]
ON [dbo].[ContactBase] ([OwnerId], [StateCode])
INCLUDE ([FullName], [EMailAddress1])
WITH (ONLINE = ON, FILLFACTOR = 80, SORT_IN_TEMPDB = ON);
GO

-- Step 3: DROP 重复索引（低使用率）
-- [DROP-CANDIDATE-REVIEW] 已确认 IX_Contact_Owner_Old 与上述新索引重复
-- ❗ slowsql 中近 7 天无此索引引用，但建议先禁用观察 7 天再 DROP
-- ALTER INDEX [IX_Contact_Owner_Old] ON [dbo].[ContactBase] DISABLE;
-- GO
-- DROP INDEX [IX_Contact_Owner_Old] ON [dbo].[ContactBase];
-- GO
```

---

## 固定风险提示

1. DMV 数据 SQL Server 重启后清零，短期数据仅供参考
2. DROP 索引前务必确认业务影响，建议先禁用观察 7 天
3. CRM 平台索引（ndx_ / fndx_ 前缀）禁止删除
4. CREATE INDEX 建议先在测试环境验证
5. 大表（> 1GB）的 REBUILD 操作建议在维护窗口执行
