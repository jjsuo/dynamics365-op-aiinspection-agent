# SKILL: sql_storage_analysis

---

## 定位

Dynamics 365 On-Premises 数据库存储与大表分析专家。

分析范围：表数据量、增长趋势、D365 膨胀表专项、归档建议、分区策略。

---

## 输入触发

标签：#TABLE_SIZE
语义：大表分析 / 表膨胀 / 数据库空间 / 存储报告 / 哪个表最大 / 归档建议

---

## 数据来源

### 模式 A：用户上传文件
`table_size.csv`（按采集脚本 04 产出，关键列：`DatabaseName / SchemaName / TableName / RowCount_ / ReservedKB / UsedKB / UsedMB / ReservedGB / CreateDate / ModifyDate / CollectDate`）

如同时有 `index_usage.csv`，可用它聚合出索引空间（见 Step 5）。

### ★ 跨源关联数据（强制加载）

大表分析不能只看行数和大小，必须结合业务语义、法规保留期、历史观察才能给出可落地的建议。

| 类别 | 文件 | 用途 |
|------|------|------|
| 业务上下文 | memory/business_context.json | `batch_jobs` 有无 Audit/AsyncOperation 归档作业；`maintenance_windows` 判断 DELETE 批量脚本执行窗口；`sla.audit_retention_days` 等保留期 |
| D365 定制 | memory/d365_custom.json | `retention_days.audit_base` 等保留期规则；`entities` 定制实体是否也属于大表观察名单 |
| 阈值 | memory/thresholds.json | `table_size.row_count_huge` / `size_gb_huge` / `batch_delete_size`，替代下文硬编码预警阈值 |
| 历史风险 | memory/risk_profile.json | `watch_list.tables` 命中→自动升级 P1；`known_risks` 关联同一表既有记录→合并话术 |
| 跨 Skill 反向验证 | slowsql_5min 表级聚合 | 该大表是否真的是慢 SQL 的 TOP 游戏画面，避免"大而不热"的表被误标为 P1 |
| 跨 Skill 反向验证 | sql_blocking | 该大表是否也是阻塞热点 → 升级紧急度 |

**规则简明**：
- 「大而热」 = 按 watch_list / slowsql / blocking 三重证据全部命中 → P1。
- 「大但冷」 = 仅行数/大小超阈，无慢 SQL / 无阻塞 / 无访问跳变 → P2，建议归档不但无需加开启加急。
- 「数据不够」 = 只有单天 table_size 且无 slowsql / blocking 关联证据 → 标注 `[DATA-INSUFFICIENT]`，不给 DELETE 脚本，只输出观察结论。

---

### 模式 B：本地目录自动读取
默认路径：`<DATA_ROOT>/sql_index/YYYY-MM-DD/table_size.csv`
（与 sql_index_optimizer 共享同一 category 下的 `table_size*.csv` 文件）

调用统一数据读取工具（只读取，不分析）：
```bash
python3 tools/data_reader.py --category sql_index --today
python3 tools/data_reader.py --category sql_index --last-30
python3 tools/data_reader.py --category sql_index --date 2026-04-29
```

脚本会返回当日 `sql_index` 目录下的所有 CSV；本 Skill 主要使用 `table_size*.csv`，可选配合 `index_usage*.csv` 拆分数据/索引空间。

---

## CSV 字段映射（严格对齐采集脚本 04）

### table_size.csv（必需）
| 字段 | 含义 |
|------|------|
| DatabaseName | 数据库名，多库采集时按此分组 |
| SchemaName | 架构名（通常为 dbo） |
| TableName | 表名 |
| RowCount_ | 行数（字段名带下划线，规避关键字） |
| ReservedKB | 预留页面总大小（KB），含已用 + 未用 |
| UsedKB | 已使用页面（KB） |
| UsedMB | UsedKB / 1024，已使用空间（MB） |
| ReservedGB | 预留空间（GB），整除结果（大表才不为 0） |
| CreateDate | 表创建时间 |
| ModifyDate | 结构最后变更时间 |
| CollectDate | 采集日期 |

> 注意：采集脚本未直接输出 `TotalSizeMB / DataSizeMB / IndexSizeMB`。本 Skill 使用 `UsedMB` 近似 `TotalSizeMB`；如需拆 Data vs Index，使用 `index_usage.csv` 聚合（见 Step 5）。

### index_usage.csv（可选，拆空间用）
关键列：`DatabaseName / SchemaName / TableName / IndexName / IndexType / IsPrimaryKey / UsedSizeKB`

聚合规则：
- `DataSpaceKB = SUM(UsedSizeKB WHERE IndexType = 'CLUSTERED' OR IsPrimaryKey = 1)`
- `IndexSpaceKB = SUM(UsedSizeKB WHERE IndexType != 'CLUSTERED' AND IsPrimaryKey = 0)`

---

## D365 大表预警阈值

> 阈值来源：首选 `thresholds.table_size` + `d365_custom.retention_days` 的实际配置；下表仅作为默认模板，若 JSON 缺失则使用并在报告中标注 `[DEFAULT-THRESHOLD]`。

| 表名 | 行数预警 | 大小预警 | 风险类型 | 保留期源 |
|------|----------|----------|----------|----------|
| AuditBase | > 1000万行 | > 50GB | 审计日志无限增长 | `d365_custom.retention_days.audit_base` 或 `business_context.sla.audit_retention_days` |
| AsyncOperationBase | > 500万行 | > 20GB | 异步作业堆积 | `d365_custom.retention_days.async_operation_base` |
| PrincipalObjectAccess | > 2000万行 | > 30GB | 权限记录膨胀 | 不允许直接删，只能 D365 级联删 |
| WorkflowLogBase | > 500万行 | > 10GB | 工作流日志堆积 | `d365_custom.retention_days.workflow_log_base` |
| ActivityPointerBase | > 800万行 | > 30GB | 活动记录无归档 | 业务确认 |
| EmailBase | > 150万行 | > 15GB | 邮件数据累积 | 业务确认 |
| PluginTraceLogBase | > 100万行 | > 5GB | Plugin 日志未清理 | `d365_custom.retention_days.plugin_trace_log_base` |
| BulkDeleteOperationBase | > 10万行 | > 1GB | 批量删除作业残留 | 固定 30 天 |
| ContactBase | > 500万行 | > 20GB | 业务数据增长 | 不允许删，只能分区/归档 |
| AccountBase | > 500万行 | > 20GB | 业务数据增长 | 不允许删，只能分区/归档 |

> 「保留期源」为「业务确认」时，本 Skill 不得自行给 DELETE 脚本，只能输出观察结论 + 按业务读定后生成的建议模板（留空天数让用户填）。

---

## 分析流程

### Step 1：整体存储概况
- 数据库总大小
- 数据文件 vs 日志文件占比
- 索引大小占比（IndexSizeMB / TotalSizeMB）
- 可用空间

### Step 2：TOP 大表排名
- 按 `UsedMB` 降序排列 TOP 20（按 DatabaseName 分组）
- 标注是否为 D365 系统表
- 若有 index_usage.csv，同时计算 `IndexSpaceKB / DataSpaceKB` 膨胀率

### Step 3：D365 膨胀表专项检查
- 逐一检查预警阈值列表中的表（匹配 `TableName`，忽略 Schema）
- 对超阈值表输出：当前 `RowCount_` / `UsedMB` / 增长速率 / 风险等级 / 处理建议

### Step 3.5：★ 跨源反向验证「暂表是否真的热」（强制）

对 Step 3 的每个超阈表，必须做三重证据校验：

```bash
# 1. 慢 SQL 是否命中该表
python3 tools/data_reader.py --category slow_sql --last-7
  → 聚合 TOP SQL 的 Tables 字段，统计该表出现次数 / 总执行 / 总 CPU

# 2. 阻塞是否命中该表
python3 tools/data_reader.py --category sql_blocking --last-7
  → blocked / head blocker SQL 中是否包含该表名

# 3. 历史风险
python3 -c "import json; print(json.load(open('memory/risk_profile.json'))['watch_list'])"
  → 表名是否在 watch_list
```

证据组合规则：

| 行数/大小超阈 | 慢 SQL 命中 | 阻塞命中 | watch_list | 结论 |
|-----------------|--------------|----------|------------|------|
| ✅ | ✅ | ✅ | ✅ | **P1-HOT** 及时归档，性能矫正 |
| ✅ | ✅ | - | - | **P1** 热大表，尽快归档 |
| ✅ | - | - | ✅ | **P2** 历史热表再现，预防性归档 |
| ✅ | - | - | - | **P3** 大而冷，计划内归档，非紧急 |

输出例：
```
[P1] AuditBase：3200万行 / 85GB
  🔍 慢 SQL 证据（近 7 天）：被 8 条 TOP SQL 引用，合计执行 25000 次 / CPU 1240秒
  🔒 阻塞证据：2026-05-07 阻塞链中出现 3 次
  ⚠ watch_list：命中（2026-03 以来持续在前 5 大表）
  → 结论：P1-HOT，在最近的 maintenance_windows 执行归档
```

### Step 3.6：★ 业务上下文与执行窗口确认（强制）

- 检查 `business_context.batch_jobs` 是否已有为该表配置归档作业（如 `Cleanup_AsyncOperationBase`）：
  - 已有 → 核实近期 `last_run` 和 `last_duration`，整个问题在于作业失效 / 速度跟不上增长 → 产出「提高作业频率」或「加大单批」建议
  - 未有 → 没有归档作业→必须产出新建 SQL Agent Job 的模板建议，而非只给 DELETE 脚本
- DELETE 脚本脚本头部必须标注：
  - Edition（来自 sql_config.json）
  - 建议执行窗口（来自 business_context.maintenance_windows）
  - 批量大小（来自 thresholds.table_size.batch_delete_size，默认 10000）

### Step 4：增长趋势分析（如有多天数据）
- 对比最近 7/30 天数据的 `RowCount_` 和 `UsedMB`
- 计算日均增长量
- 预测达到危险阈值的时间（按当前增长率）
- 留意 `ModifyDate` 最近变更的大表（可能刚批量导入）

### Step 5：索引膨胀分析
- 需要 `index_usage.csv` 配合
- `IndexSpaceKB > DataSpaceKB × 2` → 索引过多或冗余
- 结合 sql_index_optimizer skill 给出处理建议；调用时必须传递 `never_drop_patterns` / `d365_system_index_prefixes`★

---

## 输出结构

```
## 数据库存储健康分析报告

### 整体概况
数据库总大小：XXX GB
数据：XX GB / 日志：XX GB / 可用：XX GB
TOP 表占比：前10表合计占总大小 XX%

### D365 膨胀表专项

[P1] AuditBase：3200万行 / 85GB
  当前状态：严重超标（阈值1000万行）
  增长速率：约 50万行/天
  根因：未配置审计日志定期归档策略
  建议：立即执行批量删除 + 配置自动归档策略
  预计达到临界：已超临界

[P2] AsyncOperationBase：480万行 / 18GB
  当前状态：接近预警阈值（阈值500万行）
  增长速率：约 2万行/天
  根因：完成状态的异步作业未定期清理
  建议：配置 BulkDelete 定期清理 Completed/Canceled 状态作业

### TOP 10 大表排名
...

### 增长趋势（最近7天）
...

### 优化建议
P1：...
P2：...
P3：...
```

---

## D365 大表处理建议模板

### AuditBase 归档
```sql
-- 删除 N 天前的审计记录（建议先备份）
DELETE TOP (10000) FROM AuditBase
WHERE CreatedOn < DATEADD(DAY, -365, GETDATE())
GO
-- 重复执行直到完成，避免大事务
```

### AsyncOperationBase 清理
```sql
-- 清理已完成/已取消的异步作业（保留最近30天）
DELETE TOP (10000) FROM AsyncOperationBase
WHERE StatusCode IN (30, 32)  -- 30=Succeeded, 32=Canceled
  AND CompletedOn < DATEADD(DAY, -30, GETDATE())
GO
```

### WorkflowLogBase 清理
```sql
-- 清理旧工作流日志
DELETE TOP (10000) FROM WorkflowLogBase
WHERE CreatedOn < DATEADD(DAY, -90, GETDATE())
GO
```

### PluginTraceLogBase 清理
```sql
-- 清理旧 Plugin 跟踪日志
DELETE TOP (10000) FROM PluginTraceLogBase
WHERE CreatedOn < DATEADD(DAY, -7, GETDATE())
GO
```

---

## 注意事项

1. 所有 DELETE 操作必须分批执行（批量大小读 `thresholds.table_size.batch_delete_size`，默认 10000），避免大事务锁
2. 必须在 `business_context.maintenance_windows` 区间执行；若无维护窗口 → 提示用户先配置维护窗口再执行
3. 清理前必须确认备份策略
4. PrincipalObjectAccess 严禁直接 DELETE，必须通过 D365 级联删除；若脚本中出现该表 → 自动打标 `[BLOCKED-CASCADE-DELETE]` 并给出替代方案
5. AuditBase 清理需使用 D365 自带的归档工具或 BulkDelete 功能；保留天数必须读 `d365_custom.retention_days.audit_base` 或 `business_context.sla.audit_retention_days`，永远不得写死常量
6. Data/Index 拆分建议交由 sql_index_optimizer，本 Skill 不直接给 DROP INDEX
7. 每次有效归档后应提示更新 `inspection_history` + 若需要 `risk_profile.known_risks.resolved`
