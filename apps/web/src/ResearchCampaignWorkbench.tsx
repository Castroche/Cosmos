import { useEffect, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import { Beaker, RefreshCw, ShieldCheck } from 'lucide-react'
import {
  getResearchCampaign,
  getResearchCampaignJob,
  getResearchCampaignJobs,
  getResearchCampaigns,
  getStrategyLibrarySummary,
  queueResearchCampaign,
  reviewStrategyGovernance,
} from './api'
import type {
  ResearchCampaignJob,
  ResearchCampaignReport,
  ResearchDataset,
  StrategyCandidate,
} from './types'
import RelativeValueWorkbench from './RelativeValueWorkbench'
import ProductAcceptancePanel from './ProductAcceptancePanel'

type Props = {
  datasets: ResearchDataset[]
  candidates: StrategyCandidate[]
  onGovernanceComplete?: () => void
}

const number = (value: string | number | undefined, digits = 2) =>
  Number(value ?? 0).toFixed(digits)

export default function ResearchCampaignWorkbench({
  datasets,
  candidates,
  onGovernanceComplete,
}: Props) {
  const [selectedDevelopment, setSelectedDevelopment] = useState<string[] | null>(null)
  const [selectedBlind, setSelectedBlind] = useState<string[] | null>(null)
  const [reports, setReports] = useState<ResearchCampaignReport[]>([])
  const [libraryCount, setLibraryCount] = useState(0)
  const [running, setRunning] = useState(false)
  const [job, setJob] = useState<ResearchCampaignJob | null>(null)
  const [governing, setGoverning] = useState(false)
  const [error, setError] = useState('')

  const eligibleDatasets = useMemo(() => {
    const sealed = datasets.filter(item => (
      item.gap_count === 0
      && item.invalid_count === 0
      && Boolean(item.blind_set_id)
      && (item.research_role === 'development' || item.research_role === 'blind')
    ))
    const groups = new Map<string, ResearchDataset[]>()
    for (const dataset of sealed) {
      const key = dataset.blind_set_id as string
      groups.set(key, [...(groups.get(key) ?? []), dataset])
    }
    return [...groups.values()]
      .sort((a, b) => {
        if (a.length !== b.length) return b.length - a.length
        return Date.parse(b[0]?.created_at ?? '') - Date.parse(a[0]?.created_at ?? '')
      })[0]
      ?.sort((a, b) => (
        a.symbol.localeCompare(b.symbol)
        || a.timeframe.localeCompare(b.timeframe)
        || a.research_role.localeCompare(b.research_role)
      )) ?? []
  }, [datasets])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const [nextReports, summary, jobs] = await Promise.all([
          getResearchCampaigns(),
          getStrategyLibrarySummary(),
          getResearchCampaignJobs(),
        ])
        if (cancelled) return
        setReports(nextReports)
        setLibraryCount(summary.entry_count)
        let active = jobs.find(item => (
          item.status === 'queued' || item.status === 'running'
        ))
        if (!active) return
        setJob(active)
        setRunning(true)
        while (
          !cancelled
          && (active.status === 'queued' || active.status === 'running')
        ) {
          await new Promise(resolve => window.setTimeout(resolve, 1_000))
          active = await getResearchCampaignJob(active.id)
          if (!cancelled) setJob(active)
        }
        if (
          !cancelled
          && active.status === 'completed'
          && active.report_id
        ) {
          const recovered = await getResearchCampaign(active.report_id)
          if (!cancelled) {
            setReports(current => [
              recovered,
              ...current.filter(item => item.id !== recovered.id),
            ])
          }
        } else if (!cancelled && active.status === 'failed') {
          setError(active.error || '恢复的 Campaign 后台作业失败')
        }
      } catch {
        // Existing reports remain usable if recovery polling is unavailable.
      } finally {
        if (!cancelled) setRunning(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const report = reports[0]
  const defaultDevelopment = eligibleDatasets
    .filter(item => item.research_role === 'development')
    .slice(0, 6)
    .map(item => item.id)
  const defaultBlind = eligibleDatasets
    .filter(item => item.research_role === 'blind')
    .slice(0, 6)
    .map(item => item.id)
  const effectiveDevelopment = selectedDevelopment ?? defaultDevelopment
  const effectiveBlind = selectedBlind ?? defaultBlind
  const matrixSize = effectiveDevelopment.length * libraryCount + effectiveBlind.length
  const selectedSourceIds = new Set(effectiveDevelopment.map(
    id => eligibleDatasets.find(item => item.id === id)?.source_dataset_id,
  ))
  const blindSourceIds = new Set(effectiveBlind.map(
    id => eligibleDatasets.find(item => item.id === id)?.source_dataset_id,
  ))
  const pairedSelection = (
    selectedSourceIds.size > 0
    && selectedSourceIds.size === blindSourceIds.size
    && [...selectedSourceIds].every(id => blindSourceIds.has(id))
  )

  const toggleDataset = (datasetId: string) => {
    const dataset = eligibleDatasets.find(item => item.id === datasetId)
    if (!dataset) return
    const update = dataset.research_role === 'blind'
      ? setSelectedBlind
      : setSelectedDevelopment
    const effective = dataset.research_role === 'blind'
      ? effectiveBlind
      : effectiveDevelopment
    update(current => {
      const base = current ?? effective
      return base.includes(datasetId)
        ? base.filter(item => item !== datasetId)
        : base.length < 6
          ? [...base, datasetId]
          : base
    })
  }

  const runCampaign = async () => {
    if (!effectiveDevelopment.length || !effectiveBlind.length || !pairedSelection) return
    setRunning(true)
    setError('')
    try {
      let nextJob = await queueResearchCampaign(effectiveDevelopment, effectiveBlind)
      setJob(nextJob)
      while (nextJob.status === 'queued' || nextJob.status === 'running') {
        await new Promise(resolve => window.setTimeout(resolve, 1_000))
        nextJob = await getResearchCampaignJob(nextJob.id)
        setJob(nextJob)
      }
      if (nextJob.status === 'failed') {
        throw new Error(nextJob.error || 'Campaign 后台作业失败')
      }
      if (!nextJob.report_id) {
        throw new Error('Campaign 完成但缺少报告 ID')
      }
      const next = await getResearchCampaign(nextJob.report_id)
      setReports(current => [next, ...current.filter(item => item.id !== next.id)])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Campaign 运行失败')
    } finally {
      setRunning(false)
    }
  }

  const runGovernance = async () => {
    setGoverning(true)
    setError('')
    try {
      await reviewStrategyGovernance()
      onGovernanceComplete?.()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '策略健康复核失败')
    } finally {
      setGoverning(false)
    }
  }

  return (
    <>
    <ProductAcceptancePanel />
    <section className="campaign-workbench">
      <header>
        <div>
          <span className="eyebrow">MULTI-MARKET RESEARCH CAMPAIGN</span>
          <h3>策略搜索与反过拟合验收</h3>
          <p>一次性运行公开策略 × 不可变数据集矩阵；选择、统计修正和 PBO 使用同一次完整搜索范围。</p>
        </div>
        <div className="campaign-actions">
          <button className="button ghost" type="button" onClick={() => void runGovernance()} disabled={governing}>
            <ShieldCheck />{governing ? '复核中' : '复核策略健康'}
          </button>
          <button className="button primary" type="button" onClick={() => void runCampaign()} disabled={running || !effectiveDevelopment.length || !effectiveBlind.length || !pairedSelection || !libraryCount || matrixSize > 480}>
            {running ? <RefreshCw className="spinning" /> : <Beaker />}
            {running ? `运行 ${matrixSize} 个回测` : `运行矩阵 · ${matrixSize}`}
          </button>
        </div>
      </header>

      <div className="campaign-scope">
        {eligibleDatasets.map(dataset => {
          const checked = dataset.research_role === 'blind'
            ? effectiveBlind.includes(dataset.id)
            : effectiveDevelopment.includes(dataset.id)
          return (
          <label key={dataset.id} className={checked ? `selected ${dataset.research_role}` : dataset.research_role}>
            <input
              type="checkbox"
              checked={checked}
              onChange={() => toggleDataset(dataset.id)}
            />
            <span>{dataset.symbol} · {dataset.timeframe} · {dataset.research_role === 'blind' ? '密封盲测' : '研究开发'}</span>
            <small>{dataset.candle_count} bars · 隔离 {dataset.embargo_bars} bars · 覆盖 {number(Number(dataset.coverage_ratio) * 100, 1)}%</small>
          </label>
          )
        })}
        {!eligibleDatasets.length && <p>暂无成对的研究开发集与密封盲测集。</p>}
      </div>

      {job && (
        <div className={`campaign-progress ${job.status}`}>
          <div>
            <span>JOB {job.id}</span>
            <strong>{job.status.toUpperCase()}</strong>
            <small>
              {job.completed_cells}/{job.total_cells || matrixSize} 单元
              {' · '}缓存 {job.cache_hits}
              {' · '}新算 {job.computed_cells}
            </small>
          </div>
          <i>
            <span
              style={{
                width: `${job.total_cells
                  ? Math.min(job.completed_cells / job.total_cells * 100, 100)
                  : 0}%`,
              }}
            />
          </i>
        </div>
      )}

      {error && <div className="campaign-error">{error}</div>}

      {report ? (
        <>
          <div className="campaign-verdict">
            <div>
              <span>CAMPAIGN</span>
              <strong>{report.id}</strong>
              <small>{new Date(report.created_at).toLocaleString('zh-CN')} · {report.trial_count} 次独立试验</small>
            </div>
            <dl>
              <div><dt>策略候选</dt><dd>{report.candidates.length}</dd></div>
              <div><dt>通过跨市场</dt><dd>{report.candidates.filter(item => item.cross_market_passed).length}</dd></div>
              <div><dt>PBO</dt><dd className={report.pbo.passed ? 'positive' : 'negative'}>{number(Number(report.pbo.probability_of_backtest_overfitting) * 100, 1)}%</dd></div>
              <div><dt>独立盲测</dt><dd className={report.blind_selected_candidate_passed ? 'positive' : 'negative'}>{report.blind_selected_candidate_passed ? 'PASS' : 'HOLD'}</dd></div>
              <div><dt>自主执行资格</dt><dd className={report.autonomous_execution_eligible ? 'positive' : 'negative'}>{report.autonomous_execution_eligible ? 'PASS' : 'HOLD'}</dd></div>
              <div><dt>不可变证书</dt><dd className={report.execution_certificate ? 'positive' : 'negative'}>{report.execution_certificate ? report.execution_certificate.fingerprint.slice(0, 10) : 'NONE'}</dd></div>
            </dl>
          </div>

          <div className="campaign-ranking">
            <header>
              <strong>跨市场候选排行</strong>
              <span>{report.selected_library_entry_id ?? '没有策略取得执行资格'}</span>
            </header>
            <div className="campaign-table">
              <div className="campaign-row heading">
                <span>策略 / 原型</span><span>市场</span><span>压力为正</span><span>胜率</span><span>盈亏比</span><span>最差回撤</span><span>状态</span>
              </div>
              {report.candidates.slice(0, 10).map(candidate => (
                <div className="campaign-row" key={candidate.library_entry_id}>
                  <span><strong>{candidate.library_entry_id}</strong><small>{candidate.archetype}</small></span>
                  <span>{candidate.robust_market_count}/{candidate.market_count} · {candidate.unique_symbol_count}币</span>
                  <span>{number(Number(candidate.positive_stress_ratio) * 100, 0)}% / Q{number(Number(candidate.high_quality_market_ratio) * 100, 0)}%</span>
                  <span>{number(candidate.median_win_rate_percent, 1)}%</span>
                  <span>{number(candidate.median_profit_factor)} / {number(candidate.median_payoff_ratio)}</span>
                  <span>{number(candidate.worst_drawdown_percent)}%</span>
                  <span className={candidate.cross_market_passed ? 'positive' : 'negative'}>{candidate.cross_market_passed ? 'PASS' : 'HOLD'}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="campaign-allocation">
            <header><strong>组合风险先验</strong><span>未分配权重保留为现金，不强制凑满仓位</span></header>
            {report.portfolio_allocations.length ? report.portfolio_allocations.map(item => (
              <div key={item.library_entry_id}>
                <span>{item.library_entry_id}</span>
                <i style={{ '--allocation': `${Number(item.weight) * 100}%` } as CSSProperties} />
                <strong>{number(Number(item.weight) * 100, 1)}%</strong>
              </div>
            )) : <p>没有策略通过跨市场门槛，组合保持现金。</p>}
          </div>
        </>
      ) : (
        <div className="campaign-empty">
          <Beaker />
          <strong>尚未运行完整研究矩阵</strong>
          <p>当前有 {datasets.length} 个数据集、{candidates.length} 个隔离候选；单次最佳回测不能替代 Campaign。</p>
        </div>
      )}
    </section>
    <RelativeValueWorkbench datasets={datasets} />
    </>
  )
}
