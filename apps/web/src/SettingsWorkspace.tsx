import { useEffect, useState } from 'react'
import { commissionAgentPositionManagement, commissionProtection, getRiskState, getSettings, saveSettings, testConnection } from './api'
import { CHART_TIMEFRAMES } from './timeframes'
import type {
  ConfigurationUpdate,
  ConfigurationView,
  ConnectionTestResult,
  CredentialDraft,
  RiskControlState,
  UserConfiguration,
} from './types'

type SettingsWorkspaceProps = {
  view: ConfigurationView
  onSaved: (view: ConfigurationView) => void
}

const percent = (value: string) => String(Number(value) * 100)
const fraction = (value: string) => String(Number(value || 0) / 100)

export default function SettingsWorkspace({ view, onSaved }: SettingsWorkspaceProps) {
  const [configuration, setConfiguration] = useState<UserConfiguration>(() => structuredClone(view.configuration))
  const [credentials, setCredentials] = useState<CredentialDraft>({})
  const [livePhrase, setLivePhrase] = useState('')
  const [tab, setTab] = useState<'connection' | 'risk' | 'strategy'>('connection')
  const [busy, setBusy] = useState<'save' | 'test' | 'commission' | 'agent-commission' | null>(null)
  const [result, setResult] = useState<ConnectionTestResult | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [messageTone, setMessageTone] = useState<'success' | 'error'>('success')
  const [riskState, setRiskState] = useState<RiskControlState | null>(null)

  useEffect(() => {
    void getRiskState().then(risk => {
      setRiskState(risk)
    }).catch(() => {
      setRiskState(null)
    })
  }, [])

  const invalidateConnectionResult = () => {
    setResult(null)
    setMessage(null)
  }
  const update = <K extends keyof UserConfiguration>(key: K, value: UserConfiguration[K]) => {
    invalidateConnectionResult()
    setConfiguration(current => ({ ...current, [key]: value }))
  }
  const updateCredentialScope = (
    exchange: UserConfiguration['exchange_id'],
    mode: UserConfiguration['trading_mode'],
  ) => {
    invalidateConnectionResult()
    setCredentials({})
    setConfiguration(current => ({
      ...current,
      exchange_id: exchange,
      trading_mode: mode,
      live_trading_enabled: mode === 'live' ? current.live_trading_enabled : false,
    }))
  }
  const updateMarketType = (marketType: UserConfiguration['market_type']) => {
    invalidateConnectionResult()
    setConfiguration(current => ({
      ...current,
      market_type: marketType,
      automatic_execution_enabled: false,
    }))
  }
  const updateRisk = <K extends keyof UserConfiguration['risk']>(
    key: K,
    value: UserConfiguration['risk'][K],
  ) => {
    setConfiguration(current => ({ ...current, risk: { ...current.risk, [key]: value } }))
  }
  const updateStrategy = <K extends keyof UserConfiguration['strategy']>(
    key: K,
    value: UserConfiguration['strategy'][K],
  ) => {
    setConfiguration(current => ({ ...current, strategy: { ...current.strategy, [key]: value } }))
  }
  const updateValidation = <K extends keyof UserConfiguration['strategy']['validation']>(
    key: K,
    value: UserConfiguration['strategy']['validation'][K],
  ) => {
    setConfiguration(current => ({
      ...current,
      strategy: {
        ...current.strategy,
        validation: { ...current.strategy.validation, [key]: value },
      },
    }))
  }
  const updateCredential = <K extends keyof CredentialDraft>(key: K, value: CredentialDraft[K]) => {
    invalidateConnectionResult()
    setCredentials(current => ({ ...current, [key]: value }))
  }

  const payload = (): ConfigurationUpdate => ({
    configuration: {
      ...configuration,
      automatic_execution_enabled: false,
      live_trading_enabled: false,
    },
    credentials,
    live_unlock_phrase: livePhrase || undefined,
  })

  const test = async () => {
    setBusy('test')
    setMessage(null)
    try {
      const next = await testConnection(payload())
      setResult(next)
      setRiskState(await getRiskState())
    } catch (error) {
      setMessageTone('error')
      setResult(null)
      setMessage(error instanceof Error ? error.message : '连接测试失败')
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    setBusy('save')
    setMessage(null)
    try {
      const next = await saveSettings(payload())
      setCredentials({})
      setLivePhrase('')
      setResult(null)
      onSaved(next)
      setMessageTone('success')
      setMessage('设置已保存。真实资金权限仍需在交易工作台签发 HumanLiveGrant。')
    } catch (error) {
      setMessageTone('error')
      setMessage(error instanceof Error ? error.message : '保存设置失败')
    } finally {
      setBusy(null)
    }
  }

  const commission = async () => {
    if (!window.confirm('这会在测试网提交一个远离市价的最小保护订单，验证止盈止损后立即撤销。确认继续？')) return
    setBusy('commission')
    setMessage(null)
    try {
      const result = await commissionProtection(configuration.default_symbol)
      const next = await getSettings()
      onSaved(next)
      setMessageTone(result.protection_verified && result.cancellation_confirmed ? 'success' : 'error')
      setMessage(result.message)
    } catch (error) {
      setMessageTone('error')
      setMessage(error instanceof Error ? error.message : '测试网保护单验收失败')
    } finally {
      setBusy(null)
    }
  }

  const commissionAgent = async () => {
    if (!window.confirm('这会在 OKX Demo 真实成交一笔最小测试仓位，然后部分减仓、重整保护单并全部平仓。失败时系统会停机并要求人工核对。确认继续？')) return
    setBusy('agent-commission')
    setMessage(null)
    try {
      const result = await commissionAgentPositionManagement(configuration.default_symbol)
      const next = await getSettings()
      onSaved(next)
      setMessageTone(result.agent_reduction_verified && result.agent_exit_verified && result.position_flat_confirmed ? 'success' : 'error')
      setMessage(result.message)
    } catch (error) {
      setMessageTone('error')
      setMessage(error instanceof Error ? error.message : '确定性仓位管理验收失败')
    } finally {
      setBusy(null)
    }
  }

  const credentialLabel = configuration.trading_mode === 'paper' ? '可选' : '必填'
  const credentialState = view.credential_scopes?.[`${configuration.exchange_id}:${configuration.trading_mode}`] ?? view.credentials
  const healthTone = result ? (result.ok ? 'ok' : 'error') : 'idle'
  const healthLabel = result
    ? (result.ok ? `${result.exchange.toUpperCase()} 合约连接正常` : `${result.exchange.toUpperCase()} 合约账户待处理`)
    : '等待连接测试'
  const hasUnsavedCredentials = Boolean(
    credentials.api_key || credentials.secret || credentials.passphrase || credentials.clear_existing,
  )
  const commissioningConfigurationIsSaved =
    configuration.exchange_id === view.configuration.exchange_id
    && configuration.trading_mode === view.configuration.trading_mode
    && configuration.market_type === view.configuration.market_type
    && configuration.margin_mode === view.configuration.margin_mode
    && configuration.contract_leverage === view.configuration.contract_leverage
    && configuration.default_symbol === view.configuration.default_symbol
    && configuration.network_proxy_url === view.configuration.network_proxy_url
  const canCommission = Boolean(
    result?.ok && commissioningConfigurationIsSaved && !hasUnsavedCredentials,
  )
  const commissioningPassed = Boolean(
    configuration.trading_mode === 'sandbox'
    && commissioningConfigurationIsSaved
    && view.execution_ready
    && view.commissioning
    && view.commissioning.exchange === configuration.exchange_id
    && view.commissioning.market_type === configuration.market_type,
  )
  const agentCommissioningPassed = Boolean(
    commissioningPassed
    && view.commissioning?.agent_reduction_verified
    && view.commissioning.agent_exit_verified
    && view.commissioning.position_flat_confirmed,
  )
  const canCommissionAgent = Boolean(
    commissioningPassed && riskState && !riskState.halted,
  )
  const canSave = configuration.trading_mode === 'paper'
    || Boolean(result?.ok)
    || (commissioningConfigurationIsSaved && !hasUnsavedCredentials)

  return (
    <section className="settings-workspace">
      <header className="workspace-heading">
        <div>
          <span className="eyebrow">Personal runtime</span>
          <h1>系统设置</h1>
          <p>配置真实行情、交易账户、风险边界和策略默认值。密钥只写入本机系统凭证库。</p>
        </div>
        <div className="settings-health">
          <i className={healthTone} />
          <span>{healthLabel}</span>
        </div>
      </header>

      <div className="settings-layout">
        <nav className="settings-tabs" aria-label="设置分类">
          <button type="button" className={tab === 'connection' ? 'active' : ''} onClick={() => setTab('connection')}>
            <strong>交易连接</strong><span>交易所、市场和 API 凭证</span>
          </button>
          <button type="button" className={tab === 'risk' ? 'active' : ''} onClick={() => setTab('risk')}>
            <strong>风险边界</strong><span>仓位、回撤和杠杆上限</span>
          </button>
          <button type="button" className={tab === 'strategy' ? 'active' : ''} onClick={() => setTab('strategy')}>
            <strong>策略默认值</strong><span>品种池、周期和生成偏好</span>
          </button>
        </nav>

        <form className="settings-form" onSubmit={event => event.preventDefault()}>
          {tab === 'connection' && (
            <div className="form-section">
              <div className="section-intro"><h2>合约交易连接</h2><p>系统以 OKX USDT 永续为主路径；模拟盘也读取真实合约行情，真实下单仍默认锁定。</p></div>
              <div className="field-grid three">
                <label><span>交易所</span><select value={configuration.exchange_id} onChange={event => updateCredentialScope(event.target.value as 'okx' | 'binance', configuration.trading_mode)}><option value="okx">OKX</option><option value="binance">Binance</option></select></label>
                <label><span>运行模式</span><select value={configuration.trading_mode} onChange={event => updateCredentialScope(configuration.exchange_id, event.target.value as UserConfiguration['trading_mode'])}><option value="paper">真实行情模拟盘</option><option value="sandbox">交易所测试网</option><option value="live">真实交易</option></select></label>
                <label><span>市场类型</span><select value={configuration.market_type} onChange={event => updateMarketType(event.target.value as UserConfiguration['market_type'])}><option value="swap">USDT 永续（推荐）</option><option value="spot">现货兼容</option></select></label>
                {configuration.market_type === 'swap' && <label><span>保证金模式</span><select value={configuration.margin_mode} onChange={event => update('margin_mode', event.target.value as UserConfiguration['margin_mode'])}><option value="cross">全仓</option><option value="isolated">逐仓</option></select></label>}
                {configuration.market_type === 'swap' && <label><span>执行杠杆</span><input type="number" min="1" max="20" step="1" value={configuration.contract_leverage} onChange={event => update('contract_leverage', event.target.value)} /><small>下单前同步到交易所，不得高于风控最大杠杆</small></label>}
                <label><span>默认品种</span><input value={configuration.default_symbol} onChange={event => update('default_symbol', event.target.value.toUpperCase())} placeholder="BTC/USDT" /></label>
                <label><span>默认周期</span><select value={configuration.default_timeframe} onChange={event => update('default_timeframe', event.target.value)}>{CHART_TIMEFRAMES.map(item => <option key={item}>{item}</option>)}</select></label>
                <label><span>模拟本金 USDT</span><input type="number" min="1" value={configuration.paper_equity} onChange={event => update('paper_equity', event.target.value)} /></label>
                <label><span>手续费 bps</span><input type="number" min="0" value={configuration.fee_bps} onChange={event => update('fee_bps', event.target.value)} /></label>
                <label><span>预估滑点 bps</span><input type="number" min="0" value={configuration.slippage_bps} onChange={event => update('slippage_bps', event.target.value)} /></label>
              </div>

              {configuration.market_type === 'swap' && configuration.exchange_id !== 'okx' && <div className="risk-note"><strong>执行受限</strong><p>Binance 永续原子保护单尚未完成端到端验收；当前只允许行情和账户检查，开仓保持锁定。请使用 OKX 永续完成合约验收。</p></div>}

              <label className="wide-field"><span>网络代理（可选）</span><input value={configuration.network_proxy_url} onChange={event => update('network_proxy_url', event.target.value)} placeholder="http://127.0.0.1:7890" /><small>REST 与 WebSocket 共用；能直连交易所时留空。请勿在地址中嵌入用户名或密码。</small></label>

              <div className="credential-block">
                <div className="credential-title"><div><h3>API 凭证</h3><p>{credentialLabel} · 按交易所与运行模式隔离，已有凭证不会回传到浏览器</p></div><span className={credentialState.api_key_configured ? 'configured' : ''}>{credentialState.api_key_configured ? '已配置' : '未配置'}</span></div>
                <div className="field-grid">
                  <label><span>API Key</span><input type="password" autoComplete="username" value={credentials.api_key ?? ''} onChange={event => updateCredential('api_key', event.target.value)} placeholder={credentialState.api_key_configured ? '已保存在系统凭证库；留空则不修改' : '输入交易所 API Key'} /></label>
                  <label><span>Secret Key</span><input type="password" autoComplete="new-password" value={credentials.secret ?? ''} onChange={event => updateCredential('secret', event.target.value)} placeholder={credentialState.secret_configured ? '已安全保存；留空则不修改' : '输入 Secret Key'} /></label>
                  {configuration.exchange_id === 'okx' && <label><span>API Passphrase</span><input type="password" autoComplete="new-password" value={credentials.passphrase ?? ''} onChange={event => updateCredential('passphrase', event.target.value)} placeholder={credentialState.passphrase_configured ? '已安全保存；留空则不修改' : '输入 OKX Passphrase'} /></label>}
                </div>
                <label className="check-line"><input type="checkbox" checked={credentials.clear_existing ?? false} onChange={event => updateCredential('clear_existing', event.target.checked)} />保存时先清除该交易所与当前模式的已有凭证</label>
              </div>

              {result && (
                <div className={`connection-readiness ${result.ok ? 'success' : 'error'}`}>
                  <div className="readiness-heading">
                    <div><strong>{result.ok ? '合约连接已就绪' : '合约连接尚未就绪'}</strong><p>{result.message}</p></div>
                    <span>{result.latency_ms}ms</span>
                  </div>
                  <div className="readiness-stages" aria-label="连接检测阶段">
                    <div className={result.public_market_ok ? 'passed' : 'failed'}><i /><span>公共行情</span><strong>{result.public_market_ok ? '正常' : '失败'}</strong></div>
                    <div className={result.private_account_ok == null ? 'skipped' : result.private_account_ok ? 'passed' : 'failed'}><i /><span>私有账户</span><strong>{result.private_account_ok == null ? '无需检查' : result.private_account_ok ? '正常' : '失败'}</strong></div>
                    <div className={result.mode !== 'paper' && !result.private_account_ok ? 'pending' : result.protected_order_ok ? 'passed' : 'failed'}><i /><span>原子保护单</span><strong>{result.mode !== 'paper' && !result.private_account_ok ? '等待账户' : result.protected_order_ok ? '正常' : '不可用'}</strong></div>
                  </div>
                  <div className="readiness-market"><span>{result.symbol}</span><span>{result.last_price ?? '无报价'} USDT</span><span>{result.candle_count} 根 K 线</span></div>
                  {result.required_action && (
                    <div className="required-action">
                      <strong>下一步</strong>
                      <p>{result.required_action}</p>
                      {result.failure_code === 'okx_account_mode_spot' && (
                        <ol>
                          <li>进入 OKX 的 Demo Trading，不要切换到真实资金账户。</li>
                          <li>打开交易设置，将 Account mode 设为 Futures mode。</li>
                          <li>返回本页，保持 USDT 永续、全仓、3×，再次点击“测试连接”。</li>
                        </ol>
                      )}
                      {result.failure_code === 'okx_account_mode_spot' && <a href="https://www.okx.com/en-us/help/how-to-conduct-contract-simulation-trading-transactions" target="_blank" rel="noreferrer">查看 OKX 官方模拟合约说明 ↗</a>}
                    </div>
                  )}
                </div>
              )}

              {configuration.trading_mode === 'sandbox' && (
                <>
                  <div className="commissioning-block">
                    <div><strong>测试网原子保护单验收</strong><p>保存凭证并通过连接测试后，提交一个远离市价的最小订单；系统会查询确认原生止盈止损并立即撤销。</p></div>
                    <span className={commissioningPassed ? 'ready' : ''}>{commissioningPassed ? '已通过' : '未验收'}</span>
                    <button type="button" className="button ghost" onClick={commission} disabled={busy !== null || !canCommission} title={!canCommission ? '请先保存当前配置和凭证，再通过连接测试' : undefined}>{busy === 'commission' ? '正在验收…' : '开始保护单验收'}</button>
                    {commissioningPassed && view.commissioning && <small>{view.commissioning.exchange.toUpperCase()} · {view.commissioning.market_type === 'spot' ? '现货' : 'USDT 永续'} · {new Date(view.commissioning.tested_at).toLocaleString()}</small>}
                  </div>
                  {configuration.exchange_id === 'okx' && configuration.market_type === 'swap' && <div className="commissioning-block">
                    <div><strong>v2 确定性仓位管理验收</strong><p>真实成交测试仓位，验证部分减仓后的保护单缩量、最终平仓、Algo 单清理与空仓回查。</p></div>
                    <span className={agentCommissioningPassed ? 'ready' : ''}>{agentCommissioningPassed ? '已通过' : '未验收'}</span>
                    <button type="button" className="button ghost" onClick={commissionAgent} disabled={busy !== null || !canCommissionAgent} title={riskState?.halted ? '请先稳定通过对账、人工核对账户并解除全局 HALT' : undefined}>{busy === 'agent-commission' ? '正在成交并验收…' : '开始仓位管理验收'}</button>
                    <small>{riskState?.halted ? `全局 HALT：${riskState.reason}` : '只使用 OKX Demo；未通过前 v2 Position Manager 的主动减仓与平仓保持锁定。'}</small>
                  </div>}
                </>
              )}

              {configuration.trading_mode === 'live' && (
                <div className="live-grant-notice">
                  <div><strong>真实凭证仅供授权模式</strong><p>保存 Live 凭证不会解锁真实下单。只有交易工作台中未过期、双重确认且配置指纹一致的 HumanLiveGrant 才能触达 Live API。</p></div>
                  <label><span>保存凭证确认短语</span><input value={livePhrase} onChange={event => setLivePhrase(event.target.value)} placeholder="I ACCEPT LIVE TRADING RISK" /></label>
                </div>
              )}
            </div>
          )}

          {tab === 'risk' && (
            <div className="form-section">
              <div className="section-intro"><h2>合约风险边界</h2><p>按名义敞口、实际可用保证金、执行杠杆、止损到清算缓冲和单向持仓逐项检查。</p></div>
              <div className="field-grid three">
                <label><span>单笔最大风险 %</span><input type="number" step="0.1" value={percent(configuration.risk.risk_per_trade)} onChange={event => updateRisk('risk_per_trade', fraction(event.target.value))} /></label>
                <label><span>日内最大回撤 %</span><input type="number" step="0.1" value={percent(configuration.risk.max_daily_drawdown)} onChange={event => updateRisk('max_daily_drawdown', fraction(event.target.value))} /></label>
                <label><span>总敞口倍数</span><input type="number" step="0.1" value={configuration.risk.max_gross_exposure} onChange={event => updateRisk('max_gross_exposure', event.target.value)} /></label>
                <label><span>组合风险预算 %</span><input type="number" step="0.5" value={percent(configuration.risk.max_portfolio_risk)} onChange={event => updateRisk('max_portfolio_risk', fraction(event.target.value))} /></label>
                <label><span>存量仓位压力损失 %</span><input type="number" step="0.5" value={percent(configuration.risk.existing_position_stress)} onChange={event => updateRisk('existing_position_stress', fraction(event.target.value))} /></label>
                <label><span>同向敞口倍数</span><input type="number" step="0.05" value={configuration.risk.max_directional_exposure} onChange={event => updateRisk('max_directional_exposure', event.target.value)} /></label>
                <label><span>最大杠杆</span><input type="number" step="1" value={configuration.risk.max_leverage} onChange={event => updateRisk('max_leverage', event.target.value)} /></label>
                <label><span>单品种敞口倍数</span><input type="number" step="0.05" value={configuration.risk.max_symbol_exposure} onChange={event => updateRisk('max_symbol_exposure', event.target.value)} /></label>
                <label><span>最多同时持仓</span><input type="number" step="1" value={configuration.risk.max_open_positions} onChange={event => updateRisk('max_open_positions', Number(event.target.value))} /></label>
                <label><span>最低盈亏比</span><input type="number" step="0.1" value={configuration.risk.minimum_reward_risk} onChange={event => updateRisk('minimum_reward_risk', event.target.value)} /></label>
                <label><span>最大价差 bps</span><input type="number" step="1" value={configuration.risk.max_spread_bps} onChange={event => updateRisk('max_spread_bps', event.target.value)} /></label>
                <label><span>单期资金费率绝对值上限 %</span><input type="number" step="0.01" value={percent(configuration.risk.max_abs_funding_rate)} onChange={event => updateRisk('max_abs_funding_rate', fraction(event.target.value))} /><small>交易所合约模式无法取得实时费率，或绝对值超过该阈值时拒绝新开仓</small></label>
              </div>
              <div className="risk-note"><strong>Fail closed</strong><p>行情陈旧、保证金不足、计划杠杆不一致、止损过近清算区、反向持仓或敞口越界时，系统拒绝所有新开仓，只保留撤单和减仓能力。</p></div>
            </div>
          )}

          {tab === 'strategy' && (
            <div className="form-section">
              <div className="section-intro"><h2>策略默认值</h2><p>策略中心打开时自动加载这些范围，仍可为单次生成修改。</p></div>
              <div className="field-grid">
                <label><span>默认策略类型</span><select value={configuration.strategy.default_kind} onChange={event => updateStrategy('default_kind', event.target.value as UserConfiguration['strategy']['default_kind'])}><option value="trend">趋势回踩</option><option value="mean_reversion">均值回归</option><option value="breakout">区间突破</option></select></label>
                <label><span>研究历史 K 线数</span><input type="number" min="500" max="20000" step="500" value={configuration.strategy.research_bars} onChange={event => updateStrategy('research_bars', Number(event.target.value))} /><small>图表仍显示 300 根；该值只用于信号与稳健性回测</small></label>
                <label><span>最少有效交易</span><input type="number" min="10" max="10000" step="10" value={configuration.strategy.validation.minimum_trades} onChange={event => updateValidation('minimum_trades', Number(event.target.value))} /></label>
                <label><span>最低样本比例</span><input type="number" min="0.5" max="1" step="0.05" value={configuration.strategy.validation.minimum_trade_evidence_ratio} onChange={event => updateValidation('minimum_trade_evidence_ratio', event.target.value)} /><small>低于标准样本时，仍须近期窗口和成本压力同时为正</small></label>
                <label><span>近期最少交易</span><input type="number" min="5" max="1000" step="5" value={configuration.strategy.validation.minimum_recent_trades} onChange={event => updateValidation('minimum_recent_trades', Number(event.target.value))} /></label>
                <label><span>最少研究 K 线</span><input type="number" min="100" max="1000000" step="100" value={configuration.strategy.validation.minimum_sample_bars} onChange={event => updateValidation('minimum_sample_bars', Number(event.target.value))} /></label>
                <label><span>最低 Profit Factor</span><input type="number" min="1" max="5" step="0.05" value={configuration.strategy.validation.minimum_profit_factor} onChange={event => updateValidation('minimum_profit_factor', event.target.value)} /></label>
                <label><span>执行级最低胜率 %</span><input type="number" min="20" max="90" step="1" value={configuration.strategy.validation.minimum_win_rate_percent} onChange={event => updateValidation('minimum_win_rate_percent', event.target.value)} /></label>
                <label><span>最低平均盈利 / 亏损</span><input type="number" min="0.5" max="10" step="0.05" value={configuration.strategy.validation.minimum_payoff_ratio} onChange={event => updateValidation('minimum_payoff_ratio', event.target.value)} /></label>
                <label><span>最大回撤 %</span><input type="number" min="1" max="80" step="1" value={configuration.strategy.validation.maximum_drawdown_percent} onChange={event => updateValidation('maximum_drawdown_percent', event.target.value)} /></label>
                <label><span>压力手续费倍数</span><input type="number" min="1" max="5" step="0.1" value={configuration.strategy.validation.stress_fee_multiplier} onChange={event => updateValidation('stress_fee_multiplier', event.target.value)} /></label>
                <label><span>压力滑点倍数</span><input type="number" min="1" max="10" step="0.5" value={configuration.strategy.validation.stress_slippage_multiplier} onChange={event => updateValidation('stress_slippage_multiplier', event.target.value)} /></label>
                <label><span>最大预估往返成本 / R</span><input type="number" min="0.01" max="2" step="0.05" value={configuration.strategy.validation.maximum_estimated_round_trip_cost_r} onChange={event => updateValidation('maximum_estimated_round_trip_cost_r', event.target.value)} /><small>超限候选在完整审核前标记为设计不可行</small></label>
                <label><span>跨市场最低通过数</span><input type="number" min="1" max="3" step="1" value={configuration.strategy.validation.cross_market_minimum_passes} onChange={event => updateValidation('cross_market_minimum_passes', Number(event.target.value))} /></label>
                <label><span>审核合约版本</span><input type="number" min="2" max="2" step="1" value={configuration.strategy.validation.review_contract_version} disabled /><small>v1 历史记录保持只读</small></label>
                <label><span>关注品种（逗号分隔）</span><input value={configuration.strategy.watchlist.join(', ')} onChange={event => updateStrategy('watchlist', event.target.value.split(',').map(item => item.trim()).filter(Boolean))} /></label>
                <label className="switch-line"><input type="checkbox" checked={configuration.strategy.automatic_regime_selection} onChange={event => updateStrategy('automatic_regime_selection', event.target.checked)} /><span>按市场状态自动选择策略家族</span></label>
                <label className="switch-line"><input type="checkbox" checked={configuration.strategy.validation.require_higher_timeframe_confirmation} onChange={event => updateValidation('require_higher_timeframe_confirmation', event.target.checked)} /><span>Sandbox / Live 强制高周期确认</span></label>
              </div>
              <label className="wide-field"><span>默认策略描述</span><textarea rows={5} value={configuration.strategy.default_prompt} onChange={event => updateStrategy('default_prompt', event.target.value)} /></label>
              <div className="timeframe-picks"><span>研究周期</span>{CHART_TIMEFRAMES.map(item => {
                const selected = configuration.strategy.timeframes.includes(item)
                return <button type="button" key={item} aria-pressed={selected} className={selected ? 'active' : ''} onClick={() => updateStrategy('timeframes', selected && configuration.strategy.timeframes.length > 1 ? configuration.strategy.timeframes.filter(value => value !== item) : selected ? configuration.strategy.timeframes : [...configuration.strategy.timeframes, item])}>{item}</button>
              })}</div>
            </div>
          )}

          {message && <div className={`settings-result ${messageTone}`} role={messageTone === 'error' ? 'alert' : 'status'}><strong>{message}</strong></div>}
          <footer className="settings-actions"><span>设置保存在本机，不会上传到第三方服务。</span><button type="button" className="button ghost" onClick={test} disabled={busy !== null}>{busy === 'test' ? '正在连接…' : '测试连接'}</button><button type="button" className="button primary" onClick={save} disabled={busy !== null || !canSave} title={!canSave ? '交易所模式必须先通过连接测试' : undefined}>{busy === 'save' ? '正在保存…' : '保存并应用'}</button></footer>
        </form>
      </div>
    </section>
  )
}
