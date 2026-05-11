# SKILL: iis_analysis

---

## 定位

IIS + Dynamics 365 应用层健康分析专家。

分析范围：IIS 服务健康、应用池状态、请求性能、错误率、与 SQL 层的关联分析。

---

## 输入触发

标签：#IIS
语义：IIS分析 / 应用池 / 请求队列 / IIS慢 / 响应超时 / IIS日志 / 5xx错误

---

## 数据来源

### 模式 A：用户上传文件
- `u_exYYMMDD*.log`（W3C 格式 IIS 访问日志）
- `apppool_status.csv`（应用池运行快照）
- `iis_worker_processes.csv`（w3wp.exe 工作进程资源快照）
- 老格式兼容：`iis_access_*.log` / `iis_summary.csv` / `app_pool_events.csv`

### 模式 B：本地目录自动读取
默认路径：`<DATA_ROOT>/iis_logs/YYYY-MM-DD/`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

新采集脚本（`ServerCollectionScript/05_IIS日志采集.md`）产出格式：
```
<DATA_ROOT>/iis_logs/YYYY-MM-DD/
├── u_exYYMMDD_W3SVC1.log         ← W3C 日志（按站点编号分文件）
├── u_exYYMMDD_W3SVC2.log
├── apppool_status.csv            ← 应用池快照
└── iis_worker_processes.csv      ← 工作进程 CPU/内存快照
```

调用统一数据读取工具（只读取，不分析，自动解析 CSV 与 W3C .log 两种格式）：
```bash
python3 tools/data_reader.py --category iis_logs --today
python3 tools/data_reader.py --category iis_logs --date 2026-04-28
python3 tools/data_reader.py --category iis_logs --last-7
python3 tools/data_reader.py --category iis_logs --list-dates
```

返回的 `files` 中每条记录包含 `kind: csv | w3c_log`；本 Skill 按文件名关键字识别类型：
- `u_ex*.log` / `iis_access_*` / `*.log`     → W3C 访问日志（主数据）
- `apppool_status*`                           → ★ 应用池运行状态快照（新）
- `iis_worker*` / `w3wp*`                     → ★ 工作进程资源快照（新）
- `iis_summary*`                              → 汇总指标（老格式）
- `app_pool*` / `app_pool_events*`            → 应用池事件（老格式）

**数据读取后由本 Skill / AI 负责全部分析逻辑。**

---

## ★ 跨源关联数据（强制加载）

IIS 层的慢/错/端很少是自我问题：**80% 故障根源在 SQL 层或业务高峰**。本 Skill 必须关联 SQL / PerfMon / business_context 才能给出有效结论。

### 必加载清单

| 文件 | 用途 |
|------|------|
| memory/environment.json | APP01/APP02 物理配置 + 拓扑（单 APP 还是 NLB） |
| memory/thresholds.json | `thresholds.iis`（req_per_sec / p95_ms / error_5xx_pct / app_pool_memory_mb） |
| memory/business_context.json | `peak_hours` 判断请求风暴是否预期；`sla.iis_response_p95_ms` 判 SLA；`critical_urls` 优先监控；`user_concurrency` 判断发隔正常 |
| memory/risk_profile.json | 历史 历史常崩的应用池升级等级 |

### 跨源数据（识别问题后必加载）

任一触发→必须按 `BucketStart`/小时桶对齐：

| IIS 观察 | 回查源 | 命令 |
|---------|--------|------|
| 504 / time-taken > SLA | slowsql_5min + blocking | `data_reader.py --category slow_sql/sql_blocking --date X`，对齐 BucketStart |
| 500 集中爆 | 同期 perfmon_5min 的 CPU/PLE/Memory Grants Pending + windows_health event_logs EventID 1000/1026 | 确认是应用池崩溃还是 SQL 压垮回流 |
| 503 端 | apppool_status.State + iis_worker_processes.WorkingSet_MB + perfmon `Requests Queued` | 确认队列满 / 进程挂 / 回收中 |
| 401/403 激增 | windows_health event_logs Security + ADFS `36888` SChannel | ADFS/Kerberos 问题 |
| 应用池内存高 | 同期 plugin_scan 中 Plugin 步骤与应用池的关联 | D365 Plugin 火算或泄漏嫌疑 |

### 执行脚本示例

```bash
# 同时拉回同天所有跨源数据
DATE=2026-05-08
for cat in iis_logs server_per_sql slow_sql sql_blocking windows_health; do
  python3 tools/data_reader.py --category $cat --date $DATE
done
```

> 时间桶对齐规则：W3C 日志的 `time` 按 `FLOOR(minute/5)*5` 归桶 → 与 `perfmon_5min.BucketStart` / `slowsql_5min.BucketStart` / `blocking_HHmm` JOIN。

---

## 分析维度与阈值

### 1. 请求吞吐量

| 指标 | 正常 | 警告 | 危险 |
|------|------|------|------|
| Requests/sec | < 100 | 100–200 | > 200 |
| 活跃连接数 | < 500 | 500–1000 | > 1000 |
| 请求队列长度 | < 100 | 100–500 | > 500 |
| 当前 I/O 操作 | < 100 | 100–300 | > 300 |

### 2. 响应时间

| 指标 | 正常 | 警告 | 危险 |
|------|------|------|------|
| 平均响应时间 | < 2s | 2–5s | > 5s |
| P95 响应时间 | < 5s | 5–10s | > 10s |
| 最大响应时间 | < 30s | 30–60s | > 60s |

### 3. HTTP 错误率

| 状态码 | 含义 | 处理建议 |
|--------|------|----------|
| 4xx > 5% | 客户端错误偏高 | 检查认证/权限配置 |
| 500 > 1% | 应用内部错误 | 检查 CRM 应用日志 |
| 503 任意 | 服务不可用 | 立即检查应用池状态 |
| 504 任意 | 网关超时 | 检查 SQL 响应是否超时 |

### 4. 应用池健康（`apppool_status.csv` / `iis_worker_processes.csv`）

**apppool_status.csv 关键字段**（实际以 columns 为准，常见）：
- Name（应用池名） / State / AutoStart / ManagedRuntimeVersion / StartMode
- CpuLimit / IdleTimeout / RapidFailProtection / RecyclingRequests / RecyclingTime

**iis_worker_processes.csv 关键字段**：
- AppPoolName / ProcessId / CPU_Pct / WorkingSet_MB / PrivateMemory_MB / ThreadCount / HandleCount / StartTime

检查项：
- 应用池状态（Running / Stopped / Starting）—— 非 Running 直接 P1
- 工作进程内存使用（WorkingSet_MB > 1500 建议回收）
- 工作进程 CPU 使用（CPU_Pct > 80% 需关注）
- 应用池回收频率（过于频繁说明内存泄漏）
- StartTime 太新 → 最近重启过，可能是崩溃或手动回收

### 5. D365 特定检查

重点分析：
- /XRMServices/ 路径请求响应时间（OData / SOAP）
- /api/data/v9.x/ 路径请求响应时间（Web API）
- 大批量导入/导出操作（file_size > 10MB 的请求）
- 同步 Plugin 触发的长请求（time_taken > 30000ms）

---

## 分析流程

### Step 1：服务基础状态
- IIS 服务是否运行
- 应用池是否全部 Running
- 是否有近期崩溃记录

### Step 2：请求量分析
- 按小时统计请求量分布
- 识别高峰时段
- 对比 environment.json 中的服务器配置（CPU/内存）

### Step 3：响应时间分析
- 计算平均/P95/最大响应时间
- 识别超时请求（> 30s）
- 定位慢请求来自哪个路径（FetchXML / OData / SDK）

### Step 4：错误分析
- 统计各 HTTP 状态码分布
- 重点分析 500/503/504
- 关联是否与 SQL 阻塞时段重叠

### Step 5：跨层关联（强制）

IIS 层每一个问题都必须有可观测的同期证据，本步骤不是可选。

- IIS 响应慢高峰时段 ↔ SQL 阻塞高峰时段（BucketStart JOIN）
- IIS 503 错误 ↔ 应用池内存耗尽时段（iis_worker_processes.WorkingSet_MB）
- IIS 504 超时 ↔ SQL 慢查询时段（slowsql_5min）
- ★ W3C 日志的 `time` 字段按 5 分钟聚合，和 `perfmon_5min_*.csv` / `slowsql_5min_*.csv` 的 `BucketStart` 对齐：将 W3C 日志按 `FLOOR(time_in_minutes / 5) * 5` 归桶，即可与 SQL 侧数据 JOIN。

### Step 5.1：★ 每类 IIS 异常必须输出的跨源证据例

```
[P1] /api/data/v9.1/accounts PATCH P95=8.4s（超 SLA 2s） 高峰时段 14:25-14:40

⏰ 时间判定：命中 peak_hours（工作日 14:00-17:00） → 真业务高峰
🔍 同期证据（BucketStart=2026-05-08 14:25:00）：
  ┌─ SQL 层：
  │   · perfmon_5min：%Processor Time P95=92% / PLE=180s（超阈）
  │   · slowsql_5min：TOP SQL UPDATE AccountBase 执行 1840 次 / 总 CPU 920秒
  │   · blocking_1425：3 条阻塞链最长 18s，涉及 AccountBase
  ├─ 应用池：
  │   · CRMAppPool WorkingSet 2.8GB（近阈值 3GB），未崩
  └─ Windows 层：event_logs 无 EventID 1000/1026

🎯 根因结论：请求慢是 SQL 层 AccountBase 阻塞导致，非 IIS / 应用池问题
🛠 联动建议：交给 sql_block_analysis / sql_index_optimizer 处理 AccountBase
```

若找不到同期 SQL 证据 → 转向检查 Plugin 是否有同期注册或重中度升级；仍找不到 → 标注「根因不明，建议下次高峰开启 Failed Request Tracing」。

---

## 输出结构

```
## IIS 健康分析报告

### 基本信息
服务器：APP01 / APP02（来自 environment.json）
分析时间范围：YYYY-MM-DD

### 健康评分
| 维度 | 评分 | 状态 |
|------|------|------|
| 服务可用性 | XX/25 | 🟢/🟡/🔴 |
| 请求吞吐量 | XX/25 | |
| 响应时间 | XX/25 | |
| 错误率 | XX/25 | |
| 综合评分 | XX/100 | |

### 问题清单
[P1] 应用池 CRMAppPool 在 14:32 崩溃，影响服务 3 分钟
[P2] /api/data/v9.1/ 平均响应时间 4.2s，超过警告阈值 2s
[P2] 503 错误率 2.1%，集中在 09:00–10:00 业务高峰期

### 根因分析
...（结合 environment.json 服务器配置 + SQL 层数据）

### 优化建议
P1：...
P2：...
P3：...

### 跨层关联
IIS 响应慢（09:00-10:00）与 SQL01 阻塞时段高度重合
→ 根因：SQL 阻塞导致 IIS 请求等待，建议同步分析 SQL 阻塞数据
```

---

## 常见 D365 IIS 问题速查

| 症状 | 根因 | 建议 |
|------|------|------|
| 应用池频繁崩溃 | 内存泄漏或未处理异常 | 检查 Windows 事件日志 + CRM 跟踪日志 |
| OData 响应 > 10s | FetchXML 翻译慢或缺索引 | 结合慢 SQL 分析 |
| 503 错误 | 请求队列满或应用池停止 | 检查请求队列长度和应用池状态 |
| 504 超时 | SQL 响应超过 IIS 超时设置 | 检查 SQL 阻塞或慢查询 |
| 认证失败 4xx 激增 | ADFS 或 Kerberos 配置问题 | 检查 ADFS 服务状态 |
| 大文件上传失败 | maxAllowedContentLength 限制 | 检查 web.config 配置 |
