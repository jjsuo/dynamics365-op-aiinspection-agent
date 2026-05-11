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

### 1. 无用索引（建议 DROP）

条件：
- UserSeeks = 0 且 UserScans = 0 且 UserLookups = 0
- UserUpdates > 0（有写入开销）
- PageCount > 100

仅对 User-Managed 索引输出 DROP 建议。

### 2. 高碎片索引

基于 `index_fragment.csv` 的 `AvgFragmentPct` + `PageCount`：

| AvgFragmentPct | PageCount | 建议操作 |
|----------------|-----------|----------|
| > 30% | >= 1000 | REBUILD |
| 10%–30% | 任意 | REORGANIZE |
| > 80% | < 1000（小表） | 忽略 |

### 3. 缺失索引

条件：`AvgUserImpactPct > 50` 且 `(UserSeeks + UserScans) > 50`

优先级（基于服务器端已算好的 `ImpactScore`）：
- ImpactScore > 500,000 → P0（立即创建）
- 100,000–500,000 → P1
- < 100,000 → P2

输出标签：[NEW] / [SKIP] / [REUSE] / [WARN]

### 4. 冗余索引

同表中 A 索引键列是 B 索引键列的前缀，且 A 使用率低于 B → A 为冗余索引，建议删除 A。

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
...

### 详细分析
#### 3.1 缺失索引
#### 3.2 高碎片索引
#### 3.3 无用索引
#### 3.4 重复索引治理
#### 3.5 冗余索引治理
#### 3.6 大表分析

### T-SQL 优化脚本
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
-- 环境：PROD - SQL01
-- 执行时间建议：业务低峰期（22:00 后）
-- =============================================

-- Step 1: REBUILD 高碎片索引
ALTER INDEX [ndx_contact_ownerid]
ON [dbo].[ContactBase]
REBUILD WITH (
    ONLINE = ON,
    FILLFACTOR = 80,
    SORT_IN_TEMPDB = ON
);
GO

-- Step 2: CREATE 缺失索引（已确认无重叠）
-- [NEW] P0 - ContactBase OwnerId 过滤
CREATE NONCLUSTERED INDEX [IX_ContactBase_OwnerId_State_Incl]
ON [dbo].[ContactBase] ([OwnerId], [StateCode])
INCLUDE ([FullName], [EMailAddress1])
WITH (ONLINE = ON, FILLFACTOR = 80, SORT_IN_TEMPDB = ON);
GO

-- Step 3: DROP 重复索引（低使用率）
-- [WARN] 已确认 IX_Contact_Owner_Old 与上述新索引重复
-- 执行前请确认业务影响
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
