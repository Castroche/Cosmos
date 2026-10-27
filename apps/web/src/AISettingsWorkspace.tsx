import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  Check,
  ExternalLink,
  KeyRound,
  LockKeyhole,
  RefreshCw,
  Route,
  ShieldCheck,
} from 'lucide-react'
import {
  discoverAIModels,
  getAIHealth,
  getAISettings,
  getAIUsage,
  saveAISettings,
  testAIProvider,
} from './api'
import type {
  AIConfiguration,
  AIHealth,
  AIConfigurationView,
  AIProvider,
  AIProviderConfiguration,
  AIUsageReport,
} from './types'

const providerMeta: Record<AIProvider, {
  name: string
  code: string
  description: string
  keyUrl?: string
}> = {
  openai: { name: 'OpenAI', code: 'OA', description: 'Responses API · 原生 JSON Schema', keyUrl: 'https://platform.openai.com/api-keys' },
  anthropic: { name: 'Anthropic', code: 'AN', description: 'Messages API · Claude 模型目录', keyUrl: 'https://console.anthropic.com/settings/keys' },
  google: { name: 'Google Gemini', code: 'GG', description: 'Interactions API · 稳定模型优先', keyUrl: 'https://aistudio.google.com/app/apikey' },
  xai: { name: 'xAI', code: 'XA', description: 'Grok · Structured Outputs', keyUrl: 'https://console.x.ai/' },
  deepseek: { name: 'DeepSeek', code: 'DS', description: 'V4 · OpenAI 兼容', keyUrl: 'https://platform.deepseek.com/api_keys' },
  mistral: { name: 'Mistral AI', code: 'MI', description: '模型发现 · Structured Output', keyUrl: 'https://console.mistral.ai/api-keys/' },
  qwen: { name: 'Alibaba Qwen', code: 'QW', description: '百炼 Model Studio · 区域端点', keyUrl: 'https://bailian.console.aliyun.com/' },
  custom: { name: '自定义兼容端点', code: 'CX', description: 'OpenAI-compatible · 支持本地模型' },
}

type BusyState = 'loading' | 'saving' | 'testing' | 'discovering' | null

export default function AISettingsWorkspace() {
  const [view, setView] = useState<AIConfigurationView | null>(null)
  const [configuration, setConfiguration] = useState<AIConfiguration | null>(null)
  const [usage, setUsage] = useState<AIUsageReport | null>(null)
  const [health, setHealth] = useState<AIHealth | null>(null)
  const [selected, setSelected] = useState<AIProvider>('openai')
  const [apiKey, setApiKey] = useState('')
  const [clearExisting, setClearExisting] = useState(false)
  const [busy, setBusy] = useState<BusyState>('loading')
  const [message, setMessage] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [loadAttempt, setLoadAttempt] = useState(0)

  useEffect(() => {
    let active = true
    Promise.all([getAISettings(), getAIUsage(30), getAIHealth()])
      .then(([next, nextUsage, nextHealth]) => {
        if (!active) return
        setView(next)
        setUsage(nextUsage)
        setHealth(nextHealth)
        setConfiguration(structuredClone(next.configuration))
        const firstConfigured = next.configuration.providers.find(
          item => next.credentials[item.provider].api_key_configured,
        )
        if (firstConfigured) setSelected(firstConfigured.provider)
      })
      .catch(error => active && setMessage({
        tone: 'error',
        text: error instanceof Error ? error.message : '读取 AI 配置失败',
      }))
      .finally(() => active && setBusy(null))
    return () => { active = false }
  }, [loadAttempt])

  const current = useMemo(
    () => configuration?.providers.find(item => item.provider === selected) ?? null,
    [configuration, selected],
  )
  const enabledCount = configuration?.providers.filter(item => item.enabled).length ?? 0
  const configuredCount = view
    ? Object.values(view.credentials).filter(item => item.api_key_configured).length
    : 0

  const choose = (provider: AIProvider) => {
    setSelected(provider)
    setApiKey('')
    setClearExisting(false)
    setMessage(null)
  }

  const updateProvider = <K extends keyof AIProviderConfiguration>(
    key: K,
    value: AIProviderConfiguration[K],
  ) => {
    setConfiguration(previous => previous ? {
      ...previous,
      providers: previous.providers.map(item => (
        item.provider === selected ? { ...item, [key]: value } : item
      )),
    } : previous)
  }

  const updateConfiguration = <K extends keyof AIConfiguration>(
    key: K,
    value: AIConfiguration[K],
  ) => setConfiguration(previous => previous ? { ...previous, [key]: value } : previous)

  const persist = async () => {
    if (!configuration) throw new Error('AI 配置尚未加载')
    const next = await saveAISettings({
      configuration,
      credential: apiKey || clearExisting ? {
        provider: selected,
        api_key: apiKey || undefined,
        clear_existing: clearExisting,
      } : undefined,
    })
    setView(next)
    setConfiguration(structuredClone(next.configuration))
    setApiKey('')
    setClearExisting(false)
    return next
  }

  const save = async () => {
    setBusy('saving')
    setMessage(null)
    try {
      await persist()
      setMessage({
        tone: 'success',
        text: `${providerMeta[selected].name} 配置已保存；API Key 仅存放在本机系统凭证库。`,
      })
    } catch (error) {
      setMessage({ tone: 'error', text: error instanceof Error ? error.message : '保存 AI 配置失败' })
    } finally {
      setBusy(null)
    }
  }

  const test = async () => {
    setBusy('testing')
    setMessage(null)
    try {
      if (apiKey || clearExisting) await persist()
      const result = await testAIProvider(selected)
      const [next, nextUsage, nextHealth] = await Promise.all([
        getAISettings(),
        getAIUsage(30),
        getAIHealth(),
      ])
      setView(next)
      setUsage(nextUsage)
      setHealth(nextHealth)
      setConfiguration(structuredClone(next.configuration))
      setMessage({
        tone: result.ok ? 'success' : 'error',
        text: `${result.message} · ${result.latency_ms} ms`,
      })
    } catch (error) {
      setMessage({ tone: 'error', text: error instanceof Error ? error.message : 'AI 连接测试失败' })
    } finally {
      setBusy(null)
    }
  }

  const discover = async () => {
    setBusy('discovering')
    setMessage(null)
    try {
      const models = await discoverAIModels(selected)
      const next = await getAISettings()
      next.runtime[selected].models = models
      next.runtime[selected].model_count = models.length
      setView(next)
      setMessage({ tone: 'success', text: `已发现 ${models.length} 个可访问模型。` })
    } catch (error) {
      setMessage({ tone: 'error', text: error instanceof Error ? error.message : '读取模型目录失败' })
    } finally {
      setBusy(null)
    }
  }

  if (!configuration || !current || !view) {
    return (
      <div className="ai-settings-loading" role={message ? 'alert' : 'status'}>
        {busy === 'loading' ? <span className="ai-orbit" /> : <Activity aria-hidden="true" />}
        <span>{message?.text ?? '正在读取 AI 服务配置…'}</span>
        {busy !== 'loading' && <button className="button primary" onClick={() => { setBusy('loading'); setMessage(null); setLoadAttempt(value => value + 1) }}><RefreshCw />重新读取</button>}
      </div>
    )
  }

  const credentialState = view.credentials[selected]
  const runtime = view.runtime[selected]
  const meta = providerMeta[selected]
  const modelOptions = runtime.models.filter(model => (
    current.allow_preview_models || !/(preview|experimental|exp)/i.test(model)
  ))
  const circuit = health?.providers[selected]
  const decisionQuality = circuit?.agent_decision_quality

  return (
    <section className="ai-settings-workspace">
      <header className="workspace-heading ai-heading">
        <div>
          <span className="eyebrow">Local AI gateway</span>
          <h1>AI 服务</h1>
          <p>统一管理研究模型、连通性与路由。模型只能创建隔离候选，不能直接激活策略或提交订单。</p>
        </div>
        <div className="ai-health-summary" aria-label="AI 配置状态">
          <div><strong>{configuredCount}</strong><span>凭证已配置</span></div>
          <div><strong>{enabledCount}</strong><span>已加入路由</span></div>
          <div><strong>0</strong><span>审核 / 交易 LLM</span></div>
          <i className={enabledCount ? 'ready' : ''} />
        </div>
      </header>

      {circuit && circuit.status !== 'ready' && <div className="ai-circuit-banner" role="status">
        <Activity /><div><strong>研究 AI {circuit.status === 'circuit_open' ? '正在熔断冷却' : '连接降级'}</strong><span>{circuit.last_error_message || '连续请求失败，网关会在冷却后自动半开探测。'} 审核与交易热路径均为 0 LLM，仍会持续运行。</span></div><code>{circuit.consecutive_failures} 次连续失败</code>
      </div>}

      {decisionQuality && <div className={`ai-quality-banner ${decisionQuality.automatic_allowed ? 'ready' : 'blocked'}`} role="status">
        <ShieldCheck aria-hidden="true" />
        <div>
          <strong>{decisionQuality.automatic_allowed ? '自动研究路由可用' : '自动研究路由已被质量闸门暂停'}</strong>
          <span>{decisionQuality.reason}；审核与交易继续使用确定性程序。</span>
        </div>
        <code>{decisionQuality.success_rate == null ? '待积累样本' : `${(decisionQuality.success_rate * 100).toFixed(1)}% · ${decisionQuality.request_count} 次`}</code>
      </div>}

      {usage && <div className="ai-usage-summary" aria-label="近 30 天 AI 用量">
        <div><span>请求 / 失败</span><strong>{usage.summary.request_count.toLocaleString()} / {usage.summary.failed_request_count.toLocaleString()}</strong></div>
        <div><span>Token</span><strong>{usage.summary.total_tokens.toLocaleString()}</strong></div>
        <div><span>缓存命中</span><strong>{(usage.summary.cache_hit_rate * 100).toFixed(1)}%</strong></div>
        <div><span>自动结构成功率</span><strong>{decisionQuality?.success_rate == null ? '—' : `${(decisionQuality.success_rate * 100).toFixed(1)}%`}</strong></div>
        <div><span>估算费用</span><strong>¥{Number(usage.summary.estimated_cost_cny).toFixed(3)}</strong></div>
      </div>}

      <div className="ai-settings-layout">
        <aside className="ai-provider-rail" aria-label="AI 厂商">
          <div className="ai-rail-intro"><span>PROVIDERS</span><p>选择一个厂商进行配置</p></div>
          {configuration.providers.map(provider => {
            const providerState = view.credentials[provider.provider]
            const providerRuntime = view.runtime[provider.provider]
            const info = providerMeta[provider.provider]
            const stateClass = providerRuntime.status === 'ready'
              ? 'enabled'
              : providerRuntime.status === 'error'
                ? 'error'
                : provider.enabled
                  ? 'enabled'
                  : providerState.api_key_configured ? 'configured' : ''
            return (
              <button
                type="button"
                key={provider.provider}
                className={selected === provider.provider ? 'active' : ''}
                onClick={() => choose(provider.provider)}
              >
                <span className="provider-monogram">{info.code}</span>
                <span><strong>{info.name}</strong><small>{provider.model}</small></span>
                <i className={stateClass} title={providerRuntime.message} />
              </button>
            )
          })}
          <div className="ai-rail-foot">
            <LockKeyhole aria-hidden="true" />
            <span>密钥仅存本机<br />从不返回浏览器</span>
          </div>
        </aside>

        <form
          className="ai-provider-inspector"
          key={selected}
          onSubmit={event => { event.preventDefault(); void save() }}
        >
          <div className="ai-provider-title">
            <div className="provider-mark">{meta.code}</div>
            <div><span>{meta.description}</span><h2>{meta.name}</h2></div>
            <label className="ai-enable-switch">
              <input
                type="checkbox"
                checked={current.enabled}
                onChange={event => updateProvider('enabled', event.target.checked)}
              />
              <span>{current.enabled ? '已加入路由' : '加入路由'}</span>
            </label>
          </div>

          <div className={`ai-provider-runtime ${runtime.status}`}>
            <Activity aria-hidden="true" />
            <div>
              <strong>{runtime.status === 'ready' ? '运行就绪' : runtime.status === 'error' ? '验收失败' : '尚未验收'}</strong>
              <p>{runtime.message}</p>
            </div>
            <dl>
              <div><dt>延迟</dt><dd>{runtime.latency_ms == null ? '—' : `${runtime.latency_ms} ms`}</dd></div>
              <div><dt>模型</dt><dd>{runtime.model_count || '—'}</dd></div>
              <div><dt>当前模型</dt><dd>{runtime.selected_model_available == null ? '未确认' : runtime.selected_model_available ? '可用' : '未发现'}</dd></div>
            </dl>
          </div>

          <div className="ai-routing-bar">
            <div><Route aria-hidden="true" /><span>路由策略</span></div>
            <label><span>模式</span><select value={configuration.routing_mode} onChange={event => updateConfiguration('routing_mode', event.target.value as AIConfiguration['routing_mode'])}><option value="failover">故障切换</option><option value="manual">仅手动选择</option><option value="consensus">双模型独立复核</option></select></label>
            <label><span>首选厂商</span><select value={configuration.primary_provider} onChange={event => updateConfiguration('primary_provider', event.target.value as AIProvider)}>{configuration.providers.map(item => <option key={item.provider} value={item.provider}>{providerMeta[item.provider].name}</option>)}</select></label>
          </div>

          <div className="ai-form-section">
            <div className="ai-section-label"><span>01</span><div><h3>模型端点</h3><p>先发现账户可用模型，再固定一个经过验收的模型 ID。</p></div></div>
            <div className="field-grid">
              <label>
                <span>模型 ID</span>
                <input list={`models-${selected}`} value={current.model} onChange={event => updateProvider('model', event.target.value)} spellCheck={false} />
                <datalist id={`models-${selected}`}>{modelOptions.map(model => <option key={model} value={model} />)}</datalist>
                <small>保存的模型不会静默升级</small>
              </label>
              <label><span>API Base URL</span><input value={current.base_url} onChange={event => updateProvider('base_url', event.target.value)} spellCheck={false} /><small>固定厂商强制官方域名；本地兼容端点允许 localhost</small></label>
              <label><span>路由优先级</span><input type="number" min="1" max="999" value={current.priority} onChange={event => updateProvider('priority', Number(event.target.value))} /><small>数值越小越优先</small></label>
              <label><span>请求超时（秒）</span><input type="number" min="5" max="300" value={current.timeout_seconds} onChange={event => updateProvider('timeout_seconds', Number(event.target.value))} /></label>
            </div>
            <div className="ai-model-actions">
              <label className="check-line ai-preview"><input type="checkbox" checked={current.allow_preview_models} onChange={event => updateProvider('allow_preview_models', event.target.checked)} /><span>允许 Preview / Experimental 模型进入人工选择列表</span></label>
              <button type="button" className="button" disabled={busy !== null || !credentialState.api_key_configured} onClick={() => void discover()}><RefreshCw aria-hidden="true" />{busy === 'discovering' ? '读取中…' : '刷新模型目录'}</button>
            </div>
            {selected === 'deepseek' && <div className="field-grid ai-reasoning-controls">
              <label>
                <span>思考模式</span>
                <select value={current.thinking_enabled ? 'enabled' : 'disabled'} onChange={event => updateProvider('thinking_enabled', event.target.value === 'enabled')}>
                  <option value="enabled">开启（保留配置）</option>
                  <option value="disabled">关闭</option>
                </select>
                <small>自动控制与 StrategySpec 请求会为 JSON 可靠性强制关闭；此值不再影响审核或交易。</small>
              </label>
              <label>
                <span>推理强度</span>
                <select disabled={!current.thinking_enabled} value={current.reasoning_effort} onChange={event => updateProvider('reasoning_effort', event.target.value as AIProviderConfiguration['reasoning_effort'])}>
                  <option value="high">High（推荐）</option>
                  <option value="max">Max</option>
                </select>
                <small>仅供未来非控制型深度研究使用；当前自动链路不依赖该值。</small>
              </label>
            </div>}
          </div>

          <div className="ai-form-section credential-zone">
            <div className="ai-section-label"><span>02</span><div><h3>访问凭证</h3><p>已有凭证不会回显；留空保存不会修改。</p></div><strong className={credentialState.api_key_configured ? 'configured' : ''}>{credentialState.api_key_configured ? <><Check /> 已配置</> : '未配置'}</strong></div>
            <label className="ai-key-field"><span><KeyRound aria-hidden="true" />API Key</span><input type="password" autoComplete="new-password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder={credentialState.api_key_configured ? '已保存在系统凭证库；输入新值可替换' : `输入 ${meta.name} API Key`} /></label>
            <div className="ai-credential-actions">
              <label className="check-line"><input type="checkbox" checked={clearExisting} onChange={event => setClearExisting(event.target.checked)} />保存时先清除该厂商已有密钥</label>
              {meta.keyUrl && <a href={meta.keyUrl} target="_blank" rel="noreferrer">前往官方控制台获取 Key <ExternalLink aria-hidden="true" /></a>}
            </div>
          </div>

          <div className="ai-safety-line"><ShieldCheck aria-hidden="true" /><div><strong>离线研究与实时决策严格隔离</strong><p>Research AI 只生成和验证离线候选，不会自动交接到实时交易；v2 实时决策只消费 MarketState、Quant、Portfolio 和既有 Thesis，并始终经过确定性硬风控。</p></div></div>

          {message && <div className={`ai-settings-message ${message.tone}`} role="status"><span>{message.text}</span></div>}

          <footer className="ai-settings-actions">
            <span>{view.updated_at ? `上次保存 ${new Date(view.updated_at).toLocaleString()}` : '尚未保存 AI 配置'}</span>
            <div>
              <button type="button" className="button" disabled={busy !== null || (!credentialState.api_key_configured && !apiKey)} onClick={() => void test()}>{busy === 'testing' ? '正在验收…' : '测试连接'}</button>
              <button type="submit" className="button primary" disabled={busy !== null}>{busy === 'saving' ? '正在安全保存…' : '保存 AI 配置'}</button>
            </div>
          </footer>
        </form>
      </div>
    </section>
  )
}
