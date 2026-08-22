# COSMOS Quant 产品使用说明书

版本：Operator Workspace v3 / Autonomous Research v2 / Campaign v18
更新日期：2026-08-23
适用平台：Windows 10/11
项目目录：`F:\Cosmos`
默认访问地址：<http://127.0.0.1:8000>

> 风险提示：COSMOS 是量化研究与受控交易系统，不保证盈利。回测、Shadow、测试网和历史证书都不能保证未来收益。首次使用必须从 PAPER 开始，完成测试网验收后才考虑极小风险的实盘运行。

## 1. 产品运行链路

COSMOS 的完整工作链路如下：

```text
启动服务
  → 系统设置与行情连接
  → AI 服务验收（可选）
  → 策略/自主研究
  → 回测与统计裁决
  → 审核、证书或 Shadow
  → 风险检查
  → PAPER
  → SANDBOX 测试网验收
  → Forward Evidence
  → LIVE_CAPPED（满足条件时）
  → 持仓、订单、退出与复盘
```

任何一步证据不足，系统都会保持 `HOLD`、`SHADOW_ONLY`、`REJECTED` 或 `UNTESTABLE`，不会为了“持续交易”而降低门槛。

## 2. 首次安装

### 2.1 环境要求

- Python 3.12 或更高版本
- Node.js 20 或更高版本
- npm
- Windows PowerShell
- 能访问所选交易所公共行情接口的网络
- 使用 AI 研究时，需要相应 AI 厂商的 API Key
- 使用 SANDBOX/LIVE 时，需要对应模式的交易所 API 凭证

交易所 API Key 只应授予读取与交易权限，禁止授予提现权限。实盘 API Key 建议绑定本机出口 IP。

### 2.2 安装依赖

打开 PowerShell：

```powershell
Set-Location F:\Cosmos
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
npm install
```

如果项目已经安装完成，不需要重复执行本节。

## 3. 打开、检查与关闭项目

### 3.1 首次启动

首次启动或前端代码更新后运行：

```powershell
Set-Location F:\Cosmos
npm run service:start:build
```

该命令会构建前端、启动后端，并等待健康检查通过。

### 3.2 日常启动

```powershell
Set-Location F:\Cosmos
npm run service:start
```

看到以下信息代表服务已启动：

```text
COSMOS API is healthy at http://127.0.0.1:8000
```

启动器会拒绝重复进程，因此不需要反复点击或重复运行。

### 3.3 打开产品

在浏览器打开：

- 主界面：<http://127.0.0.1:8000>
- 持仓与订单：<http://127.0.0.1:8000/?section=portfolio>
- 风险控制：<http://127.0.0.1:8000/?section=risk>
- 交易 Agent：<http://127.0.0.1:8000/?section=agent>
- 研究 Agent：<http://127.0.0.1:8000/?section=researchAgent>
- 策略库：<http://127.0.0.1:8000/?section=library>
- 策略编辑器：<http://127.0.0.1:8000/?section=editor>
- 运行日志：<http://127.0.0.1:8000/?section=logs>
- AI 服务：<http://127.0.0.1:8000/?section=ai>
- 系统设置：<http://127.0.0.1:8000/?section=settings>
- API 文档：<http://127.0.0.1:8000/docs>

网站仅绑定本机回环地址，默认不能被局域网或公网直接访问。

### 3.4 检查运行状态

```powershell
npm run service:status
```

也可以检查：

- `GET http://127.0.0.1:8000/api/health`：进程健康状态
- `GET http://127.0.0.1:8000/api/ready`：数据库、运行时和关键服务就绪状态
- `GET http://127.0.0.1:8000/api/agent/readiness`：Agent 自主交易就绪状态

### 3.5 正常关闭

在没有未处理订单、未知成交或需要实时管理的持仓时运行：

```powershell
npm run service:stop
```

注意：关闭程序不会替你处理交易所上的真实持仓和挂单。SANDBOX/LIVE 关机前必须先确认账户、挂单、保护单和系统账本一致。

## 4. 界面导航

| 页面 | 用途 |
|---|---|
| 交易台 | 查看行情、当前计划、入场区间、止盈、止损、仓位与执行入口 |
| 持仓与订单 | 查看当前仓位、挂单、部分成交、历史订单和受控撤单 |
| 风险控制 | 查看逐项风险检查、全局 HALT 和恢复入口 |
| 交易 Agent | 快速启动受控自动交易，查看市场感知、当前策略、决策、交易回合与目标状态 |
| 研究 Agent | 查看候选生成、Campaign、验证证据、漂移诊断与 Champion 治理 |
| 策略库 | 统一管理内置、个人和 Agent 策略，执行复制、隐藏、归档、恢复与受约束删除 |
| 策略编辑器 | 输入研究意图，生成确定性策略计划并完成回测与人工审核 |
| 运行日志 | 按时间、级别、类别和关键词筛选审计事件，并增量加载历史记录 |
| AI 服务 | 配置 AI 厂商、模型、凭证、连接验收和研究路由 |
| 设置 | 配置交易所、运行模式、市场、风险、策略参数和自动执行 |

左侧导航在 1440 像素以上可显示完整分组文字；较窄窗口自动收起为图标栏。顶栏始终显示行情连接、交易环境、风险闸、运行中的 Agent 数量和最近心跳。点击 Agent 状态可直接进入交易 Agent 页面。

顶栏“舒适/紧凑”按钮用于切换信息密度，选择会保存在浏览器中。舒适模式用于日常操作；只有在需要同时查看更多表格行时才建议使用紧凑模式。

## 5. 首次使用：PAPER 模拟盘

PAPER 使用真实公共行情和本地模拟账户，不向交易所提交订单，是所有新用户的必经阶段。

### 5.1 系统设置

打开“设置”，在“交易连接”中配置：

1. 运行模式选择“真实行情模拟盘”。
2. 交易所优先选择 OKX。
3. 合约交易选择“USDT 永续”。
4. 选择默认品种，例如 `BTC/USDT`。
5. 选择默认周期，例如 `1h` 或 `4h`。
6. 如本机需要代理，填写 `http://127.0.0.1:7890` 一类 HTTP(S) 地址。
7. 点击“测试连接”。
8. 公共行情正常后点击“保存并应用”。

系统不会使用虚构行情作为降级数据。公共行情不可用时，页面会明确显示错误并停止生成可执行计划。

### 5.2 风险设置

打开设置页的“风险”标签，逐项确认：

- 单笔最大风险
- 日内最大回撤
- 组合风险预算
- 总敞口与同向敞口
- 单品种敞口
- 最大杠杆
- 最多同时持仓
- 最大价差
- 资金费率上限
- 止损到清算价的安全缓冲

建议首次 PAPER 仍使用保守参数，单笔风险不超过账户权益的 `0.25%–0.5%`。不要因为是模拟盘而使用无法迁移到实盘的夸张杠杆。

### 5.3 策略默认值

在设置页“策略默认值”中选择：

- 默认策略家族：趋势回踩、均值回归或区间突破
- 研究历史 K 线数量
- 关注品种列表
- 研究周期
- 压力手续费和压力滑点
- 是否按市场状态自动选择策略家族
- 是否要求高周期确认
- 策略扫描间隔
- 是否允许入场条件满足后自动执行

Campaign v18 中，胜率、Profit Factor 和平均盈亏比继续展示并可用于描述、兼容和排序，但正式自主研究裁决以净期望、Expected R、成本后期望、bootstrap 下界、DSR、PBO、purged OOF stability 和 Online FDR 为主。

首次运行时不要立即打开“自动执行”。先完成手动生成、回测、审核和 PAPER 下单闭环。

## 6. 配置 AI 服务（可选）

没有 AI 服务时，系统仍可使用认证的确定性生成器和已有策略库运行。AI 只负责 Research Layer，不拥有数据库句柄、证书签发、风险调整或下单权限。

在“AI 服务”页面：

1. 选择 OpenAI、Anthropic、Gemini、xAI、DeepSeek、Mistral、Qwen 或兼容端点。
2. 选择或填写模型端点。
3. 输入 API Key。
4. 点击“保存 AI 配置”。
5. 点击“刷新模型目录”，确认模型 ID 存在。
6. 点击“测试连接”，等待显示“运行就绪”。
7. 确认验收成功后，再决定是否加入自动研究路由。

API Key 只写入本机操作系统凭证库，不写入 SQLite、日志或浏览器返回。已有凭证不会回显，输入框留空保存不会覆盖原凭证。

如果 LLM 在 8 秒预算内失败，同一事件不会不断重试。系统会降级到认证确定性生成器；生成器也不可用时进入 SAFE_MODE，只管理已有有效证书，不新增研究风险。

## 7. 使用策略编辑器与策略库

### 7.1 生成策略

打开“策略编辑器”页面：

1. 输入研究意图。它用于审计和研究上下文，不会直接变成交易所指令。
2. 选择品种、周期和策略算法。
3. PAPER 模式可填写模拟权益；SANDBOX/LIVE 必须从交易所读取真实权益。
4. 点击“生成策略并运行回测”。
5. 查看系统生成的方向、入场区间、止盈、止损、风险金额、仓位和失效条件。

如果系统判断当前没有明确方向，会显示 `NO TRADE`。这属于正常安全结果，不是故障。

### 7.2 回测与审核

1. 策略生成后，页面会自动运行回测。
2. 查看收益、Profit Factor、最大回撤、交易数量和未通过原因。
3. 确认数据指纹、策略指纹和方向与当前计划一致。
4. 只有稳健性门槛通过时，才能点击“审核此版本”。
5. 审核后可保存到个人策略库，或回到交易台继续激活与执行流程。

重新生成策略会产生新指纹，旧回测不能自动授权新版本。

### 7.3 激活与 PAPER 执行

1. 在交易台检查当前价格是否接近策略入场区间。
2. 检查“风险检查”是否为 PASS。
3. 点击“激活策略”。
4. 阅读确认弹窗并勾选已核对方向、入场、止损和风险金额。
5. 点击“确认激活纸面策略”。
6. 条件满足后点击“提交纸面订单”；开启自动执行时由系统在条件满足后提交。
7. 在“持仓与订单”中跟踪挂单、成交、止盈和止损。

### 7.4 管理统一策略库

“策略库”将策略分为三类：

- 内置策略：公开、不可变的研究先验，不能删除；不需要显示时可以隐藏并随时恢复。
- 个人策略：由可视化编辑器创建，或从当前计划、内置策略和 Agent 候选复制而来。
- Agent 策略：由研究 Agent、AI 或 ML 产生，必须保留来源和验证证据。

新建个人策略时填写名称、策略族、研究假设和常用参数；复杂字段位于“高级 StrategySpec JSON”。保存前后端会执行完整 `StrategySpec` 校验，不能通过前端输入绕过字段范围和交叉参数限制。

删除与归档规则：

- 未回测、未认证、未交易、未被计划引用的个人草稿可以永久删除。
- 已回测、已认证、已交易、已引用或由 Agent 生成的策略只能归档。
- Agent 候选归档后进入 `retired`，恢复时回到 `proposed` 并必须重新验证。
- 当前 Champion 不能直接归档，必须先由新的 Champion 替代。
- 所有危险操作都会显示策略名称和影响范围；不要在未核对时确认。

## 8. Autonomous Research v2

### 8.1 Agent 能做什么

升级后的研究链路是：

```text
MarketSnapshot
  → ResearchHypothesisV2
  → Certified Generator / JSON DSL
  → Semantic & Integrity Gates
  → Permanent Trial / Online FDR
  → Fast Vector Gate
  → Exact & Risk Gate
  → Execution Eligibility / Shadow
  → Slow Certification
```

Agent 可以感知市场、提出经济假设、选择认证生成器、生成受限 JSON DSL、提出受控变异并总结失败。它不能直接写数据库、签证书、修改风险额度或下单。

### 8.2 两类策略的权限差异

| 来源 | 验证通过后的最高结果 |
|---|---|
| 认证生成器，且 provenance/envelope/build/Fast/Exact/Risk 全部有效 | 可获得短期 `EXECUTION_ELIGIBLE` |
| LLM 自由生成 DSL | 最高为 `SHADOW_ONLY` |
| envelope 外算子、特征或结构 | `SHADOW_ONLY` 或拒绝 |
| 数据、时间语义或样本不足 | `UNTESTABLE` |
| 负期望、假设证伪或安全违规 | `REJECTED` |

即使自由 DSL 的历史结果很好，也不能直接交易。

### 8.3 五分钟 Fast Gate

300 秒从 versioned point-in-time snapshot 准备完成开始计时，直到裁决、Trial Ledger、fingerprint 和证据引用原子提交完成。

单个事件最多允许：

- 12 个 semantic proposals
- 3 个 empirical trials
- 2 个 Exact finalists
- 1 轮结构进化

超时返回 `UNTESTABLE(deadline_exceeded)`；已经消费的 trial 和 alpha 不退还。

### 8.4 如何调用自主研究

研究 Agent 页面已经提供候选、Campaign、验证证据和技术诊断工作区；Autonomous Research v2 的底层公开入口仍保留在 API，供高级用户或外部调度器在 Swagger 页面操作：

1. 打开 <http://127.0.0.1:8000/docs>。
2. 展开 `POST /api/research/autonomous-runs`。
3. 点击“Try it out”。
4. 按 OpenAPI Schema 填写 `AutonomousResearchRunRequest`。
5. 确保 snapshot 的 `event_time_ns`、`available_time_ns` 和 `decision_time_ns` 一一对应，并满足 `available_time <= decision_time`。
6. 使用唯一的 `idempotency_key` 和 `research_event_id`。
7. 提交后只读取公开脱敏结果。

常用查询：

- `GET /api/research/autonomous-runs/{run_id}`：查询裁决
- `GET /api/research/trials`：查询永久 Trial Ledger 的脱敏记录
- `GET /api/research/certificates/{certificate_id}`：查询证书和撤销状态

公开结果只包含 disposition、粗粒度原因码、证据引用、耗时、deadline 状态和 artifact fingerprint，不返回盲测 PnL、Sharpe、市场拆分或归因。

### 8.5 如何理解裁决结果

- `EXECUTION_ELIGIBLE`：获得短期资格，不代表已经下单。仍需当前市场状态、价差、流动性、风险和执行检查。
- `SHADOW_ONLY`：只能记录信号和前向证据，不能新增风险。
- `REJECTED`：本次结构、假设或经济证据不成立，不应通过换名字或轻微参数重试来绕过预算。
- `UNTESTABLE`：当前证据无法可靠裁决，应先修复数据、PIT 语义、特征覆盖、样本或 deadline 问题。

快速资格默认只使用成熟参考策略风险预算的 5%，硬上限为 10%，且只允许一个 active instance。资格 TTL 为一根目标周期 K 线；到期后必须重新验证。TTL 到期不会机械强平已有仓位，已有仓位按原 invalidation 和 exit 管理。

## 9. 使用交易 Agent

### 9.1 快速启动自动交易

打开“交易 Agent”，在左侧“快速启动”区域：

1. 选择允许感知和交易的品种。
2. 选择运行周期。
3. 核对当前 PAPER、SANDBOX 或 LIVE 环境。
4. 核对平台默认风险模板、单笔风险和原生保护单要求。
5. 点击“启动自动交易”。

“启动自动交易”表示 Agent 开始持续扫描市场，不代表立刻提交订单。只有策略、回测、证书/执行资格、当前信号、账户状态和确定性硬风控全部通过后，系统才允许开仓。

首次使用某个权限范围时会显示图形化授权确认。授权指纹包含品种、周期、运行模式、风险模板和实际风险策略。后续使用完全相同的范围可以一键启动；扩大品种或周期、切换交易环境或改变风险范围时必须重新确认。

相同启动配置会复用原有目标，不会重复创建多个 Agent。页面显示“已恢复现有自动交易目标”时，应继续核对该目标的历史和当前状态。

### 9.2 先读懂首屏状态

交易 Agent 首屏固定回答四个问题：

- 当前状态：正在推理、持续感知、已暂停、风险阻塞或状态失联。
- 市场感知：授权品种和周期、最近事件时间与下次扫描时间。
- 当前策略：当前选择的策略，或“等待合格策略”及具体原因。
- 最近动作：最近一次 WAIT、研究、下单、撤单、减仓、退出或 HALT 决策。

WebSocket 正常时页面显示“实时状态已连接”。断线后会自动切换轻量轮询，并显示“状态可能延迟”；此时不要仅根据页面静默判断 Agent 已停止。

全局风险闸为 HALT 时，Agent 仍可感知和研究，但不能绕过限制新增风险。

### 9.3 使用二级工作区

- “概览”：查看市场感知、最近生成策略和最近决策。
- “运行活动”：查看每次运行的状态、step 数量和决策证据。
- “交易回合”：查看方向、状态、计划风险、净收益和时间。
- “绩效与记忆”：查看前向绩效和可追溯记忆。
- “目标管理”：暂停、恢复、立即运行、归档目标，并查看自主交易就绪检查。

点击某次运行可打开决策、观察和工具结果抽屉。同一市场有多个候选时，分数只在已经通过硬门槛的候选之间排序，不是预测胜率。

### 9.4 暂停、归档与恢复

- 暂停：停止该目标的新调度；历史运行和交易回合不受影响。
- 恢复：将暂停目标重新设为 active。
- 立即运行一次：只触发一次有界运行，不会放宽权限和风险限制。
- 归档：从可管理目标列表移除，但保留策略、订单、运行和审计证据。
- 恢复归档：在“目标管理 → 已归档目标”中恢复；恢复后保持暂停，必须人工核对范围再启动。

Agent 不能修改自己的授权，也不能自行恢复全局 HALT。

### 9.5 交易 Agent 与研究 Agent 的边界

交易 Agent 负责市场感知、选择已经验证的策略和受控执行；研究 Agent 负责候选生成、Campaign、回测、Shadow、漂移和治理。研究 Agent 不直接控制订单，交易 Agent 也不会在执行页面修改研究证据。

## 10. SANDBOX 测试网

只有 PAPER 流程稳定后才进入 SANDBOX。

### 10.1 准备 OKX Demo

1. 在 OKX 创建 Demo Trading API Key。
2. 权限只选择读取和交易，禁止提现。
3. 在 OKX Demo 页面将 Account mode 设置为 Futures mode。
4. USDT 永续使用 `net_mode` 单向持仓模式。
5. 准备 API Key、Secret 和 Passphrase。

### 10.2 配置测试网

在“设置 → 交易连接”中：

1. 运行模式选择“交易所测试网”。
2. 交易所选择 OKX。
3. 市场选择 USDT 永续。
4. 设置保证金模式和固定执行杠杆。
5. 输入 Demo API Key、Secret、Passphrase。
6. 点击“测试连接”。
7. 确认公共行情、私有账户和原子保护单能力正常。
8. 点击“保存并应用”。

测试网和实盘凭证相互隔离，不会复用。

### 10.3 原子保护单验收

点击“开始保护单验收”。系统会：

1. 提交远离市价的最小测试订单。
2. 查询确认原生止盈和止损关系。
3. 立即撤销测试订单。
4. 再次查询最终状态。
5. 持久化验收结果。

未通过前禁止交易所自动执行。

### 10.4 Agent 仓位管理验收

点击“开始仓位管理验收”。该操作会在 OKX Demo 中真实成交最小测试仓位，用于验证：

- 部分减仓
- 保护单同步缩量
- 最终平仓
- Algo 单清理
- 空仓回查

验收期间不要在交易所网页手动干预测试订单。失败或结果不确定会触发全局 HALT，需要人工核对账户和对账结果。

### 10.5 创建 SANDBOX Agent

1. 确认系统设置已经处于 SANDBOX。
2. 打开交易 Agent，选择已验收的品种和周期。
3. 核对“测试网”、风险模板、单笔风险和原生保护单摘要。
4. 点击“启动自动交易”，并完成首次 SANDBOX 权限确认。
5. 在“目标管理”中等待自主交易就绪检查全部通过。
6. 保持目标 active，积累真实测试网成交与前向证据。

应演练断网、重启、部分成交、撤单、保护单异常、全局 HALT 和恢复。不要只验证“能开仓”，必须验证“能安全退出和恢复”。

## 11. LIVE_CAPPED 实盘

实盘不是安装完成后的默认下一步。只有以下条件同时满足时才考虑：

- PAPER 闭环稳定
- SANDBOX 原子保护单验收通过
- Agent 仓位管理验收通过
- 当前无未知订单、待对账回合或不确定状态
- Forward evidence 达到配置的质量标准
- 使用有效证书或有效短期执行资格
- 最新 Fast/Exact/Risk/Market-State 检查通过
- RuntimeBuildIdentity 为 clean build
- 操作者理解并接受可能损失全部本金

### 11.1 切换前检查

1. 暂停 SANDBOX Agent 目标。
2. 确认 SANDBOX 无挂单和持仓。
3. 运行账户对账。
4. 查看“风险”页面，确认没有 HALT 或未解释告警。
5. 备份数据库。
6. 准备独立的实盘 API Key。

### 11.2 启用实盘

在设置页：

1. 选择 LIVE。
2. 输入独立实盘 API Key、Secret 和 Passphrase。
3. 测试公共行情、私有账户和保护单能力。
4. 打开“允许真实下单”。
5. 输入精确确认短语：

```text
I ACCEPT LIVE TRADING RISK
```

6. 保存并应用。
7. 如需延续 Agent，选择符合条件的 SANDBOX 目标进行 `live_capped` 迁移。

迁移不会自动放大风险预算。首次实盘建议单笔风险不超过权益的 `0.25%–0.5%`，并低于系统硬上限。

### 11.3 实盘运行

1. 打开交易台确认实时行情不是 stale/offline。
2. 确认计划、证书、方向、周期和交易环境一致。
3. 确认风险页为 PASS。
4. 确认 Agent Readiness 全部通过。
5. 激活策略或保持符合资格的 Agent 目标 active。
6. 首次下单应人工盯盘，确认入场和原生保护单都出现在交易所。
7. 在“持仓与订单”及交易所页面交叉核对数量、均价、止损和止盈。
8. 平仓后确认交易回合已完成复盘和净收益归因。

任何保护单缺失、状态未知、对账不一致或行情陈旧都应停止新增风险。

## 12. 日常运行标准流程

### 12.1 开机检查

1. `npm run service:start`
2. `npm run service:status`
3. 打开 `/api/ready`
4. 查看交易台行情连接状态
5. 查看风险页面是否 HALT
6. 查看持仓、挂单和交易所账户是否一致
7. 查看 Agent Readiness
8. 检查目标状态和上一运行错误
9. 确认当前构建、数据、成本和证书未发生失配

### 12.2 运行中检查

- 行情是否实时
- 当前 spread 和 funding 是否越界
- 订单是否部分成交或长期未成交
- 原生保护单是否存在且数量正确
- 当前风险预算和组合敞口是否越界
- Agent 是否频繁返回数据或 deadline 错误
- Shadow 候选是否被错误理解为可执行策略
- 证书是否被撤销或 TTL 是否过期

### 12.3 收盘/停机检查

1. 暂停不需要继续运行的 Agent 目标。
2. 确认没有未知或待提交订单。
3. SANDBOX/LIVE 核对交易所挂单、持仓和保护单。
4. 确认需要跨夜的仓位有有效退出和失效规则。
5. 创建数据库备份。
6. 只有在无需持续管理仓位时才执行 `npm run service:stop`。

## 13. 紧急停止与恢复

### 13.1 紧急停止开仓

在“风险”页面点击“紧急停止开仓”。全局 HALT 会：

- 阻止新增风险
- 保留撤单能力
- 保留减仓和平仓能力
- 保留已有仓位的安全监控

紧急停止开仓比直接结束进程更安全，因为直接结束进程会同时停止本地监控。

### 13.2 何时必须 HALT

- 交易所订单与本地账本不一致
- 入场成交但保护单未确认
- 部分成交后的保护数量不一致
- API 返回不确定状态
- 行情长时间 stale/offline
- 私有订单流中断且 REST 对账失败
- 风险、凭证、环境或市场切换异常
- 发现非本系统订单或仓位

### 13.3 恢复新开仓

不要仅因为页面出现“恢复”按钮就点击。先完成：

1. 登录交易所人工核对持仓与挂单。
2. 修复或撤销异常订单。
3. 完成 REST/私有流对账。
4. 确认所有保护单有效。
5. 确认行情、凭证、保证金和风险状态正常。
6. 记录故障原因和处理证据。
7. 再点击“恢复新开仓”。

Agent 无权自行恢复全局 HALT。

## 14. 数据、备份与日志

### 14.1 主要位置

- 数据库：`F:\Cosmos\data\cosmos.db`
- 数据库备份：`F:\Cosmos\data\backups\`
- 运行日志和 PID 状态：`F:\Cosmos\output\`
- 最终升级验收：`F:\Cosmos\output\acceptance\autonomous_research_final.md`
- Fast Gate 基准：`F:\Cosmos\output\acceptance\autonomous_fast_gate_benchmark.json`

不要用 SQLite 编辑器直接修改生产表，也不要修改 append-only Trial Ledger、FDR、证书或撤销事件。

网页“运行日志”使用分页审计接口，可按严重级别、类别和关键词筛选，并展开查看 Agent、策略和订单关联数据。交易台只展示最近三条高优先级事件；完整排障必须进入运行日志页。

常用接口：

- `GET /api/audit/events`：分页和筛选审计事件
- `GET /api/audit`：兼容旧调用的审计列表
- `GET /api/agent/overview`：轻量 Agent 全局状态
- `WS /ws/agent`：Agent 目标、运行、决策与交易回合实时状态

### 14.2 手动备份

服务运行时可调用：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/maintenance/backup
```

重大配置切换、迁移和实盘运行前都应备份。

## 15. 常见问题

### 页面打不开

执行：

```powershell
npm run service:status
```

若未运行，执行 `npm run service:start`。若健康检查失败，查看 `F:\Cosmos\output\` 下的服务日志。

### 8000 端口被占用

先确认是否已有 COSMOS 实例。不要直接启动第二个会访问同一数据库的实例。只有明确确认占用者不是 COSMOS 后，才处理占用进程或改用其他端口。

### 行情不可用

检查网络、代理、DNS、交易所服务状态和所选品种。COSMOS 不会用假数据替代真实行情。

### AI 测试失败

检查 API Key、模型 ID、端点、额度、地区可用性和代理。AI 失败不会自动放宽研究或交易门槛。

### 策略始终 HOLD

查看具体原因：可能是当前没有方向、样本不足、Expected R 下界不为正、成本压力失败、PBO/FDR 不通过、数据 PIT 语义不可验证或风险条件不满足。HOLD 是有效裁决。

### 自由 DSL 全部是 SHADOW_ONLY

这是设计要求。只有通过认证生成器 provenance 和有效 Generator Envelope 生成的候选，才可能进入五分钟执行资格路径。

### 返回 UNTESTABLE

优先检查数据/特征覆盖、available_time、warmup、样本量、市场状态输入和 deadline。不要把 UNTESTABLE 当作 REJECTED，也不要通过重试洗掉 Trial/FDR 预算。

### Agent 不下单

依次检查：

- 目标是否 active，页面是否显示“持续感知”或“正在推理”
- WebSocket 是否已连接；轮询降级时状态是否可能延迟
- 自主级别是否允许当前模式交易
- 授权指纹是否仍有效
- Agent Readiness 是否全部通过
- 是否存在有效策略或证书
- 最新信号是否明确
- 当前风险是否 PASS
- 是否处于全局 HALT
- SANDBOX commissioning 是否通过
- 自动执行是否开启，或是否需要人工提交

一键启动后没有立即开仓是正常行为。它只启动持续扫描；当前没有合格信号、没有 Champion/证书、风险闸 HALT 或保护单验收未通过时，Agent 必须保持 WAIT。

### 策略无法删除

内置策略不可删除，只能隐藏。Agent 策略、已回测、已认证、已交易或被计划引用的个人策略必须保留审计链，只能归档。只有未引用的个人草稿会显示永久删除按钮。

### Agent 状态看起来没有更新

先查看交易 Agent 页右上角连接状态。显示“轮询降级”时等待一次轮询并检查 `/api/agent/overview`；如果最近心跳持续不变，再检查目标是否暂停、市场数据是否可用以及后台服务日志。

### 无法从 SANDBOX 切换 LIVE

常见原因是旧环境仍有持仓/挂单、待复盘交易回合、不确定状态、实盘凭证未通过、确认短语错误、风险范围收窄后不覆盖旧目标，或目标尚不具备 live_capped 条件。

## 16. 推荐的完整验收顺序

首次使用建议按以下顺序逐项打勾：

- [ ] 服务健康和 ready 检查通过
- [ ] PAPER 公共行情连接通过
- [ ] 风险参数已人工确认
- [ ] 策略已生成、回测、审核和激活
- [ ] 个人策略新建、校验、归档、恢复和草稿删除规则已验收
- [ ] 内置策略隐藏与恢复已验收
- [ ] PAPER 入场、止盈、止损和复盘闭环通过
- [ ] AI 服务连接验收通过（如使用）
- [ ] 研究 Agent 候选、Campaign、验证证据和 WAIT/HOLD 行为符合预期
- [ ] 交易 Agent 首次授权和相同范围幂等复用已验收
- [ ] WebSocket 实时状态和轮询降级提示已验收
- [ ] PAPER Agent Readiness 通过
- [ ] PAPER Agent 自动交易闭环通过
- [ ] OKX Demo Futures mode 和 net_mode 配置完成
- [ ] SANDBOX 公共行情、私有账户和保护单能力通过
- [ ] 原子保护单 commissioning 通过
- [ ] Agent 仓位管理 commissioning 通过
- [ ] SANDBOX Agent 闭环和异常演练通过
- [ ] Agent 暂停、恢复、归档和安全恢复流程通过
- [ ] 运行日志筛选、分页和关联数据展开通过
- [ ] Forward evidence 达到配置要求
- [ ] 数据库已备份
- [ ] 实盘独立凭证、确认短语和小风险预算已复核
- [ ] LIVE_CAPPED 首单由操作员全程监控

## 17. 最终运行状态

系统处于“最终正常运行”并不等于它必须持仓。正确状态可能是：

- `PASS + active + 无入场信号`：继续等待
- `HOLD`：证据或风险条件不足
- `SHADOW_ONLY`：只收集前向证据
- `UNTESTABLE`：无法可靠验证
- `REJECTED`：候选被证伪或违反规则
- `EXECUTION_ELIGIBLE`：具备短期资格，仍需最新市场和风险检查
- `HALT`：阻止新增风险，等待人工处理

只有“系统健康、数据新鲜、授权有效、证书/资格有效、风险 PASS、执行链路通过且当前信号满足入场条件”同时成立时，系统才应新增风险。

**COSMOS autonomous quantitative research architecture is engineering-complete; profitability remains an empirical forward-evidence question.**
