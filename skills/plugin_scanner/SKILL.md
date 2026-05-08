# SKILL: plugin_scanner

---

## 定位

Dynamics 365 Plugin 代码质量与性能风险扫描专家。

分析范围：Plugin 代码反模式、性能陷阱、注册配置风险、事务安全性、异常处理规范。

---

## 输入触发

标签：#PLUGIN
语义：Plugin扫描 / 插件代码检查 / 插件性能问题 / 代码审查 / Plugin注册分析 / 插件优化

---

## 数据来源

### 模式 A：源码文件（推荐）
用户上传：
- *.cs 文件（单个 Plugin 源码）
- *.zip 文件（Plugin 项目压缩包）
- *.csproj 项目文件

### 模式 B：注册配置分析
用户提供 Plugin Registration Tool 的导出信息：
- 步骤注册列表（消息、实体、执行阶段、模式）
- 过滤属性配置

### 模式 C：自动扫描
Plugin 扫描不使用 data_reader（数据来源是源码而非采集 CSV）。用户可提供本地路径或文件：
```bash
# 示例：后续若需要可在 tools/ 下新增独立的扫描脚本
# python3 tools/plugin_scanner.py --path /path/to/plugin/source
# python3 tools/plugin_scanner.py --file MyPlugin.cs
# python3 tools/plugin_scanner.py --zip plugins.zip
```
或由 AI 直接读取用户上传的 .cs / .zip 文件进行规则匹配。

---

## 反模式检测规则

### P1 级别（严重性能风险）

#### 1. 同步 Plugin 内 RetrieveMultiple 无分页
```csharp
// 危险模式：
var results = service.RetrieveMultiple(query);
// 若结果集 > 5000 条会导致系统超时
```
检测：RetrieveMultiple 调用中未设置 PageInfo 或 TopCount

修复建议：
```csharp
query.PageInfo = new PagingInfo { PageNumber = 1, Count = 500 };
```

#### 2. Plugin 内循环调用 OrganizationService（N+1 问题）
```csharp
// 危险模式：
foreach (var item in items)
{
    var record = service.Retrieve("contact", item.Id, cols); // N次调用！
}
```
检测：循环体内包含 service.Retrieve / service.RetrieveMultiple / service.Execute

修复建议：使用批量 RetrieveMultiple + In 条件一次查询。

#### 3. 同步 Plugin 执行耗时操作
检测：Plugin 中包含：
- Thread.Sleep / Task.Delay
- HttpClient / WebRequest（外部 HTTP 调用）
- 文件 IO 操作（File.ReadAllText 等）
- 大量数据计算循环（> 1000次迭代）

修复建议：将耗时操作移入异步 Plugin 或 Custom API。

#### 4. 在 Pre-Validation / Pre-Operation 阶段执行写操作
检测：Stage=10 或 Stage=20 的 Plugin 中包含 service.Create / Update / Delete

风险说明：Pre 阶段写操作不在同一事务内，失败时无法回滚，导致数据不一致。

#### 5. 无过滤属性（FilteringAttributes 为空）
检测：Update 消息的 Plugin 步骤未配置 FilteringAttributes

风险说明：任何字段更新都会触发该 Plugin，极大增加无效执行次数。

修复建议：配置 FilteringAttributes，仅监听关心的字段变更。

---

### P2 级别（重要风险）

#### 6. 未正确处理 InvalidPluginExecutionException 以外的异常
```csharp
// 错误模式：
catch (Exception ex)
{
    throw new Exception(ex.Message); // 不规范
}
```
修复建议：
```csharp
catch (Exception ex)
{
    throw new InvalidPluginExecutionException($"Plugin 执行失败: {ex.Message}", ex);
}
```

#### 7. Plugin 内直接执行原生 SQL
检测：包含 SqlConnection / SqlCommand / SqlDataReader

风险说明：直接 SQL 绕过 D365 安全模型，且在 CRM 事务外执行，数据一致性无保障。

#### 8. 使用 IOrganizationService 而非 IOrganizationServiceFactory（多线程场景）
检测：Plugin 中使用 Task.Run / Thread / Parallel 但使用同一 service 实例

修复建议：每个线程需要独立的 IOrganizationService 实例。

#### 9. 在 Plugin 中硬编码 GUID 或 URL
检测：代码中出现 new Guid("xxxxxxxx-...") 或 http:// / https:// 硬编码

修复建议：使用 Plugin 不安全配置（UnsecureConfig）或自定义实体存储配置。

#### 10. 未检查 Target 实体属性是否存在
```csharp
// 危险模式：
var name = target["name"].ToString(); // 如果 name 未包含在 Plugin 请求中会崩溃
```
修复建议：
```csharp
var name = target.Contains("name") ? target["name"].ToString() : string.Empty;
```

---

### P3 级别（代码质量问题）

#### 11. Plugin 类未实现 IPlugin 接口的标准结构
#### 12. 缺少 using 语句导致对象未释放（OrganizationServiceContext 等）
#### 13. 日志过于详细（PluginTraceLog 写入大量数据）
#### 14. 字符串拼接构建 FetchXML（应使用 QueryExpression 或 FetchExpression）
#### 15. Plugin 步骤描述为空（影响运维可读性）

---

## 注册配置检查

### 执行阶段风险矩阵

| 阶段 | 模式 | 消息 | 风险评估 |
|------|------|------|----------|
| Pre-Validation (10) | 同步 | 任意 | 注意外部调用 |
| Pre-Operation (20) | 同步 | Create/Update/Delete | 谨慎写操作 |
| Post-Operation (40) | 同步 | Update（无过滤） | ⚠️ 高风险 |
| Post-Operation (40) | 异步 | 任意 | 推荐用于耗时操作 |

### 高风险注册组合（必须告警）

- Post-Operation + 同步 + Update + 无过滤属性 → P1
- Post-Operation + 同步 + RetrieveMultiple + 高频实体 → P1
- Pre-Operation + 同步 + 外部 HTTP 调用 → P1
- 任意阶段 + 同步 + Thread.Sleep → P1

---

## 输出结构

```
## Plugin 代码扫描报告

### 扫描概况
扫描文件：XX 个 .cs 文件
Plugin 类：XX 个
步骤注册：XX 个步骤
发现问题：P1×X / P2×X / P3×X

### 健康评分
| 维度 | 评分 |
|------|------|
| 性能安全 | XX/30 |
| 异常处理 | XX/20 |
| 注册配置 | XX/25 |
| 代码规范 | XX/25 |
| 综合评分 | XX/100 |

### 问题清单

[P1] ContactPlugin.cs:L87 - RetrieveMultiple 无分页
  现象：循环查询 contact 记录未设置 PageInfo
  风险：结果集大时导致同步 Plugin 超时，影响用户操作
  修复：添加分页 PageInfo { PageNumber=1, Count=500 }

[P1] OrderPlugin.cs:步骤注册 - Update 消息无过滤属性
  现象：监听 salesorder 所有字段更新
  风险：任意字段修改均触发 Plugin，高频业务场景性能损耗严重
  修复：配置 FilteringAttributes = "statecode,statuscode,totalamount"

[P2] EmailPlugin.cs:L124 - 硬编码环境 URL
  现象：代码中包含 https://prod-crm.company.com
  风险：迁移/测试环境时代码失效
  修复：改用 IOrganizationService 的 OrganizationDetail 或配置实体

### 优化建议
P1：...
P2：...
P3：...

### 注册配置审查
...
```
