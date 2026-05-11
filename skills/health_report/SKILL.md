# SKILL: health_report

---

## 定位

Dynamics 365 On-Premises 系统综合健康巡检报告聚合专家。

当用户请求"系统巡检"、"整体健康报告"、"系统现在有什么问题"时，
本 Skill 负责协调所有子 Skill，聚合结果，输出统一的跨层关联健康报告。

---

## 输入触发

语义：
- 系统巡检
- 整体健康报告
- 系统现在有什么问题
- 全面分析
- 出一份巡检报告
- 今天系统怎么样
- 帮我看看系统

---

## 执行流程

### Step 1：加载环境基准
读取 memory/environment.json 和 memory/data_catalog.json
确认各类型数据的最新可用日期。

### Step 2：并行调用各 Skill（按数据可用性）

所有数据读取统一通过 `tools/data_reader.py --category <name>`（详见 TOOLS.md）。

| Skill | category | 若数据缺失 |
|-------|----------|-----------|
| sql_block_analysis | sql_blocking | 标注"无阻塞数据" |
| slow_sql_analyst | slow_sql | 标注"无慢SQL数据" |
| sql_index_optimizer | sql_index | 标注"无索引数据" |
| sql_storage_analysis | sql_index（复用 table_size*.csv） | 标注"无存储数据" |
| sql_perf_analyzer | server_per_sql | 标注"无性能数据" |
| iis_analysis | iis_logs | 标注"无IIS数据" |
| windows_health | windows_health | 标注"无Windows数据" |
| plugin_scanner | plugin_scan | 标注"无Plugin数据" |

### Step 3：★ 跨源时间桶关联分析（新采集格式的核心优势）

**关键能力**：`perfmon_5min.BucketStart = slowsql_5min.BucketStart = blocking_HHmm.csv 的 HHmm`，三者能直接 JOIN。

做法：
1. 从 `perfmon_5min_*.csv` 中找出所有 **异常桶**（CPU P95>85% 或 Disk sec/Read>50ms 或 PLE<300s）
2. 每个异常桶用 `BucketStart` 去 JOIN `slowsql_5min_*.csv` 和 `blocking_HHmm.csv`
3. 输出「那 5 分钟时 CPU 为什么高」的确凿框图奖回答

输出表示例：

```
[异常桶] 2026-05-08 14:25:00  CPU P95 = 92%
  → 同桶 TOP 3 慢 SQL（按 TotalCpuMs）：
     1. SqlFingerprint=ab12… ExecCount=1234 TotalCpuMs=450,000  AvgDurationMs=180
     2. SqlFingerprint=cd34… ExecCount=567  TotalCpuMs=120,000  AvgDurationMs=210
     3. …
  → 同桶 blocking_1425.csv：Head Blocker SPID=58，阻塞深度=3
  → 根因：SQL #1（ContactBase 全表扫描）合计 7.5 分钟 CPU，进而导致会话排队阻塞
```

### Step 4：其他跨模块关联分析
识别跨层问题链路（见下文关联规则）。

### Step 5：综合评分与排序
汇总各模块评分，生成总体健康度评分。

### Step 6：更新记忆
将本次巡检结果写入 memory/environment.json 的 inspection_history。

---

## 跨模块关联规则

### ★ 关联链路 0（最重要）：CPU/IO 尖峰 → TopSQL → 阻塞

触发条件（有 `perfmon_5min_*.csv` 或 `slowsql_5min_*.csv` 时自动运行）：
- PerfMon 某个 BucketStart 的 `% Processor Time` P95 > 85% 或 `Avg Disk sec/Read` > 50ms
- 同 BucketStart 存在 slowsql_5min TotalCpuMs > 60,000 的条目

输出格式：
```
[关联链路] 2026-05-08 14:25 CPU 尖峰 → Top SQL → 会话排队
服务器：SQL01
  PerfMon：\Processor\% Processor Time P95=92%，Disk sec/Read MAX=68ms
    → 同 5 分钟桶 Top CPU SQL：
       - ContactBase 全表扫描（SqlFingerprint=ab12...）
         ExecCount=1234, TotalCpuMs=450000, AvgDurationMs=180
       - …
      → 同时段 blocking_1425.csv: Head Blocker SPID=58查 ContactBase
        → 根因定位：ContactBase 缺少 OwnerId+StateCode 覆盖索引
              → 导致 CPU 尖峰与会话排队同时发生
```

---

### 关联链路 1：IIS慢 → SQL阻塞 → IO瓶颈

触发条件：
- IIS 平均响应时间 > 3s
- 同时段存在 SQL 阻塞记录
- SQL IO 延迟 > 20ms

输出关联描述：
```
[关联链路] IIS 响应慢 → SQL 阻塞 → 磁盘 IO 瓶颈
IIS (APP01/APP02) 在 09:00-10:00 响应时间均值 4.2s
  → 同时段 SQL01 存在 LCK_M_IX 阻塞，持续最长 82s
    → SQL01 磁盘 IO 延迟 avg 38ms（D盘），超过 SSD 正常阈值 2ms
      → 根因定位：数据盘 IO 瓶颈导致锁等待加剧，进而阻塞 IIS 请求
```

### 关联链路 2：AsyncOperation膨胀 → 阻塞 → 用户卡顿

触发条件：
- AsyncOperationBase 行数 > 500万
- 存在 AsyncService 账号发起的阻塞记录
- 用户反映系统卡顿

输出关联描述：
```
[关联链路] AsyncOperationBase 膨胀 → Async 阻塞 → 用户操作卡顿
AsyncOperationBase 当前 480万行（接近预警阈值）
  → Async 批量 UPDATE 操作持有行锁，高峰期与在线用户冲突
    → 导致 APP01/APP02 请求等待，IIS 响应时间上升
      → 根因：未定期清理已完成异步作业，Async 扫描范围过大
```

### 关联链路 3：Plugin错误 → Sandbox崩溃 → 服务中断

触发条件：
- Windows 事件日志中存在 MSCRMSandboxService 停止事件
- 同时段存在大量 Plugin 异常
- IIS 500 错误率上升

输出关联描述：
```
[关联链路] Plugin 异常 → Sandbox 崩溃 → 用户操作报错
ContactPlugin 在 09:14 触发未处理异常
  → MSCRMSandboxService 崩溃，3分钟后自动重启
    → 崩溃期间所有同步 Plugin 请求返回 500 错误
      → 根因：Plugin 代码缺少 InvalidPluginExecutionException 规范处理
```

### 关联链路 4：索引碎片 → 慢SQL → IO压力

触发条件：
- 核心表索引碎片 > 50%
- 同表存在慢 SQL 记录（全表扫描）
- IO 延迟偏高

输出关联描述：
```
[关联链路] 索引碎片 → 全表扫描 → IO 压力上升
ContactBase 主要索引碎片率 68%
  → 针对 ContactBase 的查询退化为全表扫描（慢SQL #3）
    → 大量 PAGEIOLATCH_SH 等待，IO 延迟上升
      → 根因：索引维护作业未执行或执行频率不足
```

---

## 综合评分模型

### 各模块权重

| 模块 | 权重 | 说明 |
|------|------|------|
| SQL 阻塞 | 25% | 直接影响用户体验 |
| SQL 性能（PerfMon） | 20% | 服务器整体健康 |
| 慢 SQL | 15% | 查询效率 |
| 索引健康 | 15% | 数据访问效率 |
| IIS 健康 | 10% | 应用层稳定性 |
| Windows 服务 | 10% | 基础设施稳定性 |
| 存储/大表 | 5% | 长期风险 |

### 总分评级

| 分数 | 评级 | 含义 |
|------|------|------|
| 90–100 | 🟢 健康 | 系统运行良好 |
| 75–89 | 🟡 需关注 | 存在潜在风险，建议本周处理 |
| 60–74 | 🟠 有风险 | 存在明确问题，建议尽快处理 |
| < 60 | 🔴 需立即处理 | 存在严重问题，影响业务正常运行 |

---

## 输出标准模板

```markdown
# D365 系统健康巡检报告

**环境**：[客户名] - PROD
**巡检时间**：YYYY-MM-DD HH:MM
**数据范围**：最近 X 天（YYYY-MM-DD ~ YYYY-MM-DD）
**报告版本**：自动生成 v2.0

---

## 一、总体健康评分

| 评分 | 评级 |
|------|------|
| **XX / 100** | 🟡 需关注 |

| 模块 | 评分 | 状态 | 主要问题 |
|------|------|------|----------|
| SQL 阻塞 | XX/100 | 🟠 | 日均 15 次阻塞，最长 82s |
| SQL 性能 | XX/100 | 🟡 | PLE 偏低，IO 延迟偶发超标 |
| 慢 SQL | XX/100 | 🟠 | TOP3 SQL 逻辑读超 50万/次 |
| 索引健康 | XX/100 | 🟡 | 3 张核心表碎片 > 50% |
| IIS 健康 | XX/100 | 🟢 | 基本正常，偶发 503 |
| Windows 服务 | XX/100 | 🔴 | Sandbox 服务昨日崩溃 1 次 |
| 存储/大表 | XX/100 | 🟡 | AuditBase 接近预警阈值 |

---

## 二、核心问题 TOP5

### 🔴 P1-001：MSCRMSandboxService 崩溃
**影响**：所有同步 Plugin 停止执行约 3 分钟，用户操作报错
**根因**：ContactPlugin v2.1 未捕获 NullReferenceException
**建议**：立即检查 Plugin 代码并修复异常处理逻辑

### 🟠 P1-002：SQL01 磁盘 L:（日志盘）空间不足
**影响**：剩余 8GB，低于危险阈值 10GB，SQL Server 日志增长受限
**根因**：日志备份文件未及时清理
**建议**：立即清理旧备份文件，预留至少 30GB

### 🟠 P2-003：AsyncOperationBase 接近预警阈值
**影响**：480万行，预计 10 天后超 500万行预警线
**根因**：未配置已完成异步作业自动清理策略
**建议**：本周内配置 BulkDelete 定期清理

### 🟡 P2-004：ContactBase 索引碎片 68%
**影响**：ContactBase 查询响应时间偏慢，关联慢SQL #3
**根因**：索引维护作业未在最近 7 天执行
**建议**：非高峰期执行 REBUILD

### 🟡 P2-005：慢SQL TOP1 逻辑读 82万次/执行
**影响**：高峰期每分钟执行 45 次，合计 IO 资源消耗极大
**根因**：PrincipalObjectAccess 表缺失覆盖索引
**建议**：添加缺失索引（脚本见附件）

---

## 三、跨模块关联分析

### 关联链路 1：Plugin 崩溃 → 用户报错
[详细描述]

### 关联链路 2：索引碎片 → 慢SQL → IO压力
[详细描述]

---

## 四、优化建议清单

### P1（今天必须处理）
- [ ] 检查并修复 ContactPlugin 异常处理代码
- [ ] 清理 SQL01 L: 盘旧备份文件，释放空间

### P2（本周处理）
- [ ] 配置 AsyncOperationBase BulkDelete 策略
- [ ] 执行 ContactBase 索引 REBUILD
- [ ] 为 PrincipalObjectAccess 添加缺失索引

### P3（本月规划）
- [ ] 评估 AuditBase 归档方案
- [ ] 规划 ASYNC01 高可用方案（当前单点）
- [ ] 评估 SQL01 数据盘 IO 性能是否需要扩容

---

## 五、数据覆盖说明

| 数据类型 | 最新日期 | 覆盖天数 | 状态 |
|----------|----------|----------|------|
| SQL 阻塞 | 2026-04-30 | 14天 | ✅ |
| 慢 SQL | 2026-04-30 | 7天 | ✅ |
| 索引数据 | 2026-04-29 | 7天 | ✅ |
| 性能数据 | 2026-04-28 | 5天 | ✅ |
| IIS 日志 | 2026-04-30 | 3天 | ✅ |
| Windows | 2026-04-30 | 1天 | ⚠️ 建议增加采集天数 |

---

## 六、本次巡检记录

已更新 memory/environment.json：
- 新增风险：RISK-004（ContactPlugin 异常）
- 更新基线：SQL PLE 均值 2800s
- 巡检评分：XX/100
```

---

## 记忆更新（执行完必须写入）

```json
{
  "date": "YYYY-MM-DD",
  "overall_score": 72,
  "module_scores": {
    "sql_block": 65,
    "sql_perf": 75,
    "slow_sql": 68,
    "index": 74,
    "iis": 88,
    "windows": 55,
    "storage": 78
  },
  "top_issues": [
    "MSCRMSandboxService 崩溃",
    "SQL01 日志盘空间不足",
    "AsyncOperationBase 接近预警"
  ],
  "new_risks": ["RISK-004"],
  "resolved_risks": []
}
```
