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

Plugin 扫描采用**两步流程**：先从元数据 CSV 看注册配置，再解压 DLL 做静态扫描。

### 模式 A：采集目录（推荐，服务器侧有定期采集）

目录约定：`<DATA_ROOT>/plugin_scan/YYYY-MM-DD/`

通过统一读取工具拉元数据 + 包清单：
```bash
python3 tools/data_reader.py --category plugin_scan --today
python3 tools/data_reader.py --category plugin_scan --date 2026-05-01
python3 tools/data_reader.py --category plugin_scan --list-dates
```

返回结构：
- `files[kind=csv]`：5 个元数据 CSV
- `files[kind=zip]`：`plugin_dlls.zip` 文件清单（**未解压**），含 `abs_path` 字段；Skill 自行解压后扫描

#### CSV 字段映射（对齐采集脚本 07）

**plugin_assemblies.csv**（程序集）
`PluginAssemblyId / AssemblyName / Culture / Version / PublicKeyToken / IsolationMode / SourceType / Path / ContentSizeBytes / CreatedOn / ModifiedOn / CreatedBy / ModifiedBy`
- `IsolationMode`：1=None, 2=Sandbox, 3=External
- `SourceType`：0=Database, 1=Disk, 2=Normal

**plugin_types.csv**（插件类）
`PluginTypeId / PluginAssemblyId / TypeName / FriendlyName / Name / Description / IsWorkflowActivity / WorkflowActivityGroupName / CreatedOn / ModifiedOn`

**plugin_steps.csv**（注册步骤）
`StepId / StepName / PluginTypeId / MessageName / PrimaryEntity / ExecMode / StageCode / Rank / SupportedDeployment / StateCode / FilteringAttributes / Configuration / AsyncAutoDelete / CreatedOn / ModifiedOn`
- `ExecMode`：0=Sync, 1=Async
- `StageCode`：10=PreValidation, 20=PreOperation, 40=PostOperation
- `StateCode`：0=Enabled, 1=Disabled

**plugin_images.csv**（Pre/Post 镜像）
`ImageId / StepId / ImageName / EntityAlias / ImageType / MessagePropertyName / ImageAttributes / CreatedOn / ModifiedOn`
- `ImageType`：0=Pre, 1=Post, 2=Both

**plugin_collect_info.csv**（采集元信息）
`CollectDate / Server / OrgDatabase / CrmBinDir / DumpedFromDB / ZipSizeMB`

#### DLL 包解压

Skill 获得 `plugin_dlls.zip` 的 `abs_path` 后，解压到临时目录：
```python
import tempfile, zipfile, os
tmp = tempfile.mkdtemp(prefix="plugin_scan_")
with zipfile.ZipFile(abs_path) as zf:
    zf.extractall(tmp)
# 对 tmp 下 *.dll 做反编译（ILSpy / dnSpy / Mono.Cecil）或字符串扫描
```

与 CSV 关联：DLL 文件名 ↔ `plugin_assemblies.AssemblyName` ↔ `plugin_types.TypeName` ↔ `plugin_steps`。

### 模式 B：用户上传源码
用户直接上传：
- `*.cs` 源文件
- `*.zip` 项目压缩包
- `*.csproj` 项目文件
无 CSV 元数据时，仅做静态代码扫描（模式 B 不能自动判断注册配置风险，需用户补充注册信息）。

### 模式 C：Plugin Registration Tool 导出
用户粘贴注册导出（步骤列表 / 过滤属性），补充模式 B。

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

## 两步流程推荐步骤（采集目录场景）

### Step 1：读元数据打画像
调用 `data_reader.py --category plugin_scan --today`，得到 5 个 CSV + 1 个 ZIP 清单。

### Step 2：注册配置静态分析（无需解压 DLL）
基于 `plugin_steps.csv` 直接识别风险：
- `FilteringAttributes` 为空 且 `MessageName='Update'` 且 `ExecMode=0`（同步）→ P1
- `StageCode=20` 且 `ExecMode=0` 且 `MessageName IN ('Create','Update','Delete')` → P2 预警（需结合代码确认是否有写操作）
- `AsyncAutoDelete=0` 且 `ExecMode=1`（异步） → P3（AsyncOperationBase 会膨胀）
- `StateCode=1`（禁用）→ 标注并汇总（可能是死代码）
- 同一 (MessageName, PrimaryEntity, StageCode, ExecMode) 下 `Rank` 重复 → 执行顺序不确定，P2
- 交叉 `plugin_types.csv` 搜集同一 `PluginTypeId` 注册了过多步骤（>5）：标注为万能类
- 交叉 `plugin_assemblies.csv`：`SourceType=0`（Database）且 `ContentSizeBytes > 5MB` → 重量级程序集

这一步**不需要 DLL**，可迅速出注册配置层的问题清单。

### Step 3：解压 DLL 做深度扫描
从 `files[kind=zip]` 的 `abs_path` 解压 `plugin_dlls.zip` 到临时目录，对每个 `*.dll`：
- 优先用 Mono.Cecil / ILSpy CLI / dnSpy CLI 反编译（如未安装，降级为 `strings` + 正则匹配）
- 按上文的 P1/P2/P3 规则扫描类型、方法、IL 代码
- 通过 `PluginTypeBase.TypeName` 定位 DLL 里的类 → `[namespace].[class]`，回写到对应步骤的风险条目

### Step 4：生成报告
注册层问题（Step 2）+ 代码层问题（Step 3）按 PluginType 汇总，输出统一报告。

> 注意：如果 `ZipSizeMB` 在 `plugin_collect_info.csv` 中 > 50MB，解压前要提示用户确认（避免放爆磁盘）。

---

## 输出结构

```
## Plugin 代码扫描报告

### 扫描概况
数据来源：采集目录（YYYY-MM-DD）/ 用户上传
程序集：XX 个（plugin_assemblies.csv）
Plugin 类：XX 个（plugin_types.csv）
注册步骤：XX 个（plugin_steps.csv）
DLL 深度扫描：XX 个类 / XX 个方法
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
