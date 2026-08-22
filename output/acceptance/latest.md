# COSMOS 自动验收报告

- 生成时间：2026-08-22T00:19:17.677794+00:00
- 范围：cosmos-core-v1
- 系统能力得分：100.0/100
- 当前策略认证：hold
- 工程得分：94.0/100
- 综合得分：78.3/100
- 盈利证据：0/100
- 盈利保证：否

## 量化研究终端基础设施：100/100

| 检查 | 权重 | 结论 | 证据 |
|---|---:|---|---|
| 完整后端与前端回归 | 20 | PASS | ........................................................................ [ 50%] ........................................................................ [ 63%] ........................................................................ [ 75%] ........................................................................ [ 88%] .................................................................        [100%] ============================== warnings summary =============================== .venv\Lib\site-packages\fastapi\testclient.py:1   F:\Cosmos\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.     from starlette.testclient import TestClient as TestClient  # noqa  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html 569 passed, 1 warning in 67.09s (0:01:07) > @cosmos/web@1.0.0 typecheck > tsc -b --pretty false > @cosmos/web@1.0.0 lint > eslint .  ✔ formats the same candle instant in the selected chart time zone (0.8912ms) ✔ uses the selected time zone for chart axis ticks (0.4232ms) ✔ reports the browser local UTC offset explicitly (0.1225ms) ℹ tests 3 ℹ suites 0 ℹ pass 3 ℹ fail 0 ℹ cancelled 0 ℹ skipped 0 ℹ todo 0 ℹ duration_ms 131.0435 transforming... ✓ 1597 modules transformed. rendering chunks... computing gzip size... dist/index.html                   0.60 kB │ gzip:   0.40 kB dist/assets/index-BNnwu50y.css  118.28 kB │ gzip:  22.35 kB dist/assets/index-m8G6h4MV.js   563.09 kB │ gzip: 173.35 kB ✓ built in 1.74s (!) Some chunks are larger than 500 kB after minification. Consider: - Using dynamic import() to code-split the application - Use build.rollupOptions.output.manualChunks to improve chunking: https://rollupjs.org/configuration-options/#output-manualchunks - Adjust chunk size limit for this warning via build.chunkSizeWarningLimit. |
| 持久化 Campaign 完成 | 15 | PASS | campaign_1fd53bf74f734576: 132 cells, 6 strategies × 44 datasets, 2 exchanges, selected=None, execution=False |
| 单元级缓存可重放 | 15 | PASS | campaign_1fd53bf74f734576: 132/132 缓存命中，4.177秒 |
| Campaign 数据集指纹全部验证 | 20 | PASS | binance ATOM/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx BCH/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance TRX/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance ADA/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ATOM/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx TRX/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ADA/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance LINK/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance LINK/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance ALGO/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance DOGE/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx LTC/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance TRX/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance ATOM/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ATOM/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance ETC/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance LTC/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ALGO/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx XRP/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ETC/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx DOGE/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance DOGE/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance LTC/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx XLM/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance XLM/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance ALGO/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx LTC/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ETC/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx LINK/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx TRX/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance XRP/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance BCH/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx LINK/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance XLM/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx BCH/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance ADA/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance BCH/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx XRP/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ALGO/USDT 4h: 1643 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx XLM/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance ETC/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; binance XRP/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx DOGE/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000; okx ADA/USDT 4h: 6528 bars, funding 0.0%, OI 0.0000, positioning 0.0000, basis 0.0000, order flow 0.0000, liquidation flow 0.0000 |
| CPU 回测进程隔离 | 15 | PASS | ds_da618bad00bec70ff1676068: 26281 bars, worker_process; API health p95=40.6ms, samples=16 |
| 无陈旧运行任务 | 15 | PASS | 0 个陈旧运行任务 |

## Agent 自主研究闭环：90/100

| 检查 | 权重 | 结论 | 证据 |
|---|---:|---|---|
| 完整概率化市场感知 | 20 | PASS | 522 个持久化结构化 analyze 决策；实时源=okx:websocket，当前状态=persistent_trend，不确定=False，高周期=4h/persistent_trend，波动=normal，衍生品=long_build，微观方向=neutral，建议=wait；核心最低基差覆盖 100.0%，订单流覆盖 0.0%，实时爆仓 1555 个 |
| 策略库检索 | 10 | PASS | 114 个可检索单腿模板（数据库已播种 109 个）+ 6 个公开相对价值模板 + 2 个公开期现 Carry 模板 |
| Agent 候选生成 | 10 | PASS | 106 个 AI/ML 候选 |
| 失败驱动父子版本修改 | 10 | PASS | 102 个单腿父子修改候选 + 7 个有界相对价值子假设 |
| 独立反过拟合验证器 | 10 | PASS | Campaign=True，PBO 已执行 10000 个时间折组合，结果 passed=True；相对价值 Campaign=paircampaign_8fdc3f3d236a417b，PBO=0.000；Carry Campaign=carrycampaign_8f88ba10f0d441bc，PBO=1.000 |
| 有界搜索与重复试验约束 | 5 | PASS | 永久账本谱系=26，实验=88，谱系内策略×引擎试验=1686；跨下载试验家族=1，家族实验=79，家族策略×引擎试验=998；最新 Campaign 累计=992，序贯观察=77，有效试验=1068；相对价值谱系=pair_lineage_30d585039fe510fbf1cd，累计=13，序贯=3；Carry谱系=carry_lineage_40033ba8956ff18c271a，累计=4，序贯=2 |
| 失败与退役策略记忆 | 10 | PASS | 7 个拒绝或退役候选 |
| 修订产生稳健改进 | 10 | HOLD | 已生成单腿与相对价值父子修订，但尚无修订候选成为稳健Champion；最新单腿后继=campaign_1fd53bf74f734576，PBO=0.070；最新相对价值后继 PBO=0.000 |
| 研究覆盖至少25个策略家族 | 10 | PASS | 65/25 个研究策略原型，单腿家族分布 {'trend': 32, 'mean_reversion': 54, 'breakout': 28}，相对价值原型 ['log_spread_equal_dollar', 'relative_momentum_equal_dollar', 'rolling_beta_residual']，Carry原型 ['spot_perpetual_cash_and_carry'] |
| 无合格策略时保持空仓 | 5 | PASS | Champion=False，人工风险闩锁=True，闩锁后新增风险回合=0；既有受保护仓位回合=0 |

## 加密市场数据完整度：100/100

| 检查 | 权重 | 结论 | 证据 |
|---|---:|---|---|
| 核心币种与周期覆盖 | 15 | PASS | 严格历史合同 6/6 单元通过，特征得分 30.00，总分 100.00/100 |
| 点时市场宇宙与历史覆盖契约完整物化 | 15 | PASS | universe_69bbcc6363afe16073be07a9 v4: selected=22, materialized=22/22, datasets=44, missing=[], historical_survivorship=True, prospective=True, archive_source_verified=True (153 records, 1f51c8b36caf101fdb7d4ef4fb16f1fcbae7d1cd798532560eb9190370b524a6, 118 records, ec25e8b2e958bb4a9e73d97cd7e7451bd9dba0e831c63a8f4ff0e991a2578a88), invalid_legacy_artifacts=1 |
| 生存偏差由历史市场档案或前瞻样本控制 | 15 | PASS | universe_69bbcc6363afe16073be07a9 v4: selected=22, materialized=22/22, datasets=44, missing=[], historical_survivorship=True, prospective=True, archive_source_verified=True (153 records, 1f51c8b36caf101fdb7d4ef4fb16f1fcbae7d1cd798532560eb9190370b524a6, 118 records, ec25e8b2e958bb4a9e73d97cd7e7451bd9dba0e831c63a8f4ff0e991a2578a88), invalid_legacy_artifacts=1; prospective catalog journal: okx:spot observations=2, events=1354, chain=True, schedule_gap_free=False; okx:swap observations=2, events=447, chain=True, schedule_gap_free=False; binance:spot observations=2, events=3681, chain=True, schedule_gap_free=False; binance:swap observations=2, events=881, chain=True, schedule_gap_free=False |
| K线无缺口且无非法值 | 15 | PASS | 严格历史合同 6/6 单元通过，特征得分 30.00，总分 100.00/100 |
| 历史资金费率覆盖 | 10 | PASS | 严格历史合同 6/6 单元通过，特征得分 15.00，总分 100.00/100 |
| 历史持仓量覆盖 | 10 | PASS | 严格历史合同 6/6 单元通过，特征得分 15.00，总分 100.00/100 |
| 至少两个交易所独立数据 | 10 | PASS | 严格K线合同要求每个核心市场/周期至少两个合格交易所；严格历史合同 6/6 单元通过，特征得分 30.00，总分 100.00/100 |
| 基差、爆仓和订单流历史 | 10 | PASS | positioning=True，basis=True，order_flow=True，liquidations=True；严格历史数据总分 100.00/100；禁止历史伪回填 |

## 自动产生可靠策略的能力：80/100

| 检查 | 权重 | 结论 | 证据 |
|---|---:|---|---|
| 至少25个不同策略家族 | 20 | PASS | 65/25 个研究策略原型，单腿家族分布 {'trend': 32, 'mean_reversion': 54, 'breakout': 28}，相对价值原型 ['log_spread_equal_dollar', 'relative_momentum_equal_dollar', 'rolling_beta_residual']，Carry原型 ['spot_perpetual_cash_and_carry'] |
| 至少80个标准策略模板 | 15 | PASS | 114/80 个版本化可执行模板 |
| 跨市场矩阵完整执行 | 15 | PASS | campaign_1fd53bf74f734576: 132 cells, 6 strategies × 44 datasets, 2 exchanges, selected=None, execution=False |
| PBO置信上界通过 | 15 | PASS | passed=True, UCB=0.074 |
| 存在跨市场稳健 Champion | 20 | HOLD | selected=None, execution=False |
| 不可篡改策略执行证书 | 15 | PASS | Campaign engine v17；证书绑定策略、开发/盲测数据指纹、PBO、完整开发/盲测聚合指纹与策略级FDR；篡改和盲集复用测试通过 |

## 自主实盘准备度：100/100

| 检查 | 权重 | 结论 | 证据 |
|---|---:|---|---|
| 订单与仓位无不确定状态 | 15 | PASS | 0 个不确定回合；运行时 unknown orders=0，unknown positions=0，unprotected=0 |
| 订单幂等账本 | 15 | PASS | 0 个重复 client_order_id；重复提交故障测试通过 |
| 成交后原生保护单 | 15 | PASS | 沙盒仓位管理 commissioning=True，运行时对账干净=True；原子保护、失败回滚、部分减仓保护再平衡测试已执行 |
| 执行故障注入套件 | 20 | PASS | 32 类执行/研究完整性故障：32 类通过；........................................................................ [ 32%] ........................................................................ [ 65%] ........................................................................ [ 98%] ....                                                                     [100%] |
| 重启恢复验证 | 15 | PASS | 重启中断的只读任务可关闭；可能已到达交易所的执行任务转为 uncertain 并强制对账；人工停机闩锁跨恢复路径保持 |
| 只有认证策略可以开仓 | 20 | PASS | Campaign engine v17；证书绑定策略、开发/盲测数据指纹、PBO、完整开发/盲测聚合指纹与策略级FDR；篡改和盲集复用测试通过 |

## 稳定盈利证据等级：0/100

| 检查 | 权重 | 结论 | 证据 |
|---|---:|---|---|
| PBO与多重检验 | 15 | PASS | passed=True, UCB=0.074 |
| 跨市场样本外稳健 | 20 | HOLD | selected=None, execution=False |
| 成本压力后仍盈利 | 15 | HOLD | 无Champion |
| 参数邻域稳定 | 10 | HOLD | 无Champion |
| 收益不依赖单一币种 | 10 | HOLD | 无Champion |
| 胜率与盈亏比置信下界达标 | 10 | HOLD | 无Champion |
| 低相关组合通过 | 10 | HOLD | 无合格策略，无法形成合格组合 |
| 事件驱动真实成交证据 | 10 | HOLD | 没有由合格Champion产生的真实因果成交证据 |

## 主要阻塞项

- 没有跨市场稳健 Champion，盈利证据分强制为0
- 修订产生稳健改进
- 存在跨市场稳健 Champion
- 跨市场样本外稳健
- 成本压力后仍盈利
- 参数邻域稳定
- 收益不依赖单一币种
- 胜率与盈亏比置信下界达标
- 低相关组合通过
- 事件驱动真实成交证据
