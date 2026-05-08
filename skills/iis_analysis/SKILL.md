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
- iis_access_*.log（W3C 格式 IIS 日志）
- iis_summary.csv（汇总采集数据）
- app_pool_events.csv（应用池事件）

### 模式 B：本地目录自动读取
默认路径：`<DATA_ROOT>/iis_logs/YYYY-MM-DD/`
（DATA_ROOT 默认 `<项目根>/data`，可用 `--data-root` 或 `$DATA_ROOT` 覆盖）

调用统一数据读取工具（只读取，不分析，自动解析 CSV 与 W3C .log 两种格式）：
```bash
python3 tools/data_reader.py --category iis_logs --today
python3 tools/data_reader.py --category iis_logs --date 2026-04-28
python3 tools/data_reader.py --category iis_logs --last-7
python3 tools/data_reader.py --category iis_logs --list-dates
```

返回的 `files` 中每条记录包含 `kind: csv | w3c_log`；本 Skill 按文件名关键字识别类型：
- `iis_access_*` / `*.log`     → 访问日志
- `iis_summary*`               → 汇总指标
- `app_pool*`                  → 应用池事件

**数据读取后由本 Skill / AI 负责全部分析逻辑。**

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

### 4. 应用池健康

检查项：
- 应用池状态（Running / Stopped / Starting）
- 应用池崩溃次数（24小时内 > 0 为异常）
- 工作进程内存使用（> 1.5GB 建议回收）
- 工作进程 CPU 使用（> 80% 需关注）
- 应用池回收频率（过于频繁说明内存泄漏）

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

### Step 5：跨层关联
- IIS 响应慢高峰时段 ↔ SQL 阻塞高峰时段
- IIS 503 错误 ↔ 应用池内存耗尽时段
- IIS 504 超时 ↔ SQL 慢查询时段

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
