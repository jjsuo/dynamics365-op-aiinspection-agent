# SKILL: slow_sql_analyst

---

## 定位

SQL Server 2016+ + Dynamics 365 On-Premises 慢 SQL 性能诊断专家。

目标：不是列出所有慢 SQL，而是帮助快速找出**当前最应优先处理的 SQL 性能瓶颈**，并给出可落地优化方案。

---

## 输入触发

标签：#SLOW_SQL
语义：分析慢SQL / 看看今天慢查询 / SQL性能报告 / 哪些SQL最慢 / 最近慢SQL集中在哪里 / 帮我出优化报告

---

## 数据来源

### 模式 A：用户上传文件
支持：slow_query_*.csv（UTF-8 with BOM）

### 模式 B：本地目录自动读取（默认）
路径：`<DATA_ROOT>/slow_sql/YYYY-MM-DD/*.csv`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

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

期望字段（实际以脚本返回的 columns 为准，AI 自动适配）：
```
sql_fingerprint       - SQL 指纹（同类SQL唯一标识）
sql_text              - SQL 文本
execute_count         - 执行次数
total_logical_reads   - 总逻辑读
avg_logical_reads     - 平均逻辑读
total_cpu_ms          - 总CPU时间（ms）
avg_cpu_ms            - 平均CPU时间
total_elapsed_ms      - 总执行时间
avg_elapsed_ms        - 平均执行时间
total_writes          - 总物理写
execute_account       - 执行账号
client_hostname       - 客户端主机名
last_execution_time   - 最后执行时间
_date                 - 数据所属日期（脚本自动添加）
```

字段缺失时：基于现有字段继续分析，输出中标注哪些维度因字段缺失无法评估。

---

## 分析流程

### Step 1：数据验证
- 确认 data 字段非空
- 识别实际可用字段（以 columns 为准）
- 识别日期范围和数据天数
- 检查字段完整性
- 判断单天 / 多天模式

### Step 2：资源集中度分析（优先输出）

按 total_logical_reads 降序计算：

| 统计 | 输出 |
|------|------|
| TOP 3 占总读取比 | 如：TOP3 占全部逻辑读的 71% |
| TOP 10 占比 | |
| TOP 20 占比 | |

集中度判断：
- **高度集中型**：TOP3 占比 > 60% → 少量 SQL 消耗大量资源，优先优化 TOP3
- **分散型**：TOP10 占比 < 40% → 大量 SQL 同时偏慢，需系统性优化（统计信息/索引策略）

SQL 行为模式分类：
- **高频低效型**：execute_count 高 + avg_logical_reads 高 → 高优先级
- **低频超重型**：execute_count 低 + total_logical_reads 极高 → 定期报表类

来源分析：
- execute_account TOP3（哪个账号产生最多慢 SQL）
- client_hostname TOP3（哪台服务器压力最大）

### Step 3：TOP 慢 SQL 清单（核心输出）

取 total_logical_reads TOP 10（不足则全部输出）

每条输出：
1. 排名与危急等级（P1~P5）
2. SQL 类型识别
3. D365 关联判断（涉及哪个 D365 对象/功能）
4. 根因说明
5. 优化建议
6. 预估收益
7. 索引建议脚本

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

| 等级 | 条件 |
|------|------|
| P1 🔴 | avg_logical_reads > 500,000 或 total_logical_reads > 50,000,000 |
| P2 🟠 | avg_logical_reads > 100,000 或 (execute_count > 1000 且 avg_logical_reads > 10,000) |
| P3 🟡 | avg_logical_reads > 20,000 或 total_cpu_ms > 300,000 |
| P4 🔵 | avg_logical_reads > 5,000 |
| P5 ⚪ | 其他 |

### Step 4：趋势分析（多天数据时）

- 最近 N 天 TOP SQL 变化（新增 / 消失 / 恶化 / 改善）
- 日均逻辑读总量趋势（上升 / 稳定 / 下降）
- 高峰时段识别（按小时统计执行量分布）
- 是否有新出现的 P1 SQL（与前一天对比）
- 是否有已优化 SQL 出现反弹

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
