import { useMemo, useState } from 'react'
import { Activity, CheckCircle2, XCircle } from 'lucide-react'
import type { BacktestResult, UserConfiguration } from './types'

type BacktestView = 'summary' | 'trades' | 'validation'

type BacktestEvidenceProps = {
  result: BacktestResult
  validation: UserConfiguration['strategy']['validation']
}

const money = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

function pct(value: string | undefined) {
  if (value == null) return '—'
  const numeric = Number(value)
  return `${numeric > 0 ? '+' : ''}${numeric.toFixed(2)}%`
}

function plainPct(value: string | undefined) {
  if (value == null) return '—'
  return `${Number(value).toFixed(2)}%`
}

const strategyFamilyLabels: Record<string, string> = {
  trend: '趋势',
  mean_reversion: '均值回归',
  breakout: '突破',
  unknown: '未归因',
}

function EquityCurve({ result }: { result: BacktestResult }) {
  const values = result.equity_curve.map(Number).filter(Number.isFinite)
  if (values.length < 2) {
    return <div className="backtest-chart-empty">权益曲线数据不足</div>
  }
  const width = 1000
  const height = 220
  const low = Math.min(...values)
  const high = Math.max(...values)
  const range = Math.max(high - low, 1)
  const points = values.map((value, index) => {
    const x = index / (values.length - 1) * width
    const y = height - 18 - (value - low) / range * (height - 36)
    return `${x.toFixed(2)},${y.toFixed(2)}`
  })
  const positive = Number(result.final_equity) >= Number(result.initial_equity)
  return (
    <div className={`backtest-equity-chart ${positive ? 'positive-curve' : 'negative-curve'}`}>
      <div>
        <span>权益曲线</span>
        <small>{money.format(low)} — {money.format(high)} USDT</small>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="回测权益曲线">
        <path className="curve-area" d={`M 0 ${height} L ${points.join(' L ')} L ${width} ${height} Z`} />
        <polyline points={points.join(' ')} />
      </svg>
    </div>
  )
}

export default function BacktestEvidence({ result, validation }: BacktestEvidenceProps) {
  const [view, setView] = useState<BacktestView>('summary')
  const strategyAttribution = result.strategy_attribution ?? []

  const validationChecks = useMemo(() => {
    const tradeSampleStandard = result.trade_sample_standard || validation.minimum_trades
    const tradeSampleFloor = result.trade_sample_floor
      || Math.ceil(tradeSampleStandard * Number(validation.minimum_trade_evidence_ratio))
    const tradeSamplePassed = (
      result.total_trades >= tradeSampleStandard
      || result.trade_sample_corroborated
    )
    const checks = [
      {
        label: '有效交易数量',
        passed: tradeSamplePassed,
        value: result.trade_sample_tier === 'strong_multi_evidence'
          ? `${result.total_trades} / 强证据最低 ${result.trade_sample_strong_floor ?? 30} · 多重佐证`
          : result.trade_sample_corroborated && result.total_trades < tradeSampleStandard
            ? `${result.total_trades} / 标准 ${tradeSampleStandard} · 近期与压力佐证通过`
          : `${result.total_trades} / 标准 ${tradeSampleStandard} · 最低 ${tradeSampleFloor}`,
      },
      {
        label: '已收盘样本',
        passed: result.sample_bars >= validation.minimum_sample_bars,
        value: `${result.sample_bars} / ≥ ${validation.minimum_sample_bars}`,
      },
      {
        label: 'Profit Factor',
        passed: Number(result.profit_factor) >= Number(validation.minimum_profit_factor),
        value: `${Number(result.profit_factor).toFixed(2)} / ≥ ${validation.minimum_profit_factor}`,
      },
      {
        label: '执行级胜率',
        passed: Number(result.win_rate_percent) >= Number(validation.minimum_win_rate_percent),
        value: `${plainPct(result.win_rate_percent)} / ≥ ${validation.minimum_win_rate_percent}%`,
      },
      {
        label: '平均盈利 / 平均亏损',
        passed: Number(result.payoff_ratio) >= Number(validation.minimum_payoff_ratio),
        value: `${Number(result.payoff_ratio).toFixed(2)} / ≥ ${validation.minimum_payoff_ratio}`,
      },
      {
        label: '最大回撤',
        passed: Number(result.max_drawdown_percent) <= Number(validation.maximum_drawdown_percent),
        value: `${result.max_drawdown_percent}% / ≤ ${validation.maximum_drawdown_percent}%`,
      },
      {
        label: '成本压力收益',
        passed: Number(result.stress_return_percent ?? 0) > 0,
        value: pct(result.stress_return_percent),
      },
      {
        label: '时间稳定性',
        passed: result.temporal_stability?.passed === true,
        value: result.temporal_stability
          ? `${result.temporal_stability.positive_folds}/${result.temporal_stability.evaluable_folds} 个可评估分段为正`
          : '缺少分段验证',
      },
      {
        label: '无期末强制平仓',
        passed: !result.terminal_position_closed,
        value: result.terminal_position_closed ? '发生（需审查）' : '未发生',
      },
    ]
    if (result.trade_sample_strongly_corroborated) {
      checks.push({
        label: '参数前推验证',
        passed: result.parameter_walk_forward?.passed === true,
        value: result.parameter_walk_forward
          ? `${result.parameter_walk_forward.positive_steps}/${result.parameter_walk_forward.evaluable_steps} 个后续分段为正`
          : '缺少过去选参→下一分段验证',
      })
    }
    if (result.statistical_validation) {
      checks.push({
        label: '搜索后统计可信度',
        passed: result.statistical_validation.uncertainty_passed,
        value: `DSR ${(Number(result.statistical_validation.deflated_sharpe_probability) * 100).toFixed(1)}% · ${result.statistical_validation.trial_count} 次试验`,
      })
    }
    return checks
  }, [result, validation])

  return (
    <>
      <div className="backtest-metrics">
        <div><span>净收益</span><strong className={Number(result.return_percent) >= 0 ? 'positive' : 'negative'}>{pct(result.return_percent)}</strong></div>
        <div><span>压力收益</span><strong className={Number(result.stress_return_percent ?? 0) >= 0 ? 'positive' : 'negative'}>{pct(result.stress_return_percent)}</strong></div>
        <div><span>胜率</span><strong>{plainPct(result.win_rate_percent)}</strong></div>
        <div><span>平均盈亏比</span><strong>{Number(result.payoff_ratio).toFixed(2)}</strong></div>
        <div><span>Profit Factor</span><strong>{Number(result.profit_factor).toFixed(2)}</strong></div>
        <div><span>最大回撤</span><strong>{plainPct(result.max_drawdown_percent)}</strong></div>
        <div><span>Sharpe</span><strong>{Number(result.sharpe).toFixed(2)}</strong></div>
        <div><span>单笔期望</span><strong>{money.format(Number(result.expectancy))} USDT</strong></div>
        <div><span>独立交易 / 样本</span><strong>{result.total_trades} / {result.sample_bars}</strong></div>
      </div>

      <nav className="backtest-tabs" aria-label="回测结果视图">
        <button type="button" className={view === 'summary' ? 'active' : ''} onClick={() => setView('summary')}>收益与成本</button>
        <button type="button" className={view === 'trades' ? 'active' : ''} onClick={() => setView('trades')}>成交明细 <span>{result.trades.length}</span></button>
        <button type="button" className={view === 'validation' ? 'active' : ''} onClick={() => setView('validation')}>门槛验证 <span>{validationChecks.filter(item => item.passed).length}/{validationChecks.length}</span></button>
      </nav>

      {view === 'summary' && (
        <>
          <div className="backtest-summary-view">
            <EquityCurve result={result} />
            <div className="backtest-cost-ledger">
              <div><span>最近 25% 样本</span><strong className={Number(result.recent_window?.return_percent ?? 0) >= 0 ? 'positive' : 'negative'}>{pct(result.recent_window?.return_percent)} / PF {Number(result.recent_window?.profit_factor ?? 0).toFixed(2)}</strong></div>
              <div><span>扣费后净利润</span><strong>{money.format(Number(result.net_profit))} USDT</strong></div>
              <div><span>平均盈利 / 亏损</span><strong>{money.format(Number(result.average_win))} / {money.format(Number(result.average_loss))}</strong></div>
              <div><span>手续费</span><strong>-{money.format(Number(result.total_fees))}</strong></div>
              <div><span>滑点成本</span><strong>-{money.format(Number(result.total_slippage))}</strong></div>
              <div><span>资金费</span><strong>{money.format(Number(result.total_funding))}</strong></div>
              <div><span>市场敞口时间</span><strong>{plainPct(result.exposure_percent)}</strong></div>
              <div><span>年化 / Calmar</span><strong>{pct(result.annualized_return_percent)} / {Number(result.calmar).toFixed(2)}</strong></div>
            </div>
          </div>
          {strategyAttribution.length > 0 && (
            <section className="strategy-attribution">
              <header>
                <div>
                  <span>STRATEGY ATTRIBUTION</span>
                  <strong>策略家族盈亏归因</strong>
                </div>
                <small>按独立入场到完全平仓聚合；分批止盈不重复计作多笔交易</small>
              </header>
              <div className="strategy-attribution-rows">
                {strategyAttribution.map(item => {
                  const profitable = Number(item.net_profit) >= 0
                  const share = Math.max(2, Math.min(100, Number(item.trade_share_percent)))
                  return (
                    <div key={item.algorithm} className={profitable ? 'profitable' : 'unprofitable'}>
                      <div className="strategy-attribution-name">
                        <strong>{strategyFamilyLabels[item.algorithm] ?? item.algorithm}</strong>
                        <small>{item.long_trades} 多 / {item.short_trades} 空</small>
                      </div>
                      <div className="strategy-attribution-share">
                        <div><i style={{ width: `${share}%` }} /></div>
                        <small>{item.trade_share_percent}% · {item.total_trades} 笔</small>
                      </div>
                      <dl>
                        <div><dt>净收益</dt><dd className={profitable ? 'positive' : 'negative'}>{money.format(Number(item.net_profit))}</dd></div>
                        <div><dt>PF</dt><dd>{Number(item.profit_factor).toFixed(2)}</dd></div>
                        <div><dt>胜率</dt><dd>{plainPct(item.win_rate_percent)}</dd></div>
                        <div><dt>平均每笔</dt><dd>{money.format(Number(item.average_trade))}</dd></div>
                        <div><dt>总成本</dt><dd>{money.format(Number(item.total_costs))}</dd></div>
                      </dl>
                    </div>
                  )
                })}
              </div>
            </section>
          )}
          {result.temporal_stability && (
            <section className={`temporal-stability ${result.temporal_stability.passed ? 'passed' : 'failed'}`}>
              <header>
                <div>
                  <span>固定策略 · 时间序列验证</span>
                  <strong>{result.temporal_stability.passed ? '时间稳定性通过' : '时间稳定性不足'}</strong>
                </div>
                <small>
                  {result.temporal_stability.positive_folds}/{result.temporal_stability.evaluable_folds} 个可评估分段为正
                  {' · '}每段至少 {result.temporal_stability.minimum_trades_per_fold} 笔
                  {' · '}无前视
                </small>
              </header>
              <div className="temporal-folds">
                {result.temporal_stability.windows.map((window, index) => (
                  <div key={`${window.label}-${window.data_start}`}>
                    <span>分段 {index + 1}</span>
                    <strong className={Number(window.return_percent) >= 0 ? 'positive' : 'negative'}>
                      {pct(window.return_percent)}
                    </strong>
                    <small>
                      PF {Number(window.profit_factor).toFixed(2)}
                      {' · '}回撤 {plainPct(window.max_drawdown_percent)}
                      {' · '}{window.total_trades} 笔
                    </small>
                    <time>{window.data_start.slice(0, 10)} — {window.data_end.slice(0, 10)}</time>
                  </div>
                ))}
              </div>
              <p>{result.temporal_stability.reasons.join('；')}</p>
            </section>
          )}
          {result.parameter_walk_forward && (
            <section className={`parameter-walk-forward ${result.parameter_walk_forward.passed ? 'passed' : 'failed'}`}>
              <header>
                <div>
                  <span>参数前推 · 只用过去选参</span>
                  <strong>{result.parameter_walk_forward.passed ? '参数选择稳定' : '参数选择证据不足'}</strong>
                </div>
                <small>
                  {result.parameter_walk_forward.positive_steps}/{result.parameter_walk_forward.evaluable_steps} 个后续分段为正
                  {' · '}累计 {pct(result.parameter_walk_forward.cumulative_validation_return_percent)}
                  {' · '}非额外硬风控
                </small>
              </header>
              <div className="parameter-walk-steps">
                {result.parameter_walk_forward.steps.map((step, index) => (
                  <div key={`${step.validation_fold}-${index}`}>
                    <span>前推 {index + 1} · {step.selected_label}</span>
                    <strong className={Number(step.validation_return_percent) >= 0 ? 'positive' : 'negative'}>
                      {pct(step.validation_return_percent)}
                    </strong>
                    <small>训练 {step.training_fold_count} 段 / {step.training_trades} 笔 · 验证 PF {Number(step.validation_profit_factor).toFixed(2)} / {step.validation_trades} 笔</small>
                  </div>
                ))}
              </div>
              <p>{result.parameter_walk_forward.reasons.join('；')}</p>
            </section>
          )}
        </>
      )}

      {view === 'trades' && (
        <div className="backtest-trades data-table">
          <table>
            <thead><tr><th>入场</th><th>退出</th><th>策略</th><th>方向</th><th>入场价</th><th>退出价</th><th>净盈亏</th><th>成本</th><th>退出原因</th></tr></thead>
            <tbody>{result.trades.slice().reverse().slice(0, 50).map((trade, index) => (
              <tr key={`${trade.entry_time}-${trade.exit_time}-${index}`}>
                <td>{new Date(trade.entry_time).toLocaleString('zh-CN')}</td>
                <td>{new Date(trade.exit_time).toLocaleString('zh-CN')}</td>
                <td>{strategyFamilyLabels[trade.algorithm ?? 'unknown'] ?? trade.algorithm}</td>
                <td className={trade.side === 'long' ? 'positive' : 'negative'}>{trade.side === 'long' ? '做多' : '做空'}</td>
                <td>{money.format(Number(trade.entry_price))}</td>
                <td>{money.format(Number(trade.exit_price))}</td>
                <td className={Number(trade.pnl) >= 0 ? 'positive' : 'negative'}>{money.format(Number(trade.pnl))}</td>
                <td>{money.format(Number(trade.fees) + Number(trade.slippage_cost) + Number(trade.funding_cost))}</td>
                <td>{trade.exit_reason}</td>
              </tr>
            ))}</tbody>
          </table>
          {result.trades.length > 50 && <p>当前显示最近 50 笔，共 {result.trades.length} 笔。</p>}
        </div>
      )}

      {view === 'validation' && (
        <div className="backtest-validation">
          <div className={`validation-verdict ${result.robustness_passed ? 'passed' : 'failed'}`}>
            {result.robustness_passed ? <CheckCircle2 /> : <XCircle />}
            <div>
              <strong>{result.robustness_passed ? '稳健性门槛通过' : '当前版本保持 HOLD'}</strong>
              <p>{result.validation_reasons.length ? result.validation_reasons.join('；') : '全部稳健性门槛通过。'}</p>
            </div>
          </div>
          <div className="validation-list">{validationChecks.map(item => (
            <div key={item.label} className={item.passed ? 'passed' : 'failed'}>
              {item.passed ? <CheckCircle2 /> : <XCircle />}
              <span>{item.label}</span>
              <strong>{item.value}</strong>
            </div>
          ))}</div>
          {result.statistical_validation && (
            <section className={`statistical-evidence ${result.statistical_validation.uncertainty_passed ? 'passed' : 'failed'}`}>
              <header>
                <div>
                  <span>MULTIPLE-TESTING CORRECTED</span>
                  <strong>{result.statistical_validation.uncertainty_passed ? '统计不确定性门槛通过' : '搜索后证据不足'}</strong>
                </div>
                <small>{result.statistical_validation.trial_count} 次候选试验 · {result.statistical_validation.observation_count} 个收益观测</small>
              </header>
              <dl>
                <div><dt>Deflated Sharpe 概率</dt><dd>{plainPct(String(Number(result.statistical_validation.deflated_sharpe_probability) * 100))}</dd></div>
                <div><dt>单侧 Sharpe p 值</dt><dd>{Number(result.statistical_validation.one_sided_sharpe_p_value).toFixed(4)}</dd></div>
                <div><dt>Wilson 胜率下界</dt><dd>{plainPct(result.statistical_validation.win_rate_lower_bound_percent)}</dd></div>
                <div><dt>Bootstrap 盈亏比下界</dt><dd>{Number(result.statistical_validation.bootstrap_payoff_lower_bound).toFixed(2)}</dd></div>
                <div><dt>Monte Carlo 95% 回撤</dt><dd>{plainPct(result.statistical_validation.monte_carlo_drawdown_95_percent)}</dd></div>
                <div><dt>FDR / 家族错误控制</dt><dd>{result.statistical_validation.false_discovery_rate_passed ? 'PASS' : 'HOLD'}</dd></div>
              </dl>
              <p>{result.statistical_validation.reasons.join('；')}</p>
            </section>
          )}
        </div>
      )}

      <footer className="backtest-evidence">
        <Activity aria-hidden="true" />
        <span>数据 {result.data_start?.slice(0, 10)} — {result.data_end?.slice(0, 10)} · 指纹 {result.data_fingerprint ?? '—'} · 方向 {result.cost_model.direction_filter === 'long' ? '仅做多' : result.cost_model.direction_filter === 'short' ? '仅做空' : '双向'} · 手续费 {String(result.cost_model.fee_bps ?? '—')} bps · 滑点 {String(result.cost_model.slippage_bps ?? '—')} bps · 历史资金费 {result.cost_model.funding_included ? '已计入' : '不可用'}</span>
      </footer>
    </>
  )
}
