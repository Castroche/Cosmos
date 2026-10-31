# 2026-09-17 运行问题修复与 OKX Demo 启用

> 历史报告：密钥配对／会话认证描述已被本机免登录、Host／Origin 校验取代。当前行为与应用分层、学习对照机制以 [ARCHITECTURE](ARCHITECTURE.md) 和 [升级说明](LEARNING_ARCHITECTURE_UPGRADE.md) 为准。

最终检查时间：2026-09-17 14:09:41（北京时间）。原账户服务健康，自动化 `enabled=true / RUNNING / OKX_DEMO`，覆盖 BTC、ETH、SOL；连续失败 0，风险未暂停，当前无持仓。更新后的 BTC 决策已正常完成并选择 NO_TRADE。服务继续运行在 `http://127.0.0.1:8000`。

## 修改

1. **监控内存增长**：未匹配或被认证提前拦截的路径统一归入固定标签，未知 HTTP 方法归入 OTHER；计数器、仪表各限制最多 10,000 个序列。匿名随机路径不再创建独立指标。
2. **本机路径信息暴露**：匿名会话接口只返回 `operator-access.key` 文件名；完整位置仅由本机启动器输出。登录页已同步说明。Swagger、ReDoc、OpenAPI 也纳入认证。
3. **WebSocket 代理断线**：新增统一连接模块，为 websockets 15/16 的代理响应 Future 增加完成检查和幂等 EOF 处理，避免重复回调解析已结束的响应；处理连接建立前失败所遗留的 Future 异常。连接、关闭有明确超时，既有重连保留。此兼容处理不绕过 TLS、代理认证或目标站点检查。
4. **Windows 初始化**：延续上轮 SID 权限设置修复；密钥写入使用权限受限的临时文件，完整落盘后原子发布。
5. **自动化启动竞态**：验收前记录风险和配置代次，真正开启前重新检查；等待验收期间的停止、风控暂停或配置变化会取消旧启动请求。恢复失败会关闭对应启动，后续状态写入使用乐观并发检查，并返回实际保存状态。
6. **启动实例识别**：进程状态文件记录数据目录，防止误复用不同目录的试运行服务；新增 `-StartPaused` 维护启动方式。暂停配置在后台自动化运行前保存，仍保留对账管理。
7. **新发现的 WAIT 合约冲突**：决策引擎与提示允许没有 Thesis 的 WAIT，但 API 将 `thesis_id=null` 判为无效，导致合法等待决策被记为 DECISION_ERROR。现只在显式提供 ID 时要求它引用有效 Thesis，仍拒绝不存在的 ID，没有降低下单风控。
8. **新发现的错误计数遗漏**：LLM 返回结构化 decision_error 时以前会走成功回调，将运行面板错误数清零；现在记为 DecisionContractError，更新错误计数和退避，正常的 NO_TRADE、等待和风险拒绝仍按对应业务路径处理。

代理兼容处理参考并对照了[上游连接实现](https://github.com/python-websockets/websockets/blob/main/src/websockets/asyncio/client.py)及当前本机 16.1.1 源码；实现留在应用模块中，没有修改 site-packages。未来升级依赖时应复核是否可以移除兼容处理。

## 账户与检查范围

- 已停止独立试运行实例，切回 `F:\Cosmos\data` 的原账户数据库。
- 原配置为 OKX sandbox / swap，实盘开关为 false；历史持仓记录 12 条 CLOSED、1 条 FLAT。
- 启动前验收确认无未归属持仓或挂单，账户为双向持仓、全仓、3 倍杠杆，BTC/ETH/SOL 原生保护能力可用。
- 本机代理可以访问 OKX；直接连接超时。原账户的显式代理配置恢复了 REST 和实时行情，未修改代理程序。
- 前端生产构建、修改后端文件的 AST/Ruff F/B、PowerShell 语法解析及差异空白检查通过。没有运行完整测试套件。
- 真实浏览器登录成功，无 JavaScript pageerror，未出现 HTTP 4xx/5xx，登录页不再出现完整路径，密钥没有显示在工作台。
- 运行复查确认匿名随机路径不进入指标标签，文档接口匿名返回 401。
- 原账户行情返回 300 根 K 线，质量 healthy，来源 okx:websocket；三个品种 MarketState 均可用于新增风险。
- 更新后的代理模块观察期间没有 Traceback、Future exception 或 EOFError；这不能替代长期网络故障验证。

## 证据

运行摘要保存在 `output/security-trial-20260917/`：

- `demo-commission.json`：账户验收摘要，已省略权益金额。
- `demo-start-final.json`：最终启用请求结果。
- `demo-browser.json`、`demo-workspace.png`：原账户工作台浏览器观察。
- `demo-runtime.json`：运行状态快照；较早的快照包含触发本次 WAIT 修复的两个历史错误，应以最终启用及最新决策记录为准。
- `demo-current-status.json`、`demo-final-observation.json`：最终运行状态与更新后的决策、日志检查。

自动化只开放 OKX Demo，BTC、ETH、SOL 由既有策略和风控决定是否入场。开启自动化不等于必须成交；无机会时继续等待。
