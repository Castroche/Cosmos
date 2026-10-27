# 多厂商 AI 策略研究架构

状态：统一网关、八类供应商适配器、模型发现、结构化输出、故障切换、熔断、设置页健康状态和 AI 研究候选链路已实现。2026-07-19 已使用本机 DeepSeek 凭证完成真实模型目录、结构化输出和 StrategySpec 候选落库验收；其他厂商在获得各自凭证前仅完成离线协议契约测试。

## 目标

Research AI 只负责提出满足 `StrategySpec` 约束的离线研究假设，不创建、激活或执行实时交易计划。所有输出都必须经过本地 Pydantic 校验、不可变数据集回测、压力测试和 Champion/Challenger 比较；研究结果不会自动交接到 Trading Intelligence v2。

系统不把“最新”实现为永久写死的模型名。每个供应商包含：

1. 一个经过验证的稳定默认模型；
2. 通过供应商模型目录 API 动态发现当前可用模型；
3. 结构化输出、上下文、推理和可用性能力探测；
4. 管理员显式批准模型切换，禁止静默升级改变策略行为；
5. 带供应商、模型、请求模板版本和输出指纹的完整审计记录。

## 首批供应商

| 供应商 | 原生接口 | 模型发现 | 结构化输出策略 | 2026-07-19 基线 |
|---|---|---|---|---|
| OpenAI | Responses API | `/v1/models` | JSON Schema Structured Outputs | GPT-5.6 Terra；Sol 用于高价值复核 |
| Anthropic | Messages API | `/v1/models` | `output_config.format` JSON Schema | Claude 4.6 系列，按模型能力字段选择 |
| Google Gemini | Interactions API | Models API | `response_format` + JSON Schema | Gemini 3 系列；稳定版优先于 Preview |
| xAI | Chat Completions / xAI SDK | `/v1/models` | `response_format=json_schema` | Grok 4.5 系列 |
| DeepSeek | OpenAI 或 Anthropic 兼容接口 | 配置目录 + 健康探测 | JSON mode 后本地严格校验 | `deepseek-flash`（DeepSeek V4.1 Flash） |
| Mistral | Chat Completions | `/v1/models` | Custom Structured Outputs | `mistral-*-latest` 能力探测后解析到实际模型 |
| Alibaba Qwen | OpenAI-compatible / Responses | 模型目录 | JSON mode 后本地严格校验 | Qwen 3.7 系列；按区域端点隔离 |
| Custom | OpenAI-compatible Chat Completions | 可选 `/v1/models` | JSON Schema 或 JSON mode | 用于 Azure、自托管和其他兼容服务 |

模型基线只作为首次配置建议。运行时以凭证实际可访问的目录和能力为准，预览模型默认不进入自动路由。

## 分层设计

```text
AI strategy proposal request
        |
        v
Prompt builder (versioned, no secrets, bounded market summary)
        |
        v
AI Router ---------------------------------------------------+
  | capability + policy + health + budget                    |
  +--> OpenAI adapter      +--> Anthropic adapter             |
  +--> Gemini adapter      +--> OpenAI-compatible adapters    |
        |                                                   fallback
        v                                                     |
Normalized AIResponse <---------------------------------------+
        |
        v
JSON decode -> StrategySpec validation -> semantic guardrails
        |
        v
StrategyCandidate(source=ai, proposed, never executable)
        |
        v
immutable dataset -> walk-forward/stress -> Challenger -> manual promotion
```

### 统一请求

- `task`: 固定为 `strategy_proposal`，首期不开放任意聊天或工具调用。
- `market_context`: 只包含确定窗口的统计摘要、市场状态和数据集 ID，不发送交易凭证、账户标识或完整订单历史。
- `schema`: 从 `StrategySpec` 生成，供应商适配器只能做语法降级，不能放宽本地约束。
- `request_id`、`prompt_version`、`schema_version`、`timeout_ms`、`max_output_tokens`。
- `preferred_provider` 可选；默认由路由器按策略选择。

### 统一响应

- 供应商、请求模型、供应商实际返回模型和端点类型；
- 供应商请求 ID、耗时、重试次数、输入/输出 token；
- 缓存命中/未命中输入、隐藏推理、可见输出、实际推理档位、计价时段与人民币估算费用；
- 原始响应的 SHA-256 指纹，不默认持久化完整敏感提示；
- 解析后的 `StrategySpec`、拒绝原因和供应商错误分类；
- `fallback_chain`，能够还原每次故障切换，而不是把多模型结果混成一个答案。

## 路由策略

1. 先按结构化输出能力、地区端点、模型稳定性和管理员允许列表过滤。
2. 策略生成使用质量路由；批量参数候选使用成本路由；Champion 复核可采用双模型独立评审。
3. 仅对超时、429 和 5xx 做带抖动的有限重试；认证、权限、Schema 错误不跨供应商盲目重试。
4. 熔断器按供应商隔离。连续 3 次失败后进入 60 秒冷却，冷却结束由下一次有效研究事件自动半开探测，成功后清零连续失败；`GET /api/ai/health` 返回最近错误、失败/成功时间、冷却截止时间和半开次数。熔断仅降级 AI 研究生成，不得停掉本地审核、交易执行、保护、退出或对账。
   自动 Agent 结构化决策另有滚动质量闸门：最近 20 次中样本不少于 5 次、成功率低于 80% 且最后一次仍失败时暂停该厂商 15 分钟；其他合格厂商仍可接管，冷却后允许一次恢复探测。人工连接测试和显式手动研究不被自动质量闸门阻断。
5. Fallback 每次重新使用相同的版本化输入和 Schema；禁止把上一供应商的自由文本喂给下一家。
6. 多模型意见不直接投票决定交易。每个输出形成独立候选，使用同一不可变数据集公平回测。

## 安全边界

- API Key 仅存操作系统凭证库，按供应商和端点隔离；SQLite、日志、浏览器响应和异常消息不得包含密钥。
- 自定义 `base_url` 默认只允许 HTTPS；私网/回环地址需要管理员显式开启，阻止 SSRF 和云元数据访问。
- 禁止任意模型工具调用、代码执行、网页访问和直接调用交易 API。
- 输出必须先通过 JSON 解码、`StrategySpec` 字段范围、交叉参数、来源和指纹校验。
- AI 候选保持 `source=ai`；研究代码没有实时执行入口。晋升 Champion 只更新离线证据，不能创建 Thesis、TradePlan、RiskAllocation 或订单。
- 预算按供应商设置单次 token、每分钟请求、每日费用软限额和硬限额；硬限额触发 fail closed。
- 日志只保留指纹、计量和截断后的错误类别；供应商原始正文为可选的加密调试数据并设短期 TTL。

## 对外 API

- `GET /api/ai/settings`：返回配置、凭证布尔状态、健康和模型目录，不返回密钥。
- `GET /api/ai/usage?days=30&provider=deepseek`：返回本地记录期内的请求、缓存、隐藏推理和估算费用汇总，以及最近请求明细。
- `PUT /api/ai/settings`：保存全部非敏感配置和可选单厂商新密钥；已有密钥永不回显。
- `POST /api/ai/providers/{provider}/test`：最小结构化输出测试，不产生策略候选。
- `POST /api/ai/providers/{provider}/discover-models`：用当前凭证刷新并持久化模型目录。
- `POST /api/research/ai-candidates`：同步创建受超时约束的 AI 研究候选；完成后只登记 `proposed` 候选。显式选择厂商时是人工调用；自动模式只使用已启用厂商。

## 已实现与后续验收

### 已实现：基础设施与真实研究闭环

- Provider 协议、统一错误、响应计量、模型能力和路由策略模型；
- SecretStore 增加 AI 凭证作用域；配置响应只暴露布尔状态；
- HTTP 客户端统一代理、连接池、总超时、重试上限和 TLS 校验；
- Provider 合约测试覆盖原生/兼容请求形态、鉴权、错误脱敏、固定厂商域名限制和故障切换。

同时已实现 OpenAI Responses、Anthropic Messages、Gemini Interactions 原生适配器，以及 xAI、DeepSeek、Mistral、Qwen 和自定义兼容适配器；设置页支持模型目录、完整连接验收和运行状态。策略生成中心可创建 AI 研究候选，输出只允许进入现有候选库与人工晋升链路。

### 已实现：DeepSeek 成本与缓存优化

- DeepSeek 请求显式发送 `thinking` 与 `reasoning_effort`；默认保持思考开启和 `high`，不再依赖供应商默认推断。连接 smoke test 关闭思考。
- Agent 的固定规则、规范化工具目录和输出 Schema 位于动态运行状态之前；工具和 JSON 字段使用确定性排序，以扩大可复用前缀。
- 动态上下文移除 run/goal 元数据和重复 observation 外键，但保留非空行情、持仓、风险、授权、Episode 与工具证据。
- Agent 决策输出限制理由、证据和引用规模，生成预算由 1800 降至 1200 token；无效结构仍只允许一次有界重试。
- 每次成功调用持久化缓存命中、缓存未命中、隐藏推理、可见输出、Prompt/Schema 版本和费用；不持久化 `reasoning_content` 正文。
- 人民币估价按 DeepSeek 官方生效日版本化。2026-08-17 起区分北京时间高峰（09:00–12:00、14:00–18:00）和空闲时段；时效性交易不延迟，批量研究可根据看板另行安排到空闲时段。

### 后续增强

- 将已实现的可审计成本计量接入每日硬预算与超额 fail-closed；
- 将长耗时 AI 请求改为持久化异步 Job，并加入幂等键与重启恢复；
- 供应商输出质量看板：Schema 成功率、候选通过率、漂移、费用和 P95 延迟；
- 模型升级先 shadow 评估，达到样本门槛后再人工批准替换默认模型。

验收必须包括：全套单元测试、无密钥时 fail closed、密钥不回显、每家真实 API 的模型发现与结构化输出 smoke test、故障切换演练，以及确认 AI 候选无法直接生成订单。没有真实供应商凭证时，只能完成离线合约测试，不能声称真实 AI 已可用。

## 官方依据

- OpenAI Models / Responses API: https://developers.openai.com/api/docs/models
- Anthropic Models / Messages API: https://platform.claude.com/docs/en/api/models/list
- Google Gemini Interactions API: https://ai.google.dev/gemini-api/docs/interactions-overview
- xAI Structured Outputs: https://docs.x.ai/developers/model-capabilities/text/structured-outputs
- DeepSeek API: https://api-docs.deepseek.com/
- Mistral Models / Structured Outputs: https://docs.mistral.ai/api/endpoint/models
- Alibaba Qwen Structured Output: https://help.aliyun.com/en/model-studio/qwen-structured-output
