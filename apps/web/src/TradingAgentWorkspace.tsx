import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  Archive,
  BrainCircuit,
  ChevronRight,
  CirclePause,
  Clock3,
  Play,
  RadioTower,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import {
  archiveAgentGoal,
  getAgentForwardPerformance,
  getAgentOverview,
  getAgentReadiness,
  getAgentRunTrace,
  getAgentState,
  launchAgent,
  openAgentStream,
  previewAgentLaunch,
  restoreAgentGoal,
  runAgentOnce,
  setAgentGoalStatus,
} from './api'
import type {
  AgentAutonomyMode,
  AgentControlState,
  AgentForwardPerformance,
  AgentGoalOverview,
  AgentLaunchPreview,
  AgentLaunchRequest,
  AgentReadiness,
  AgentRunTrace,
  AgentRuntimeOverview,
  ConfigurationView,
} from './types'

type AgentTab = 'overview' | 'activity' | 'episodes' | 'performance' | 'goals'

function localTime(value?: string | null) {
  if (!value) return '尚无记录'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function modeForPlatform(mode: ConfigurationView['configuration']['trading_mode']): AgentAutonomyMode {
  if (mode === 'live') return 'live_capped'
  return mode
}

function statusLabel(status: AgentGoalOverview['runtime_status']) {
  return {
    running: '正在推理', waiting: '持续感知', paused: '已暂停', blocked: '风险阻塞', offline: '状态失联',
  }[status]
}

export default function TradingAgentWorkspace({ settings }: { settings: ConfigurationView }) {
  const [overview, setOverview] = useState<AgentRuntimeOverview | null>(null)
  const [state, setState] = useState<AgentControlState | null>(null)
  const [readiness, setReadiness] = useState<AgentReadiness | null>(null)
  const [performance, setPerformance] = useState<AgentForwardPerformance | null>(null)
  const [selectedGoalId, setSelectedGoalId] = useState('')
  const [tab, setTab] = useState<AgentTab>('overview')
  const [streamState, setStreamState] = useState<'connecting' | 'connected' | 'fallback'>('connecting')
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [trace, setTrace] = useState<AgentRunTrace | null>(null)
  const [preview, setPreview] = useState<AgentLaunchPreview | null>(null)
  const [symbols, setSymbols] = useState(() => settings.configuration.strategy.watchlist.slice(0, 3))
  const [timeframes, setTimeframes] = useState(() => {
    const preferred = settings.configuration.strategy.timeframes.filter(item => ['1h', '4h'].includes(item))
    return preferred.length ? preferred : settings.configuration.strategy.timeframes.slice(0, 1)
  })
  const executionMode = modeForPlatform(settings.configuration.trading_mode)

  const refreshDetails = useCallback(async () => {
    try {
      const [nextOverview, nextState, nextReadiness, nextPerformance] = await Promise.all([
        getAgentOverview(), getAgentState(), getAgentReadiness(), getAgentForwardPerformance(),
      ])
      setOverview(nextOverview)
      setState(nextState)
      setReadiness(nextReadiness)
      setPerformance(nextPerformance)
      setSelectedGoalId(current => current || nextOverview.goals[0]?.goal_id || '')
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : 'Agent 状态读取失败')
    }
  }, [])

  useEffect(() => {
    const kickoff = window.setTimeout(() => void refreshDetails(), 0)
    let stopped = false
    let socket: WebSocket | null = null
    let reconnect: number | undefined
    const connect = () => {
      if (stopped) return
      setStreamState('connecting')
      socket = openAgentStream(setOverview, status => {
        if (status === 'connected') setStreamState('connected')
        if (status === 'error' || status === 'disconnected') {
          setStreamState('fallback')
          if (!stopped) reconnect = window.setTimeout(connect, 4000)
        }
      })
    }
    connect()
    const detailTimer = window.setInterval(() => void refreshDetails(), 10_000)
    const fallbackTimer = window.setInterval(() => {
      void getAgentOverview().then(setOverview).catch(() => undefined)
    }, 5_000)
    return () => {
      stopped = true
      socket?.close()
      if (reconnect) window.clearTimeout(reconnect)
      window.clearTimeout(kickoff)
      window.clearInterval(detailTimer)
      window.clearInterval(fallbackTimer)
    }
  }, [refreshDetails])

  const selectedGoal = overview?.goals.find(item => item.goal_id === selectedGoalId) ?? overview?.goals[0]
  const fullGoal = state?.goals.find(item => item.id === selectedGoal?.goal_id)
  const runs = useMemo(
    () => state?.runs.filter(item => item.goal_id === selectedGoal?.goal_id) ?? [],
    [selectedGoal?.goal_id, state?.runs],
  )
  const episodes = useMemo(
    () => state?.episodes.filter(item => item.goal_id === selectedGoal?.goal_id) ?? [],
    [selectedGoal?.goal_id, state?.episodes],
  )
  const archivedGoals = state?.goals.filter(item => item.status === 'archived') ?? []

  const launchRequest = (authorize = false, fingerprint?: string): AgentLaunchRequest => ({
    profile_id: 'primary-auto-trader',
    symbols,
    timeframes,
    autonomy_mode: executionMode,
    risk_template: 'platform-default',
    objective: `持续感知 ${symbols.join('、')} 的 ${timeframes.join('、')} 周期，仅在策略验证和硬风控通过后自动交易`,
    poll_seconds: 60,
    authorize,
    preview_fingerprint: fingerprint,
  })

  const startAgent = async () => {
    if (!symbols.length || !timeframes.length) return
    setBusy('launch'); setMessage('')
    try {
      const nextPreview = await previewAgentLaunch(launchRequest())
      if (nextPreview.confirmation_required) {
        setPreview(nextPreview)
      } else {
        const result = await launchAgent(launchRequest())
        setMessage(result.reused ? '已恢复现有自动交易目标' : '自动交易 Agent 已启动')
        await refreshDetails()
      }
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '启动失败')
    } finally {
      setBusy('')
    }
  }

  const confirmLaunch = async () => {
    if (!preview) return
    setBusy('launch')
    try {
      const result = await launchAgent(launchRequest(true, preview.grant_fingerprint))
      setPreview(null)
      setMessage(result.reused ? '授权已更新，现有 Agent 已恢复' : '授权已保存，Agent 已启动')
      await refreshDetails()
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '授权启动失败')
    } finally {
      setBusy('')
    }
  }

  const actOnGoal = async (action: 'toggle' | 'run' | 'archive') => {
    if (!fullGoal) return
    setBusy(action); setMessage('')
    try {
      if (action === 'toggle') {
        await setAgentGoalStatus(fullGoal, fullGoal.status === 'active' ? 'paused' : 'active')
      } else if (action === 'run') {
        await runAgentOnce(fullGoal.id)
      } else {
        if (!window.confirm(`确认归档 Agent 目标？\n\n${fullGoal.objective}\n\n历史运行和交易证据仍会保留。`)) return
        await archiveAgentGoal(fullGoal.id)
      }
      await refreshDetails()
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '操作失败')
    } finally {
      setBusy('')
    }
  }

  const inspectRun = async (runId: string) => {
    setBusy('trace')
    try { setTrace(await getAgentRunTrace(runId)) }
    catch (cause) { setMessage(cause instanceof Error ? cause.message : '运行详情读取失败') }
    finally { setBusy('') }
  }

  const restoreGoal = async (goalId: string) => {
    setBusy(`restore-${goalId}`); setMessage('')
    try {
      await restoreAgentGoal(goalId)
      setMessage('目标已恢复为暂停状态，请核对范围后手动恢复运行')
      await refreshDetails()
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '目标恢复失败')
    } finally { setBusy('') }
  }

  return <section className="ops-page agent-console-page">
    <header className="ops-page-header">
      <div><span className="ops-kicker">交易执行 Agent</span><h1>自动交易控制台</h1><p>持续感知市场、选择已验证策略，并在确定性风控许可后执行。</p></div>
      <div className={`connection-state ${streamState}`}><RadioTower /><span>{streamState === 'connected' ? '实时状态已连接' : streamState === 'fallback' ? '轮询降级 · 状态可能延迟' : '正在连接状态流'}</span></div>
    </header>

    <div className="agent-command-strip">
      <div className="agent-primary-state">
        <i className={selectedGoal?.runtime_status ?? 'offline'} />
        <div><span>当前运行状态</span><strong>{selectedGoal ? statusLabel(selectedGoal.runtime_status) : '尚未启动'}</strong><small>{selectedGoal ? `最后心跳 ${localTime(selectedGoal.last_heartbeat_at)}` : '选择品种和周期即可启动'}</small></div>
      </div>
      <div><span>市场感知</span><strong>{selectedGoal ? `${selectedGoal.market_sense.filter(item => item.status === 'healthy').length}/${selectedGoal.market_sense.length} 正常` : '—'}</strong><small>{selectedGoal?.next_scan_at ? `下次扫描 ${localTime(selectedGoal.next_scan_at)}` : '等待调度'}</small></div>
      <div><span>当前策略</span><strong>{selectedGoal?.current_strategy?.name ?? '等待合格策略'}</strong><small>{selectedGoal?.current_strategy ? `${selectedGoal.current_strategy.symbol} · ${selectedGoal.current_strategy.timeframe} · ${selectedGoal.current_strategy.family}` : selectedGoal?.blocking_reasons[0] ?? '当前没有满足验证门槛的策略'}</small></div>
      <div><span>最近动作</span><strong>{selectedGoal?.last_action?.toUpperCase() ?? 'WAIT'}</strong><small>{selectedGoal?.last_action_summary ?? '尚无最新决策'}</small></div>
    </div>

    {overview?.risk_halted && <div className="agent-risk-banner"><ShieldCheck /><div><strong>风险闸正在阻止新开仓</strong><span>{overview.risk_reason}</span></div><small>Agent 可以继续感知和研究，不会绕过该限制。</small></div>}

    <div className="agent-console-layout">
      <aside className="agent-selector-panel">
        <div className="panel-title"><span>可管理目标</span><b>{overview?.goals.length ?? 0}</b></div>
        <div className="agent-goal-selector">
          {overview?.goals.map(goal => <button key={goal.goal_id} className={goal.goal_id === selectedGoal?.goal_id ? 'active' : ''} onClick={() => setSelectedGoalId(goal.goal_id)}><i className={goal.runtime_status} /><span>{goal.symbols.join(' · ')}</span><small>{goal.timeframes.join(' · ')} · {statusLabel(goal.runtime_status)}</small><ChevronRight /></button>)}
          {!overview?.goals.length && <p className="ops-empty">还没有可管理的 Agent 目标。</p>}
        </div>
        <div className="quick-launch">
          <div className="panel-title"><span>快速启动</span><Sparkles /></div>
          <label><span>交易品种</span><div className="choice-grid">{settings.configuration.strategy.watchlist.map(symbol => <button type="button" key={symbol} className={symbols.includes(symbol) ? 'active' : ''} onClick={() => setSymbols(current => current.includes(symbol) ? current.filter(item => item !== symbol) : [...current, symbol])}>{symbol.replace('/USDT', '')}</button>)}</div></label>
          <label><span>运行周期</span><div className="choice-grid">{settings.configuration.strategy.timeframes.map(timeframe => <button type="button" key={timeframe} className={timeframes.includes(timeframe) ? 'active' : ''} onClick={() => setTimeframes(current => current.includes(timeframe) ? current.filter(item => item !== timeframe) : [...current, timeframe])}>{timeframe}</button>)}</div></label>
          <div className="launch-risk-summary"><span>{settings.configuration.trading_mode === 'live' ? '实盘' : settings.configuration.trading_mode === 'sandbox' ? '测试网' : '模拟盘'}</span><span>平台默认风险模板</span><span>单笔风险 {Number(settings.configuration.risk.risk_per_trade) * 100}%</span><span>原生保护单</span></div>
          <button className="button primary launch-button" disabled={busy === 'launch' || !symbols.length || !timeframes.length} onClick={() => void startAgent()}><Play />{busy === 'launch' ? '正在核对…' : '启动自动交易'}</button>
        </div>
      </aside>

      <main className="agent-detail-panel">
        <div className="subnav-tabs">{([
          ['overview', '概览'], ['activity', '运行活动'], ['episodes', '交易回合'], ['performance', '绩效与记忆'], ['goals', '目标管理'],
        ] as Array<[AgentTab, string]>).map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>)}</div>

        {tab === 'overview' && <div className="agent-overview-grid">
          <section><div className="panel-title"><span>市场感知</span><Activity /></div><div className="sense-list">{selectedGoal?.market_sense.map(item => <article key={`${item.symbol}-${item.timeframe}`}><i className={item.status} /><strong>{item.symbol}</strong><b>{item.timeframe}</b><span>{item.status === 'healthy' ? '感知正常' : item.status}</span><time>{localTime(item.last_event_at)}</time></article>)}{!selectedGoal?.market_sense.length && <p className="ops-empty">Agent 启动后，这里会显示每个市场周期的实时感知状态。</p>}</div></section>
          <section><div className="panel-title"><span>最近生成的策略</span><BrainCircuit /></div><div className="strategy-brief-list">{selectedGoal?.recently_generated_strategies.map(strategy => <article key={strategy.plan_id}><div><strong>{strategy.name}</strong><span>{strategy.symbol} · {strategy.timeframe} · {strategy.family}</span></div><b className={strategy.status}>{strategy.status}</b><small>{localTime(strategy.updated_at)}</small></article>)}{!selectedGoal?.recently_generated_strategies.length && <p className="ops-empty">当前尚未生成可追溯策略，Agent 会在方向证据出现后开始筛选。</p>}</div></section>
          <section className="latest-decision"><div className="panel-title"><span>最近决策</span><Clock3 /></div><strong>{selectedGoal?.last_action?.toUpperCase() ?? 'WAIT'}</strong><p>{selectedGoal?.last_action_summary ?? '等待首个完整市场事件。'}</p>{selectedGoal?.blocking_reasons.map(reason => <span key={reason}>{reason}</span>)}</section>
        </div>}

        {tab === 'activity' && <div className="activity-timeline">{runs.map(run => <button key={run.id} onClick={() => void inspectRun(run.id)}><i className={run.status} /><time>{localTime(run.completed_at ?? run.updated_at)}</time><div><strong>{run.status === 'waiting' ? '等待下一市场条件' : run.status}</strong><span>{run.step_count}/{run.max_steps} 步 · {run.id}</span><p>{run.last_error || '运行证据已持久化，点击查看决策链。'}</p></div><ChevronRight /></button>)}{!runs.length && <p className="ops-empty">尚无运行记录。</p>}</div>}

        {tab === 'episodes' && <div className="episode-table"><div className="table-head"><span>品种</span><span>方向</span><span>状态</span><span>计划风险</span><span>净收益</span><span>时间</span></div>{episodes.map(episode => <div key={episode.id}><strong>{episode.symbol}</strong><span>{episode.side}</span><b className={episode.status}>{episode.status}</b><span>{episode.planned_risk} USDT</span><span>{episode.realized_pnl ?? '—'}</span><time>{localTime(episode.closed_at ?? episode.opened_at)}</time></div>)}{!episodes.length && <p className="ops-empty">还没有由该 Agent 完整归因的交易回合。</p>}</div>}

        {tab === 'performance' && <div className="performance-workspace"><section><div className="panel-title"><span>测试网前向绩效</span><b>{performance?.status ?? '读取中'}</b></div><dl><div><dt>完整回合</dt><dd>{performance?.attributable_episodes ?? 0}</dd></div><div><dt>净收益</dt><dd>{performance?.net_pnl ?? '0'} USDT</dd></div><div><dt>累计 R</dt><dd>{performance?.total_r ?? '0'}R</dd></div><div><dt>胜率</dt><dd>{performance?.win_rate_percent ?? '0'}%</dd></div><div><dt>Profit Factor</dt><dd>{performance?.profit_factor ?? '—'}</dd></div></dl></section><section><div className="panel-title"><span>可追溯记忆</span><b>{state?.memories.length ?? 0}</b></div>{state?.memories.slice(0, 12).map(memory => <article className="memory-row" key={memory.id}><span>{memory.memory_type}</span><p>{memory.content}</p><small>{memory.tags.join(' · ')}</small></article>)}</section></div>}

        {tab === 'goals' && <div className="goal-management"><div className="goal-management-head"><div><span>{selectedGoal?.autonomy_mode.toUpperCase()}</span><h2>{selectedGoal?.objective ?? '未选择目标'}</h2><p>{selectedGoal?.symbols.join(' · ')} / {selectedGoal?.timeframes.join(' · ')}</p></div>{selectedGoal && <b className={selectedGoal.runtime_status}>{statusLabel(selectedGoal.runtime_status)}</b>}</div>{fullGoal && <div className="goal-actions"><button className="button ghost" disabled={Boolean(busy)} onClick={() => void actOnGoal('toggle')}>{fullGoal.status === 'active' ? <CirclePause /> : <Play />}{fullGoal.status === 'active' ? '暂停目标' : '恢复目标'}</button><button className="button ghost" disabled={Boolean(busy) || fullGoal.status !== 'active'} onClick={() => void actOnGoal('run')}><RefreshCw />立即运行一次</button><button className="button danger" disabled={Boolean(busy)} onClick={() => void actOnGoal('archive')}><Archive />归档目标</button></div>}{readiness && <div className="readiness-list"><div className="panel-title"><span>自主交易就绪检查</span><b>{readiness.checks.filter(item => item.passed).length}/{readiness.checks.length}</b></div>{readiness.checks.map(check => <article key={check.code} className={check.passed ? 'passed' : 'failed'}><i /> <strong>{check.code.replaceAll('_', ' ')}</strong><span>{check.message}</span></article>)}</div>}{archivedGoals.length > 0 && <section className="archived-goals"><div className="panel-title"><span>已归档目标</span><b>{archivedGoals.length}</b></div>{archivedGoals.map(goal => <article key={goal.id}><div><strong>{goal.symbols.join(' · ')}</strong><span>{goal.timeframes.join(' · ')} · {goal.objective}</span></div><button className="button ghost" disabled={Boolean(busy)} onClick={() => void restoreGoal(goal.id)}><RefreshCw />恢复为暂停</button></article>)}</section>}</div>}
      </main>
    </div>

    {message && <div className="ops-toast" role="status">{message}<button onClick={() => setMessage('')}><X /></button></div>}
    {preview && <div className="modal-backdrop" onMouseDown={() => setPreview(null)}><div className="authorization-dialog" role="dialog" aria-modal="true" onMouseDown={event => event.stopPropagation()}><span className="ops-kicker">首次授权确认</span><h2>允许 Agent 在所选范围内自动交易</h2><p>{preview.symbols.join(' · ')} / {preview.timeframes.join(' · ')} · {preview.mode}</p><dl>{Object.entries(preview.risk_summary).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl>{preview.blocking_reasons.map(reason => <div className="authorization-warning" key={reason}>{reason}</div>)}<small>系统会记住该权限指纹；扩大范围、提高权限或切换实盘时将再次确认。</small><div><button className="button ghost" onClick={() => setPreview(null)}>取消</button><button className="button primary" disabled={busy === 'launch'} onClick={() => void confirmLaunch()}><ShieldCheck />确认并启动</button></div></div></div>}
    {trace && <div className="detail-drawer-backdrop" onMouseDown={() => setTrace(null)}><aside className="detail-drawer" onMouseDown={event => event.stopPropagation()}><header><div><span>运行证据</span><strong>{trace.run.id}</strong></div><button onClick={() => setTrace(null)}><X /></button></header><div className="trace-summary"><span>{trace.run.status}</span><span>{trace.run.step_count}/{trace.run.max_steps} 步</span><time>{localTime(trace.run.completed_at ?? trace.run.updated_at)}</time></div>{trace.decisions.map((decision, index) => <article className="decision-record" key={decision.id}><b>{String(index + 1).padStart(2, '0')}</b><div><strong>{decision.action}</strong><p>{decision.reason}</p><small>{decision.evidence.join(' · ')}</small></div></article>)}</aside></div>}
  </section>
}
