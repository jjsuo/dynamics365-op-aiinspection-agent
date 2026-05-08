# HEARTBEAT

## 说明

HEARTBEAT 定义 Agent 的定期自检任务。
当 Agent 处于持续运行模式时，按以下周期执行后台检查。

---

## 每日自检任务（Daily）

每天首次启动时执行：

1. 检查 memory/environment.json 的 last_updated 日期
   - 若超过 7 天未更新，提示用户更新环境配置

2. 检查 memory/data_catalog.json 中各数据类型的最新日期
   - 若某类数据超过 2 天未更新，输出提醒：
     "⚠️ 慢SQL数据已 3 天未采集，建议检查采集脚本是否正常运行"

3. 检查 known_risks 中 P1 级别风险
   - 若存在未解决的 P1 风险，启动时显示醒目提醒

4. 读取最新一天的阻塞数据摘要（静默分析）
   - 若检测到严重阻塞（持续时间 > 60s 或频率 > 10次/小时），主动推送告警

---

## 每周自检任务（Weekly）

每周一次（或用户主动触发"周报"时）：

1. 生成趋势摘要：
   - 最近 7 天阻塞趋势（改善/稳定/恶化）
   - 最近 7 天慢 SQL TOP5 变化
   - 索引碎片趋势
   - D365 大表增长速率

2. 对比 performance_baseline：
   - SQL PLE 趋势
   - CPU / IO 平均值变化
   - IIS 平均响应时间变化

3. 更新 memory/inspection_history.json

---

## 告警阈值（主动触发告警）

| 指标 | 告警阈值 | 级别 |
|------|----------|------|
| 阻塞持续时间 | > 60 秒 | P1 |
| 阻塞频率 | > 10次/小时 | P1 |
| AsyncOperationBase 增长 | > 10万行/天 | P2 |
| AuditBase 增长 | > 50万行/天 | P2 |
| SQL CPU 均值 | > 85% | P1 |
| SQL IO 延迟 | > 50ms | P1 |
| PLE | < 300s | P2 |
| IIS 5xx 错误率 | > 5% | P1 |
| Windows 应用池崩溃 | 任意发生 | P1 |

---

## 告警输出格式

```
🔴 [P1告警] SQL01 阻塞告警
时间：2026-04-30 14:32
现象：检测到持续阻塞 82 秒，涉及 AsyncOperationBase 表
根因：疑似批量异步任务与在线用户操作锁冲突
建议：立即执行 KILL [spid] 或暂停 Async 服务
```
