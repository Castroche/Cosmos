# COSMOS Trading Intelligence v2 架构

## 生产热路径

```mermaid
flowchart LR
  MD[OKX REST / Public WebSocket] --> MS[MarketStateV2]
  OE[BLS / Fed / SEC / OKX status] --> MS
  MS --> EV[Event Engine]
  EV --> MA[MarketAnalyst]
  MA --> PT[PortfolioTrader]
  Q[Quant Intelligence v1] --> PT
  PF[Portfolio + Previous Thesis] --> PT
  PT --> TH[具体点位 Thesis / TradePlan]
  TH --> WT[立即提交 MARKET/LIMIT 意图]
  WT --> RE[Deterministic Risk Engine]
  RE --> EX[OKX protected execution]
  EX --> PM[Deterministic Position Manager]
  PM --> DL[Decision Ledger + Attribution]
  DL --> EV
```

普通行情只更新 MarketState。普通持仓变化只进入确定性 Position Manager。只有无有效 Thesis、Wake/Entry 条件命中、计划到期或失效、交易结束、Thesis 失效、严格重大官方事件和有界 LLM 重试可调用 LLM。

## 状态与持久化

- MarketState、Thesis、Plan、WakeCondition、Position、DecisionEpisode 均版本化并带稳定哈希。
- 所有转换使用 SQLite 事务、乐观版本和幂等事件键。
- WakeCondition 在执行动作前按 Plan/WaitRuntime 实例原子 claim，跨重启不能重复触发，后继实例可安全复用条件标签。
- `EXTERNAL_EVENT` 仅允许已注册生产者；NO_TRADE 必须至少保留一个行情派生条件和枚举硬阻断，最长静默 60 分钟（WAIT 为 120 分钟）。不可达、已经满足、路径不存在、价格尺度错误或超时过长的非交易等待由确定性 Logic Gate 删除/补充 Regime fallback/截断期限，不改写方向、入场、止损、止盈、仓位或杠杆。
- 无 Thesis 时，Regime、结构突破以及带成交量确认的 5m/15m 波动扩张/ADX/EMA 边沿变化可持久化唤醒一次；普通 tick、持续为真的状态、已有 Plan 或持仓不会触发该兜底。
- 每个品种只允许一个 Active Thesis，有效活跃时间最多四小时；NO_TRADE 会撤销旧 Thesis，拒绝决策使用持久化有界重试与冷却而非随行情反复调用 LLM。
- DecisionEpisode 串联 MarketState、Delta、Analyst、Quant、Trader、Validator、Plan、Trigger、RiskAllocation、Orders、Position 和 Outcome。
- Prompt 版本、模型、Token、延迟、输入输出哈希、data_as_of 和证据引用全部入账。

## 决策边界

MarketAnalyst 无交易工具，也不承担审核职责。PortfolioTrader 是唯一策略决策者，返回方向、MARKET/LIMIT 订单意图、参考价或限价、止损、止盈和有效期。新交易意图在确定性风控通过后立即提交，不再挂载入场 WakeCondition；需要未来确认时返回 WAIT，由条件唤醒重新决策。健康行情不强制交易，没有机会可以 NO_TRADE。结构化契约只允许一次格式修复，仍失败记为 `DECISION_ERROR`。TradePlan 不包含数量、风险比例、杠杆或交易所请求。

Risk Engine 在触发后根据权益、止损距离、波动、流动性、相关性、敞口、日内损益和人工授权生成 RiskAllocation。初版 LLM confidence 不放大风险。

## 执行与恢复

- 生产首发范围为 OKX BTC/ETH/SOL USDT 永续。
- OKX_DEMO 复用原生 TP/SL、幂等 client ID、部分成交、私有流和 REST 对账。
- Position Manager 确定性处理止损、分批止盈、Break-even、ATR trailing、最长持仓、退出请求、保护覆盖和重启恢复；EXIT 通过幂等 reduce-only 订单收口。
- 订单状态不确定、保护缺失或对账失败时停止新增风险；历史订单与持仓证据不会被删除。
- Live 入场对账与持仓管理尚未完成，当前执行边界无条件拒绝 LIVE_AUTHORIZED；授权预览给出阻断原因，历史授权不能绕过。
- HALT/停止自动化使旧决策代次失效；每个交易所子订单在提交边界重新检查。HALT 返回前等待已进入提交阶段的请求结束，已发出的订单仍需成交/撤单对账。
- 账户、凭证和连接设置更新与入场串行；自动化开启或仍有持仓/活跃入场时不允许切换。Demo 管理客户端在配置代次变化后重建。
- 管理 API 使用本机密钥配对会话或 Bearer 认证，浏览器写请求和 WebSocket 校验 Origin。行情原始写入 HTTP 接口关闭，生产者写入及执行检查均限制未来时间偏差。

## 官方事件

OfficialEventClient 独立采集 BLS CPI/NFP、Federal Reserve FOMC、SEC 新闻与 OKX 系统状态。每个源独立健康降级；一个源失败不能清空其他源结果。ETF Flow 在没有可靠官方免费实时源时固定为 `unavailable`。外部文本先规范化，再进入 MarketState 和 LLM 上下文。

## Quant Intelligence

Quant v1 只训练 BTC/ETH/SOL 5m，使用 2 ATR 目标、1 ATR 止损、24 根超时的 Triple Barrier。推理在 5m 收盘执行，TTL 10 分钟。模型只有在 purged OOF Brier 与 log-loss 均优于三分类均匀基线时才标记健康。Live 的 LONG/SHORT 必须使用健康模型且方向一致、成本后期望为正；Demo/Shadow 保留 Quant 作为反事实证据，不让 Quant 创建计划、订单或仓位。

## 离线 Research

Strategy DSL、Campaign、PBO、CPCV、Walk-forward、Shadow、Champion、密封盲集和模型训练均属于离线 Research。研究结果保存为候选、模型和证据，但没有自动交接实时执行的入口。

## 已移除的架构

旧 `AgentGoal + poll_seconds` 调度、`SCHEDULE_TRIGGERED`、旧 WAIT 终止语义、`screen_trade_candidates → backtest → execute`、旧 Agent WebSocket/API 和旧 Live 授权已从生产源码与路由表移除。历史 `trade_episodes` 仅通过只读模型用于证书与审计解析。

`/api/dashboard` 现在只是行情、K 线、账户、订单和全局风险状态的只读聚合，不再隐式生成 `StrategyPlan`、运行 Strategy DSL 或执行回测；`/api/risk` 也不会创建计划。策略生成与回测仅能从离线 Research 接口显式发起。

当前接口以 `/api/trading-intelligence/*` 为唯一实时交易命名空间。旧路径不注册，因此返回 404；迁移计数可通过 `/api/trading-intelligence/migration-summary` 查看。
