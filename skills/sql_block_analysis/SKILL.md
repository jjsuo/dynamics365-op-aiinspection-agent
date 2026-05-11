# SKILL: sql_block_analysis

---

## 定位

SQL Server + Dynamics 365 阻塞分析专家。支持实时数据分析、历史日志趋势分析、D365 专项阻塞识别。

---

## 输入触发

标签：#SQL_BLOCK / #CSV_BLOCK / #HISTORY_BLOCK
语义：分析阻塞 / 今天阻塞 / 最近几天阻塞 / 查看某日阻塞 / blocking趋势 / 死锁分析

---

## 数据来源

### 模式 A：用户实时粘贴
- sp_whoisactive 输出
- sys.dm_exec_requests 查询结果
- sys.dm_os_waiting_tasks 输出
- 阻塞链截图或文本描述

### 模式 B：本地历史文件自动读取
默认路径：`<DATA_ROOT>/sql_blocking/YYYY-MM-DD/*.csv`
（DATA_ROOT 默认 `<项目根>/data`，可通过 `--data-root` 或 `$DATA_ROOT` 覆盖）

新采集脚本（`ServerCollectionScript/03_SQL阻塞采集.md`）产出格式：
```
<DATA_ROOT>/sql_blocking/YYYY-MM-DD/
├── blocking_0900.csv    ← 每 5 分钟一个快照，时间戳 HHmm
├── blocking_0905.csv
├── blocking_0910.csv
└── ...
```

老格式（`blocking_01.csv` 等）仍支持；`data_reader` 会将当日所有 `*.csv` 全量加载到 `files[]`。

调用统一数据读取工具（只读取，不分析）：
```bash
python3 tools/data_reader.py --category sql_blocking --today
python3 tools/data_reader.py --category sql_blocking --yesterday
python3 tools/data_reader.py --category sql_blocking --last-3
python3 tools/data_reader.py --category sql_blocking --last-7
python3 tools/data_reader.py --category sql_blocking --date 2026-04-24
python3 tools/data_reader.py --category sql_blocking --start 2026-04-01 --end 2026-04-24
python3 tools/data_reader.py --category sql_blocking --list-dates   # 查看可用日期
```

脚本返回结构（详见 TOOLS.md）：
- `status` / `data_root` / `category` / `date_range`
- `loaded`：成功读取的文件列表（含日期前缀）
- `missing`：缺失日期列表
- `files`：每个文件的 `{file, date, path, total_rows, columns, data}`
- `static_files`：category 根目录下的静态文件（阻塞类别通常为空）

**数据读取后由本 Skill / AI 负责全部分析逻辑**，包括：阻塞识别、等待类型分类、D365 表识别、趋势判断。

---

## CSV 列约定（采集脚本产物，23 列）

新采集脚本严格对齐 `blocking_01.csv` 的列顺序和名称：

| # | 列名 | 说明 |
|---|------|------|
| 1  | SnapshotId（或 ßßß）| 本次快照时间戳标识 |
| 2  | blocktypes       | 阻塞层级（0=根，1=叶节点，±=中间） |
| 3  | spid             | 当前会话 SPID |
| 4  | blocked          | 被谁阻塞（0 = 不被阻塞） |
| 5  | waittype         | 等待类型（原始字节） |
| 6  | waittime         | 已等待时间（ms） |
| 7  | lastwaittype     | 上次等待类型 |
| 8  | waitresource     | 等待资源 |
| 9  | dbname           | 数据库名 |
| 10 | username         | 用户名 |
| 11 | cpu              | CPU 消耗 |
| 12 | physical_io      | 物理 IO |
| 13 | memusage         | 内存占用 |
| 14 | login_time       | 登录时间 |
| 15 | last_batch       | 最后批次时间 |
| 16 | open_tran        | 未提交事务数 |
| 17 | processstatus    | 会话状态（running/sleeping 等） |
| 18 | hostname         | 客户端主机名 |
| 19 | program_name     | 客户端程序名 |
| 20 | cmd              | 当前命令 |
| 21 | net_library      | 网络库 |
| 22 | loginame         | 登录名 |
| 23 | executing_sql_text | 当前执行的 SQL 文本 |

本 Skill 读入后对列名做兼容处理：优先按 `SnapshotId`，若不存在回落到 `ßßß`。

---

## 时间理解规则

| 输入 | 解析 |
|------|------|
| 今天阻塞 | 当天日期 |
| 昨天阻塞 | 当天 -1 |
| 最近3天 | today-2 ~ today |
| 最近7天 | today-6 ~ today |
| 2026-04-24 | 精确日期 |
| 4月20号到4月24号 | 日期范围 |

---

## 分析流程

### Step 1：阻塞识别（基于新列约定）
- `blocked != 0` → 该会话被阻塞；`blocked = 0` 但被多个 SPID 指向 → head blocker
- `blocktypes` 就是采集脚本已算好的阻塞层级，直接拿来用：
  - `blocktypes = 0` → 根阻塞者
  - `blocktypes = 1` → 被根阻塞的首层受害者
  - 更大的 blocktypes 值 → 多层阻塞链中的中间层
- 最大阻塞深度 = MAX(blocktypes)
- 受影响 SPID 数 = COUNT(DISTINCT spid WHERE blocked != 0)
- 多文件（同日 `blocking_HHmm.csv` 多个）时：按 SnapshotId 分组，每组一个快照

### Step 2：等待类型分类

| Wait Type | 含义 | 根因方向 |
|-----------|------|----------|
| LCK_M_* | 锁等待 | 并发冲突、长事务、缺行级索引 |
| PAGEIOLATCH_* | 页 IO 等待 | 磁盘 IO 瓶颈、缺索引导致全扫描 |
| CXPACKET / CXCONSUMER | 并行等待 | MAXDOP 过高、大查询并行拆分 |
| SOS_SCHEDULER_YIELD | CPU 调度等待 | CPU 压力过高 |
| WRITELOG | 日志写等待 | 日志磁盘 IO 瓶颈 |
| ASYNC_NETWORK_IO | 网络 IO 等待 | 客户端消费慢（D365 大结果集） |
| RESOURCE_SEMAPHORE | 内存授权等待 | 内存不足，大查询排队 |
| THREADPOOL | 线程池耗尽 | 并发连接过多 |

### Step 3：Head Blocker 分析
- 识别根源阻塞者（blocking_session_id = 0 但被其他 SPID 阻塞）
- 分析其执行的 SQL 文本
- 判断是否为 D365 系统操作

### Step 4：D365 专项检查

重点关注以下表的阻塞：

| 表名 | 常见阻塞原因 |
|------|-------------|
| AsyncOperationBase | 批量异步任务与在线用户并发 |
| PrincipalObjectAccess | 权限查询与写入冲突 |
| WorkflowLogBase | 工作流日志高频写入 |
| AuditBase | 审计日志与业务操作并发 |
| ActivityPointerBase | 活动记录高并发插入 |
| SystemUserBase | 登录/权限查询与用户管理并发 |

### Step 5：历史趋势分析（多天或同日多快照数据时）
- 高频阻塞 SQL TOP10（按 executing_sql_text 出现频率）
- 阻塞高峰时段（按小时统计，新格式文件名 `blocking_HHmm.csv` 直接给出小时粒度）
- Wait Type 趋势变化
- 是否同类阻塞重复出现（表明根因未解决）
- 阻塞持续时间趋势（按 waittime 最大值）

### Step 6：阻塞扩散风险评估
- 当前阻塞链深度（> 5 为高风险）
- 受影响用户 / 进程数量
- 是否影响 CRM 关键业务路径（APP01/APP02 → SQL01）

### Step 7：★ 跨源关联提示

文件名 `blocking_HHmm.csv` 的 `HHmm` 可直接映射到 PerfMon 的 5 分钟桶：
- 比如 `blocking_1425.csv` → 对应 `BucketStart = '2026-05-08 14:25:00'`
- 提示用户：可以在 `sql_perf_analyzer` 的 `perfmon_5min_*.csv` 中查同桶 CPU / IO / PLE
- 可以在 `slow_sql_analyst` 的 `slowsql_5min_*.csv` 中查同桶 TopSQL
- 完整关联分析请调用 `health_report`

---

## 输出结构

```
## SQL 阻塞分析报告

### 基本信息
分析时间范围：YYYY-MM-DD
数据来源：本地历史文件 / 实时输入
服务器：SQL01（来自 environment.json）

### 阻塞概况
总阻塞事件：XX 次
最长阻塞持续时间：XX 秒
高峰时段：09:00–10:00（XX 次）
主要等待类型：LCK_M_IX（67%）/ PAGEIOLATCH_SH（23%）

### 阻塞链分析
Head Blocker：SPID 58（AsyncService 账号）
  └─ 阻塞：SPID 62, 71, 85（CRM 用户操作）
  执行 SQL：UPDATE AsyncOperationBase SET StatusCode=30 WHERE...
  已持续：127 秒（危险）

### 根因分析
主要根因：AsyncOperationBase 批量更新操作持有表级锁，
导致在线用户的查询无法获得读锁。
关联分析：结合 environment.json，ASYNC01 为单点服务器，
批量任务无法分散，高峰期与业务冲突严重。

### 影响评估
[P1] 阻塞链深度 4，受影响 SPID 8 个，持续 127 秒
[P2] 同类阻塞在最近 7 天出现 23 次，趋势恶化

### 优化建议
P1：KILL SPID 58 立即解除当前阻塞
    后续：调整 Async 服务批量任务到业务低峰期（22:00-06:00）
P2：为 AsyncOperationBase 添加行级锁覆盖索引
    CREATE INDEX IX_AsyncOp_Status ON AsyncOperationBase(StatusCode, CompletedOn)
P3：长期考虑增加第二台 Async 服务器以分散负载

### 是否需要补充数据
若需要更精确分析，请提供：
- 阻塞发生时的 sp_whoisactive 完整输出
- 阻塞 SQL 的执行计划（XML格式）
```

---

## 辅助 SQL（用户参考）

```sql
-- 查看当前阻塞链
SELECT
    r.session_id,
    r.blocking_session_id,
    r.wait_type,
    r.wait_time / 1000.0 AS wait_seconds,
    r.status,
    SUBSTRING(t.text, 1, 200) AS sql_text,
    r.command
FROM sys.dm_exec_requests r
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.blocking_session_id > 0
ORDER BY r.wait_time DESC;
GO

-- 查看等待类型统计
SELECT TOP 20
    wait_type,
    waiting_tasks_count,
    wait_time_ms / 1000.0 AS wait_seconds,
    signal_wait_time_ms / 1000.0 AS signal_seconds
FROM sys.dm_os_wait_stats
WHERE wait_type NOT IN (
    'SLEEP_TASK','BROKER_TO_FLUSH','BROKER_TASK_STOP',
    'CLR_AUTO_EVENT','DISPATCHER_QUEUE_SEMAPHORE',
    'FT_IFTS_SCHEDULER_IDLE_WAIT','HADR_FILESTREAM_IOMGR_IOCOMPLETION',
    'HADR_WORK_QUEUE','LAZYWRITER_SLEEP','LOGMGR_QUEUE',
    'ONDEMAND_TASK_QUEUE','REQUEST_FOR_DEADLOCK_SEARCH',
    'RESOURCE_QUEUE','SERVER_IDLE_CHECK','SLEEP_DBSTARTUP',
    'SLEEP_DBTASK','SLEEP_ERRORLOG','SLEEP_MASTERDBREADY',
    'SLEEP_MASTERMDREADY','SLEEP_MASTERUPGRADED','SLEEP_MSDBSTARTUP',
    'SLEEP_SYSTEMTASK','SLEEP_TEMPDBSTARTUP','SNI_HTTP_ACCEPT',
    'SP_SERVER_DIAGNOSTICS_SLEEP','SQLTRACE_BUFFER_FLUSH',
    'SQLTRACE_INCREMENTAL_FLUSH_SLEEP','WAITFOR',
    'XE_DISPATCHER_WAIT','XE_TIMER_EVENT'
)
ORDER BY wait_time_ms DESC;
GO
```
