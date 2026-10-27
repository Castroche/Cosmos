# COSMOS Trading Intelligence v2 产品使用说明

## 1. 运行边界

实时交易系统只使用 Trading Intelligence v2。旧定时 Agent、自动策略扫描、旧 WAIT 调度和 `screen → backtest → execute` 链已经删除。Strategy DSL、Campaign、Walk-forward、PBO/CPCV、Shadow 和 Champion 只存在于离线 Research。

当前允许的实时模式：

- `SHADOW`：只记录决策和反事实，不提交交易所订单。
- `OKX_DEMO`：使用 OKX Demo API 自动交易。
- `LIVE_AUTHORIZED`：仅在有效 HumanLiveGrant 下允许；当前应保持关闭，直到 Demo 全自动验收完成并接入独立 Live 凭证。

## 2. 启动与停止

```powershell
npm run service:start:build
npm run service:status
npm run service:stop
```

打开 <http://127.0.0.1:8000>。启动服务只启动数据、事件和持仓对账后台，不等于授权实盘。

## 3. 设置 OKX Demo

1. 在“设置”选择 OKX、Sandbox、USDT 永续。
2. 只填写 OKX Demo API Key、Secret 和 Passphrase；禁止提现权限。
3. 确认 OKX 账户模式支持永续合约，并使用系统要求的单向持仓模式。
4. 保存后运行连接测试，公共行情、私有账户、市场类型和权限均须通过。
5. 完成原生 TP/SL commissioning。
6. 完成 v2 确定性仓位管理 commissioning。

任一步失败时，自动化保持 fail closed。

## 4. 交易工作台

工作台按以下顺序展示：

```text
市场状态 → Thesis → Plan → 等待条件 → 触发 → 风险分配 → 持仓 → 结果
```

### 实时图表

- BTC、ETH、SOL 切换时，每个品种保持独立公共 WebSocket 行情。
- 切到其他页面再返回时，图表连接继续存活，不应重新建立整套页面状态。
- WebSocket 临时中断时自动重连，并由 REST K 线补洞。
- 行情过期、价差异常或关键特征不可用时，数据质量降级并阻止新增风险，不会填零或使用演示行情冒充真实数据。

### MarketStateV2

检查 `data_as_of`、状态哈希、Regime，以及结构、趋势、动量、波动、成交量、微观结构、衍生品、跨市场和官方事件各组的 `healthy/degraded/unavailable` 状态。

### Thesis、Plan 与 WakeCondition

- `LONG/SHORT` 必须有完整 TradePlan。
- `WAIT` 必须引用有效 Thesis 和精确条件。
- `NO_TRADE` 必须有机器可执行 WakeCondition。
- 普通行情只更新状态；只有条件命中、到期/失效、交易终结、严格重大事件或有界重试才可唤醒 LLM。
- 同一 WakeCondition 通过幂等键最多生效一次。

### Quant

5m 收盘后更新 QuantPrediction，TTL 为 10 分钟。Demo 的 LONG/SHORT 必须有未过期、方向一致且成本后期望为正的 Quant edge；冲突时确定性降级为 WAIT/NO_TRADE。

### 持仓、订单与收益

左侧“持仓与订单”页面同时显示当前持仓、近期订单和已平仓交易。历史收益只统计已有平仓成交证据的 v2 仓位，使用交易所成交价与手续费计算费用后净收益；挂单和未平仓头寸不会提前计入。页面支持按 BTC/ETH/SOL 和 7 天、30 天、90 天、1 年或全部时间筛选，并展示累计收益曲线、胜率、Profit Factor、最大回撤和费用。

## 5. 启动全自动 Demo

1. 确认自动化状态不是 `DEGRADED`，failure budget 未耗尽。
2. 运行 Demo commissioning。
3. 启动 `OKX_DEMO` 自动化。
4. 观察三品种 MarketState 和 5m Quant 更新时间。
5. 查看 Thesis、Plan 和 WakeCondition 是否持久化。
6. 触发后检查 RiskAllocation、订单原生保护、部分成交和 Position 状态。
7. 平仓后检查 DecisionEpisode 和 attribution 是否完整。

普通市场波动和普通持仓管理应产生零次 LLM 调用。

## 6. 官方事件源

系统只向模型提供规范化事件类型、时间、来源和摘要：

- CPI/NFP：BLS 官方发布日历。
- FOMC：Federal Reserve 官方日历。
- 监管：SEC 官方新闻。
- 交易所状态：OKX system/status。
- ETF Flow：没有可靠官方免费实时源时固定为 `unavailable`。

默认禁止新增风险窗口：CPI/NFP 前 15 分钟至后 5 分钟；FOMC 前 30 分钟至后 15 分钟；OKX 故障或维护期间立即停止新增风险。

## 7. 离线 Research

“研究 Agent”、策略编辑器和策略库只生成、验证和保存研究候选。它们不能激活实时计划、不能调用交易执行入口，也不会向 Trading Intelligence 自动交接 Champion。

离线能力包括不可变数据集、Walk-forward、成本压力、PBO/CPCV、密封盲集、Quant 训练、Shadow 反事实和负结果证据。

## 8. 风险与故障处理

以下情况必须阻止新增风险：

- 行情陈旧或关键数据不可用。
- Quant 缺失、过期、方向冲突或成本后期望非正。
- 订单状态不确定、保护单缺失或对账连续失败。
- 日内回撤、组合风险、相关性、流动性或滑点超过上限。
- OKX 维护或严格官方事件风险窗口。
- Demo commissioning 未通过。
- Live 模式无有效 HumanLiveGrant。

HALT 不撤销已有保护单。先核对交易所真实订单、持仓、Algo 保护和本地账本，再人工恢复。

## 9. 实盘授权

Demo 全自动验收完成前不要接入 Live API。之后应使用独立 Live 凭证，并通过 HumanLiveGrant 的 preview、双重确认、签发和到期/撤销流程。Grant 必须绑定交易所、品种、风险上限、最大杠杆、日内回撤、最大持仓数、配置指纹和到期时间。

无有效 Grant 时，系统必须路由至 Demo 或 Shadow，绝不能触达 Live API。

## 10. 常用检查

- `GET /api/health`：进程健康。
- `GET /api/ready`：运行依赖就绪。
- `GET /api/trading-intelligence/overview`：v2 总览。
- `GET /api/trading-intelligence/official-events`：官方事件与风险窗口。
- `GET /api/trading-intelligence/quant-models`：Quant 模型健康。
- `GET /api/trading-intelligence/automation`：自动化状态。
- `GET /api/trading-intelligence/episodes`：决策证据链。
- `GET /api/trading-intelligence/attribution`：结果归因。
- `GET /api/trading-intelligence/trade-history`：已平仓仓位、费用后收益和累计曲线。
- `GET /api/trading-intelligence/migration-summary`：旧证据只读计数。

旧 `/api/agent/*`、旧 Live 授权和旧策略 approve/activate/execute 路径没有兼容层，访问结果为 404。
