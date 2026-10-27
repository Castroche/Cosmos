# COSMOS 架构、流畅度、稳定性与安全静态审查

日期：2026-09-16。范围：当前工作区，包含未提交改动。方法：只读源码、配置和调用链分析；未运行测试、构建、服务、交易请求或漏洞利用。除本报告外未修改文件。

后续状态：2026-09-17 已针对下述发现修改源码，具体处理方式与未运行验证的范围见 [修复说明](F:/Cosmos/docs/STATIC_REMEDIATION.md)。下文保留原始审查证据，不应视为修复后重新复现的结果。

## 结论

当前系统有良好的领域建模基础：交易意图与确定性风控分离、交易状态版本化、订单幂等、保护单与对账、历史决策归因。主要短板集中在这些模块的连接处：执行授权没有在最终提交边界重新确认，Live 执行与后台管理不对称，加仓风控上下文缺失，运行客户端和配置生命周期不统一。

界面有可以直接从代码确认的失败路径：首次配置请求失败后无限加载；概览轮询在持续慢响应下饥饿。长期流畅度则受到全历史订单读取、同步数据库访问和逐 tick 全量图表计算的影响。没有做性能测量，因此不报告 FPS、延迟、吞吐量或实际发生频率。

以下“确认”指静态调用链可以推出问题，不表示已在账户或浏览器复现。

## 高优先级问题

### 01 · High · HALT 成功后，在途请求仍可能提交新订单

- **类型/确定性：** 执行授权时序缺陷；确认存在竞态窗口。
- **位置：** [api.py:12409](F:/Cosmos/backend/cosmos_quant/api.py:12409)、[api.py:12518](F:/Cosmos/backend/cosmos_quant/api.py:12518)、[api.py:11287](F:/Cosmos/backend/cosmos_quant/api.py:11287)、[v2_execution.py:429](F:/Cosmos/backend/cosmos_quant/v2_execution.py:429)。
- **证据：** 执行函数开头读取 `risk_control.halted`，随后等待读取持仓、报价、保护能力等。HALT 路由立即写状态并返回；最终 `await self.exchange.place_protected_order(...)` 前没有再读取 HALT，也没有与 HALT 共享提交协议。
- **触发：** 请求 A 通过初始检查后等待交易所；请求 B HALT 成功；A 恢复并提交订单。
- **影响：** 用户收到停机成功后，仍可能出现此前尚未发出的新开仓请求。此处不包括 HALT 前已发给交易所的订单自然成交。
- **修复方向：** 为执行命令建立风险版本/停止代次，最终提交前重读并校验；明确 HALT 与提交的线性化边界和在途订单语义，阻止尚未提交的旧代次命令继续执行。Live grant 撤销也应采用同样的最终授权校验，不能仅信任先前加载的对象。
- **暂缓措施：** HALT 后仍需观察在途订单与撤单对账结果，不能把 HTTP 成功等同于在途执行已经清空。

### 02 · High · Live 能提交订单，但入场订单生命周期仅处理 Demo

- **类型/确定性：** 执行与恢复架构断层；确认。
- **位置：** [api.py:12498](F:/Cosmos/backend/cosmos_quant/api.py:12498)、[api.py:2001](F:/Cosmos/backend/cosmos_quant/api.py:2001)、[api.py:2049](F:/Cosmos/backend/cosmos_quant/api.py:2049)、[entry_runtime.py:66](F:/Cosmos/backend/cosmos_quant/entry_runtime.py:66)、[entry_runtime.py:82](F:/Cosmos/backend/cosmos_quant/entry_runtime.py:82)。
- **证据：** 执行路由会为 `LIVE_AUTHORIZED` 构造 `okx:live` 适配器。后台持仓循环却固定使用 `build_v2_demo_adapter()`；`EntryOrderService` 默认 `mode="OKX_DEMO"`，明确跳过其他模式的 entry。即时执行把订单保存为 OPEN，并把成交记账交给该生命周期。
- **触发：** Live 凭证、HumanLiveGrant、Quant 等前置条件满足后，由 Live 执行路由成功下单。
- **影响：** Live 入场记录无法通过现有后台链完成成交记账、生成/更新对应 v2 持仓、终态通知与后续确定性管理。原生保护单仍可能存在，不能据此认定交易完全无保护；但应用管理闭环缺失。
- **修复方向：** 在 Live 管理闭环实现前，执行边界显式禁用 Live；后续按账户和模式分区运行完整 entry/position reconciler，并将账户与模式纳入持仓、风险和订单关联键。
- **边界：** 未读取真实凭证，未验证当前账户是否能够通过 Live 前置条件。

### 03 · High · 合法同向加仓在 API 层必然缺少风控上下文

- **类型/确定性：** 功能 BUG；确认。
- **位置：** [api.py:12443](F:/Cosmos/backend/cosmos_quant/api.py:12443)、[api.py:12552](F:/Cosmos/backend/cosmos_quant/api.py:12552)、[trading_intelligence_engine.py:119](F:/Cosmos/backend/cosmos_quant/trading_intelligence_engine.py:119)。
- **证据：** API 已要求加仓匹配 `position_id` 和 `position_version`，但创建 `TradingIntelligenceRiskContext(...)` 时没有传 `existing_position`。该字段默认 None。风控随后执行 `if plan.position_id is not None and existing is None: raise ValueError(...)`。
- **影响：** 身份与版本均正确的加仓仍会被拒绝，可能被上层当作执行失败进入降级/重试，而不是完成加仓。
- **修复方向：** 从已核验的目标持仓及组合风险快照构造 `ExistingPositionRisk`，包含当前数量、标记价格、止损风险、版本，并在统一执行边界再次确认版本。

### 04 · High（有端口访问前提）· 管理和交易 API 没有调用者认证

- **规则：** FASTAPI-AUTH-001 / FASTAPI-AUTHZ-001。
- **类型/确定性：** 应用层信任边界缺失；确认。
- **位置：** [api.py:2392](F:/Cosmos/backend/cosmos_quant/api.py:2392)、[api.py:4840](F:/Cosmos/backend/cosmos_quant/api.py:4840)、[api.py:11676](F:/Cosmos/backend/cosmos_quant/api.py:11676)、[api.py:12959](F:/Cosmos/backend/cosmos_quant/api.py:12959)。
- **证据：** FastAPI 配置了 TrustedHost 和 CORS，但没有全局/路由鉴权依赖。设置修改、行情状态注入、自动化控制、授权签发接口不验证调用者身份。两次风险确认是请求布尔字段，不是身份认证。
- **影响：** 能访问端口的本地进程可调用这些管理能力；如果后续经代理或隧道对外暴露，访问者也可能获得同样能力。具体交易仍受业务前置条件约束，不意味着可以无条件直接开实盘。
- **修复方向：** 建立本地配对会话或调用凭证，统一保护管理与执行路由；行情采集入口与 UI 控制入口分离，人工实盘授权绑定可核验的会话。浏览器写操作校验 Origin，WebSocket 握手也应限制来源与连接数。
- **现有缓解/边界：** 默认监听 127.0.0.1，TrustedHost 和受限 CORS 已有价值。不能把此项描述为默认可被任意互联网主机直接利用；也不能把 CORS 当作本地进程认证。浏览器跨站到本机端口还受浏览器自身策略限制，本次未验证。

### 05 · High（需状态写入能力）· 未来时间戳可以长期污染“最新行情”

- **类型/确定性：** 输入完整性 BUG，可与 04 组合；确认。
- **位置：** [api.py:11676](F:/Cosmos/backend/cosmos_quant/api.py:11676)、[trading_intelligence.py:205](F:/Cosmos/backend/cosmos_quant/trading_intelligence.py:205)、[trading_intelligence_store.py:73](F:/Cosmos/backend/cosmos_quant/trading_intelligence_store.py:73)、[api.py:12476](F:/Cosmos/backend/cosmos_quant/api.py:12476)。
- **证据：** 写入接口接受调用者提供的完整 MarketState，包括 `data_as_of` 与特征健康标记；模型没有未来时间偏差限制。最新记录按 `data_as_of DESC` 选取，执行时只检查 `now - data_as_of > 10 minutes`，未来时间差为负数，不会被拒绝。
- **影响：** 错误采集器或有写入权限的调用者可让未来记录长期压过正常新行情，使后续读取持续使用错误上下文。稳定哈希仅保证内容标识，不证明来源可信。
- **修复方向：** 统一校验时区、合理未来偏差、最大历史年龄和生产者身份；只允许服务端采集器写入生产状态，回放数据使用独立命名空间；执行入口同时拒绝过旧和过度超前数据。

### 06 · High（运行中更换凭证时）· 下单客户端与持仓管理客户端可能属于不同配置代次

- **类型/确定性：** 资源生命周期 BUG；确认客户端没有随配置更新机制，影响取决于修改内容。
- **位置：** [api.py:2031](F:/Cosmos/backend/cosmos_quant/api.py:2031)、[api.py:2065](F:/Cosmos/backend/cosmos_quant/api.py:2065)、[api.py:4840](F:/Cosmos/backend/cosmos_quant/api.py:4840)、[api.py:12502](F:/Cosmos/backend/cosmos_quant/api.py:12502)。
- **证据：** position loop 在 `adapter is None` 时构造 Demo 客户端，正常运行中不比对配置或凭证代次。设置保存仅强制更新 `runtime`，新下单则每次读取当前凭证另建客户端。更新设置对旧持仓的禁止条件仅在账户上下文变化时生效，单独换凭证不等价于换上下文；且 v2 Demo 客户端独立于通用 runtime。
- **影响：** 原客户端继续管理旧账户/旧代理配置，新下单请求使用新凭证；可导致漏管新账户或错误的未知持仓/订单判断。仅修改非连接相关参数不一定触发账户错位。
- **修复方向：** 建立账户级连接管理器，配置与凭证变更生成代次；旧代次敞口未收口前不得替换账户身份。所有下单、对账和持仓管理使用同一账户作用域，连接切换经过暂停和重新对账。

## 中优先级 BUG 与性能退化路径

### 07 · Medium · 首次读取设置失败后，界面永久停留在加载页

- **类型/确定性：** 用户可见功能 BUG；确认。
- **位置：** [App.tsx:265](F:/Cosmos/apps/web/src/App.tsx:265)、[App.tsx:448](F:/Cosmos/apps/web/src/App.tsx:448)。
- **证据：** 初始 `getSettings().then(...)` 没有 catch、重试或错误状态；`settingsView` 为空时只渲染“正在读取本机配置”。effect 仅在挂载时运行。
- **触发/影响：** 首次请求超时或服务尚未就绪，后端随后恢复也不会自动恢复 UI，只能重新加载页面。
- **修复方向：** 明确 loading/error/ready 状态，展示失败原因、重试入口，并采用有上限的退避重试。

### 08 · Medium · 10 秒轮询使持续慢响应被全部丢弃

- **类型/确定性：** 前端刷新饥饿 BUG；确认条件路径。
- **位置：** [TradingIntelligenceWorkspace.tsx:126](F:/Cosmos/apps/web/src/TradingIntelligenceWorkspace.tsx:126)、[TradingIntelligenceWorkspace.tsx:142](F:/Cosmos/apps/web/src/TradingIntelligenceWorkspace.tsx:142)、[api.ts:50](F:/Cosmos/apps/web/src/api.ts:50)。
- **证据：** 每发起请求就递增 `overviewRequest`，只有最后发起的请求允许更新状态；setInterval 每 10 秒继续发送，而通用超时为 15 秒。
- **触发：** 例如每次耗时 12 秒：第一个返回前第二个已递增版本，第二个返回前第三个又已递增版本，如此循环。
- **影响：** 请求可能一直成功但 UI 一直不刷新，错误也可能被同一版本检查吞掉，同时累积重叠请求。
- **修复方向：** 单请求在途，完成后再安排下一次轮询；主动刷新使用取消/合并机制。显示最近成功更新时间，区分行情连接状态与账户/策略概览新鲜度。

### 09 · Medium · 历史订单读取无界，且热路径存在同步数据库扫描

- **类型/确定性：** 确认的随数据量增长成本；实际卡顿程度未测量。
- **位置：** [trading_intelligence_store.py:614](F:/Cosmos/backend/cosmos_quant/trading_intelligence_store.py:614)、[entry_runtime.py:78](F:/Cosmos/backend/cosmos_quant/entry_runtime.py:78)、[api.py:11638](F:/Cosmos/backend/cosmos_quant/api.py:11638)、[trading_intelligence_automation.py:180](F:/Cosmos/backend/cosmos_quant/trading_intelligence_automation.py:180)。
- **证据：** `list_entry_orders()` 没有 LIMIT。入场对账先查询并反序列化所有历史订单，再在 Python 中跳过已通知终态；概览同样返回全部入场订单。决策队列循环直接同步读取最多 5000 个事件再扫描是否已消费。
- **影响：** 运行越久，每轮查询、JSON/Pydantic 解析、传输和渲染成本越高；同步工作会占用共享 asyncio 事件循环，影响行情推送和交易任务及时性。RLock 也让同进程数据库操作相互等待，WAL 不能消除应用层串行化。
- **修复方向：** SQL 直接筛出活跃订单及尚未通知的终态订单；概览仅给活跃数据和有界最近记录，历史分页；队列使用持久化消费状态/游标和索引，避免反复重扫历史；同步 I/O 从事件循环移出。

### 10 · Medium · 图表逐更新全量计算，REST 回包可能覆盖较新的 WebSocket 状态

- **类型/确定性：** 前半为性能风险，后半为确认存在的时序窗口。
- **位置：** [ProfessionalChart.tsx:464](F:/Cosmos/apps/web/src/ProfessionalChart.tsx:464)、[TradingIntelligenceWorkspace.tsx:163](F:/Cosmos/apps/web/src/TradingIntelligenceWorkspace.tsx:163)、[TradingIntelligenceWorkspace.tsx:186](F:/Cosmos/apps/web/src/TradingIntelligenceWorkspace.tsx:186)。
- **证据：** candles 更新后执行完整 `series.setData`、构造 Map，并计算 EMA/SMA/VWAP/Bollinger/RSI/MACD；即使某指标未显示，传给更新函数的指标表达式仍会先计算。定时 REST 补数据直接 `setMarketWindow(result)`，没有与已经到达的 WebSocket 数据比较新鲜度或合并。
- **影响：** 高频更新会增加主线程负担；较慢 REST 快照在新 tick 之后返回，可暂时让最新价/当前 K 线回退。当前 300 根窗口有上限，不能仅凭代码宣称一定严重掉帧。
- **修复方向：** 普通 tick 用 series.update 更新末根，指标只算启用项并缓存增量；历史补洞与新行情按时间/序列合并；UI 更新按帧合并，拆分行情与订单面板的渲染边界。

## 架构与稳定性补充判断

1. **应先形成模块化单体，不宜直接加进程副本。** API、行情、交易、研究和后台任务共享全局实例及本地锁。简单增加 Uvicorn workers 会为每个进程启动后台循环，进程内锁无法保护跨进程交易编排。需要先落实唯一执行所有者/租约，再考虑横向扩展。
2. **通用 runtime 与 v2 独立客户端双轨运行是主要复杂度来源。** 它们有不同的创建、换配置、关闭与对账生命周期。应围绕账户作用域统一所有权，而不仅是拆小文件。
3. **RuntimeManager.initialize 没有初始化锁或原子换代。** [runtime.py:62](F:/Cosmos/backend/cosmos_quant/runtime.py:62)先检查再 `await self.close()`；配置变化/初始化失败恢复时，多请求可共同进入。并发初始化和请求仍引用旧客户端的行为属于需要重点处理的静态风险。本次未运行并发复现。
4. **关键 worker 缺少明确监督与健康证据。** lifespan 创建任务后主要在退出时等待；position loop 的部分读取和适配器构造在每轮 try 外。异常可能终止 worker，而 [api.py:3190](F:/Cosmos/backend/cosmos_quant/api.py:3190)的健康接口仍固定返回 `status: ok`。建议把工作心跳、最后成功对账时间和 task.done/exception 纳入 readiness，关键任务失败立即阻止新增风险。
5. **LLM 延迟与行情状态更新仍有耦合。** 每品种市场刷新会 await automation.on_market_state，后者可能持有品种锁等待 LLM。把行情状态落库、事件排队和耗时决策分离，可让慢模型不影响新状态采集与展示。
6. **保证金预留需明确口径。** 风险快照计算 pending_margin，但执行上下文只传 venue available_margin，没有显式使用该预留。交易所可用保证金可能已包含挂单冻结，不能直接重复扣减，也不能默认未知提交已冻结。应区分已被交易所确认冻结与仅本地预留；本次将其列为待核对项，不断言已发生超额分配。

## 建议处理顺序

1. 修复最终提交边界的 HALT/授权竞态；在完整 Live 管理链交付前关闭其执行入口。
2. 补齐同向加仓上下文；统一账户/凭证/适配器生命周期。
3. 增加管理 API 调用者认证，收紧生产行情写入和时间戳校验。
4. 修复启动失败页与慢轮询饥饿；给历史数据查询加范围和分页。
5. 再做图表增量更新、后台监督、数据采集与 LLM 解耦，以及 API/应用服务/存储模块拆分。

## 审查限制

未执行测试或构建，未连接交易所，未测量响应时间或帧率，未读取账户密钥，未核验在线部署的反向代理与浏览器本地网络策略，未做依赖 CVE 扫描。本报告不声称穷尽全部漏洞，也不把静态风险等同于已经发生的事故。
