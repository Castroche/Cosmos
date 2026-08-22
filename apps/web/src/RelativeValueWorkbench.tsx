import { useEffect, useMemo, useState } from 'react'
import { GitCompareArrows, RefreshCw, ShieldX } from 'lucide-react'
import {
  getRelativeValueLibrary,
  getRelativeValueReports,
  runRelativeValueResearch,
} from './api'
import type {
  PairBacktestReport,
  ResearchDataset,
} from './types'

type Props = {
  datasets: ResearchDataset[]
}

const number = (value: string | number | undefined, digits = 2) =>
  Number(value ?? 0).toFixed(digits)

export default function RelativeValueWorkbench({ datasets }: Props) {
  const eligible = useMemo(
    () => datasets
      .filter(item => (
        item.market_type === 'swap'
        && item.gap_count === 0
        && item.invalid_count === 0
      ))
      .sort((a, b) => b.candle_count - a.candle_count),
    [datasets],
  )
  const defaultA = eligible.find(item => item.symbol === 'BTC/USDT' && item.timeframe === '4h')
    ?? eligible[0]
  const defaultB = eligible.find(item => (
    item.id !== defaultA?.id
    && item.timeframe === defaultA?.timeframe
  ))
  const [datasetA, setDatasetA] = useState(defaultA?.id ?? '')
  const [datasetB, setDatasetB] = useState(defaultB?.id ?? '')
  const [reports, setReports] = useState<PairBacktestReport[]>([])
  const [librarySize, setLibrarySize] = useState(0)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void Promise.all([
      getRelativeValueReports(),
      getRelativeValueLibrary(),
    ])
      .then(([nextReports, library]) => {
        setReports(nextReports)
        setLibrarySize(library.entries.length)
      })
      .catch(() => undefined)
  }, [])

  const effectiveDatasetA = datasetA || defaultA?.id || ''
  const effectiveDatasetB = datasetB || defaultB?.id || ''
  const first = eligible.find(item => item.id === effectiveDatasetA)
  const secondOptions = eligible.filter(item => (
    item.id !== effectiveDatasetA
    && (!first || item.timeframe === first.timeframe)
  ))
  const second = eligible.find(item => item.id === effectiveDatasetB)
  const validPair = Boolean(
    first
    && second
    && first.id !== second.id
    && first.timeframe === second.timeframe,
  )

  const run = async () => {
    if (!validPair) return
    setRunning(true)
    setError('')
    try {
      const next = await runRelativeValueResearch(
        effectiveDatasetA,
        effectiveDatasetB,
      )
      setReports(current => [
        ...next,
        ...current.filter(item => !next.some(result => result.id === item.id)),
      ])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '相对价值研究失败')
    } finally {
      setRunning(false)
    }
  }

  const visible = reports.filter(item => (
    (!effectiveDatasetA || item.dataset_a_id === effectiveDatasetA)
    && (!effectiveDatasetB || item.dataset_b_id === effectiveDatasetB)
  ))
  const latest = visible.length ? visible : reports.slice(0, 4)

  return (
    <section className="relative-value-workbench">
      <header>
        <div>
          <span className="eyebrow">CAUSAL MULTI-LEG RESEARCH</span>
          <h3>双腿相对价值研究</h3>
          <p>
            等美元多空、同步时间戳、信号后下一根成交；双腿手续费、滑点和历史资金费均计入。
          </p>
        </div>
        <span className="research-only-badge"><ShieldX />研究专用 · 禁止执行</span>
      </header>

      <div className="relative-value-controls">
        <label>
          <span>第一腿数据集</span>
          <select
            value={effectiveDatasetA}
            onChange={event => {
              const next = event.target.value
              const selected = eligible.find(item => item.id === next)
              setDatasetA(next)
              if (!selected || !second || selected.timeframe !== second.timeframe) {
                setDatasetB(
                  eligible.find(item => (
                    item.id !== next
                    && item.timeframe === selected?.timeframe
                  ))?.id ?? '',
                )
              }
            }}
          >
            {eligible.map(item => (
              <option value={item.id} key={item.id}>
                {item.symbol} · {item.timeframe} · {item.candle_count} bars
              </option>
            ))}
          </select>
        </label>
        <GitCompareArrows />
        <label>
          <span>第二腿数据集</span>
          <select
            value={effectiveDatasetB}
            onChange={event => setDatasetB(event.target.value)}
          >
            {secondOptions.map(item => (
              <option value={item.id} key={item.id}>
                {item.symbol} · {item.timeframe} · {item.candle_count} bars
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="button primary"
          disabled={!validPair || running || !librarySize}
          onClick={() => void run()}
        >
          {running ? <RefreshCw className="spinning" /> : <GitCompareArrows />}
          {running ? '回测双腿' : `运行 ${librarySize} 个公开假设`}
        </button>
      </div>

      {first && second && (
        <div className="relative-value-data-evidence">
          {[first, second].map(item => (
            <span key={item.id}>
              <strong>{item.symbol}</strong>
              <small>
                资金费 {item.funding_count} 点
                {' · '}持仓量 {item.open_interest_count ?? 0} 点
                {' · '}OI覆盖 {number(Number(item.open_interest_coverage_ratio ?? 0) * 100, 1)}%
              </small>
            </span>
          ))}
        </div>
      )}

      {error && <div className="campaign-error">{error}</div>}

      <div className="relative-value-table">
        <div className="relative-value-row heading">
          <span>配对 / 参数</span>
          <span>收益</span>
          <span>胜率</span>
          <span>盈亏比</span>
          <span>回撤</span>
          <span>资金费覆盖</span>
          <span>结论</span>
        </div>
        {latest.map(report => (
          <div className="relative-value-row" key={report.id}>
            <span>
              <strong>{report.symbol_a} / {report.symbol_b}</strong>
              <small>
                {report.timeframe} · W{report.strategy_spec.formation_window}
                {' · '}Z{report.strategy_spec.entry_zscore}
              </small>
            </span>
            <span>{number(report.return_percent)}%</span>
            <span>{number(report.win_rate_percent)}%</span>
            <span>{number(report.payoff_ratio)}</span>
            <span>{number(report.max_drawdown_percent)}%</span>
            <span>
              {number(report.funding_coverage_percent_a, 1)}%
              {' / '}
              {number(report.funding_coverage_percent_b, 1)}%
            </span>
            <span className="negative">
              {report.execution_eligible ? 'PASS' : 'HOLD'}
              <small>{report.research_gate_passed ? '单次门槛通过' : '研究门槛未通过'}</small>
            </span>
          </div>
        ))}
        {!latest.length && (
          <div className="relative-value-empty">
            选择两个同周期不可变数据集后运行；单一配对结果不会取得稳健性或执行资格。
          </div>
        )}
      </div>

      {latest[0] && (
        <div className="relative-value-reasons">
          <strong>最新拒绝依据</strong>
          <ul>
            {latest[0].validation_reasons.slice(0, 6).map(reason => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
