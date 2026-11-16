# COSMOS Quant

本项目采用 [MIT 许可证](LICENSE)。

COSMOS 是面向 OKX BTC/ETH/SOL USDT 永续的事件驱动交易智能系统。生产热路径只有 Trading Intelligence v2：

```text
官方/市场事件 → MarketStateV2 → MarketAnalyst → PortfolioTrader
              → MARKET/LIMIT TradePlan → 确定性风控后立即提交
              → 确定性 RiskAllocation → OKX Demo 保护单执行
              → 确定性 Position Manager → DecisionEpisode
```

旧定时 Agent、`screen → backtest → execute`、自动策略扫描、旧 WAIT 调度及旧 Live 授权接口已经从源码和路由表移除，不提供兼容层。访问旧路径会得到 HTTP 404；历史 goal/run/episode/order/position 仅作为只读证据保留。

## 当前能力

最新应用分层、可验证学习与影子对照见 [升级说明](docs/LEARNING_ARCHITECTURE_UPGRADE.md)。
工作台与多品种能力见 [此前升级说明](docs/TERMINAL_LEARNING_UPGRADE.md)。

- OKX BTC/ETH/SOL 永续 1m、5m、15m、1h、4h、1d 统一 MarketStateV2。
- 公共 WebSocket 实时报价、多品种持久连接、K 线 REST 补洞、断线重连与数据质量标记。
- BLS CPI/NFP、Federal Reserve FOMC、SEC 新闻、OKX 系统状态官方事件采集；ETF Flow 明确为 `unavailable`。
- MarketAnalyst 提供行情上下文，PortfolioTrader 是唯一策略决策者并直接给出入场区间、止损和止盈；没有 Critic 或策略审核热路径。LLM 不输出数量、风险比例、杠杆或交易所请求。
- Thesis、Plan、WakeCondition、Position 版本化状态机与幂等事件键。
- Quant Intelligence v1：BTC/ETH/SOL 5m、ATR Triple Barrier、带成本期望与 10 分钟 TTL。
- SHADOW 与 OKX_DEMO 全自动事件驱动运行；普通行情和确定性持仓管理不会调用 LLM。
- OKX 原生 TP/SL、幂等订单、部分成交、私有流、REST 对账、重启恢复与故障停机。
- HumanLiveGrant 接口保留；因 Live 成交对账与持仓管理尚未形成闭环，当前禁止签发新授权及提交实盘订单，即使已有凭证或历史授权也不能绕过。
- DecisionEpisode 保存从 MarketState 到 Outcome 的完整决策证据与归因。
- Strategy DSL、Campaign、PBO、CPCV、Walk-forward、Shadow、Champion 仅用于离线 Research，不是实时开仓前置链。
- TradingView Lightweight Charts 实时 K 线、成交量、指标、绘图与多周期切换。

## 安装

需要 Python 3.12+ 和 Node.js 20+。

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm ci
```

## 启动

```powershell
npm run service:start:build
npm run service:status
```

打开 <http://127.0.0.1:8000>。Windows 也可使用根目录的 `COSMOS-一键启动.cmd`。停止服务：

```powershell
npm run service:stop
```

本机网页打开后直接进入工作台，无需访问密钥或登录；服务重启后也无需重新连接。
服务仅监听 `127.0.0.1`，浏览器 API 和行情连接保留来源校验。

维护时可使用 `powershell -File scripts/cosmos-service.ps1 start -StartPaused` 启动并暂停自动化，
再从工作台验收、开启 OKX Demo。启动器记录数据目录，避免误把独立试运行实例当作原账户实例。
本机脚本可直接访问管理 API，无需 `Authorization` 请求头。
`GET /api/health` 是存活探针，`GET /api/readiness` 返回关键后台任务状态。

生产行情只允许服务端采集器写入；原始 `POST /api/trading-intelligence/market-state`
接口现返回 403。需要更新行情时使用 `/market-state/{symbol}/refresh`。
历史静态修复记录见 [修复说明](docs/STATIC_REMEDIATION.md)，当前认证方式及验证边界以上述升级说明为准。

## 模拟盘启用顺序

1. 在设置中选择 OKX、Sandbox、USDT 永续，配置 Demo API Key/Secret/Passphrase。
2. 通过公共行情、私有账户与账户模式连接测试。
3. 完成 OKX Demo 原生保护单 commissioning。
4. 完成 v2 确定性仓位管理 commissioning。
5. 在交易工作台启动 `OKX_DEMO` 自动化。
6. 检查 MarketState、Quant、Thesis、Plan、WakeCondition、RiskAllocation、Position 和 DecisionEpisode。
7. 演练重启、断网、部分成交、撤单、保护单异常和全局 HALT。

启动服务不会启用实盘；当前 Live 执行入口始终硬拒绝，需先实现完整 Live 管理链才可重新开放。

## 主要 API

- `GET /api/trading-intelligence/overview`
- `GET /api/trading-intelligence/official-events`
- `POST /api/trading-intelligence/official-events/refresh`
- `GET /api/trading-intelligence/market-state/{symbol}`
- `GET /api/trading-intelligence/quant-models`
- `GET /api/trading-intelligence/episodes`
- `GET /api/trading-intelligence/events`
- `GET /api/trading-intelligence/attribution`
- `GET /api/trading-intelligence/trade-history`
- `GET /api/trading-intelligence/automation`
- `POST /api/trading-intelligence/automation/commission`
- `POST /api/trading-intelligence/automation/start`
- `POST /api/trading-intelligence/automation/stop`
- `POST /api/trading-intelligence/live-grants/preview`
- `POST /api/trading-intelligence/live-grants`
- `DELETE /api/trading-intelligence/live-grants/{grant_id}`
- `GET /api/trading-intelligence/migration-summary`

## 验证

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/cosmos_quant backend/tests
.\.venv\Scripts\python.exe -m pytest backend/tests -q
npm run typecheck --workspace apps/web
npm run lint --workspace apps/web
npm run test --workspace apps/web
npm run build --workspace apps/web
```

运行契约见 [Trading Intelligence v2](docs/TRADING_INTELLIGENCE_V2.md)，产品操作见 [产品使用说明书](docs/PRODUCT_USER_MANUAL.md)，架构见 [ARCHITECTURE](docs/ARCHITECTURE.md)。量化交易可能损失全部本金；回测和历史表现不代表未来收益。
