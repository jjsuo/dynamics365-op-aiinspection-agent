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

### Step 1：阻塞识别
- blocking_session_id != 0 → 存在活跃阻塞
- 构建阻塞链（head blocker → victim chain）
- 计算最大阻塞深度
- 统计受影响的 SPID 数量

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

### Step 5：历史趋势分析（多天数据时）
- 高频阻塞 SQL TOP10（按出现频率）
- 阻塞高峰时段（按小时统计）
- Wait Type 趋势变化
- 是否同类阻塞重复出现（表明根因未解决）
- 阻塞持续时间趋势

### Step 6：阻塞扩散风险评估
- 当前阻塞链深度（> 5 为高风险）
- 受影响用户 / 进程数量
- 是否影响 CRM 关键业务路径（APP01/APP02 → SQL01）

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
