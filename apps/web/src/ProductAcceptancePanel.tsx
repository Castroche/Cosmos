import { useEffect, useState } from 'react'
import { CheckCircle2, RefreshCw, XCircle } from 'lucide-react'
import { getProductAcceptance } from './api'
import type { ProductAcceptanceReport } from './types'

export default function ProductAcceptancePanel() {
  const [report, setReport] = useState<ProductAcceptanceReport | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    void getProductAcceptance()
      .then(value => {
        if (!cancelled) setReport(value)
      })
      .catch(cause => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : '验收报告读取失败')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (!report) {
    return <section className="product-acceptance-panel empty">
      <RefreshCw />
      <p>{error || '正在读取机器验收报告…'}</p>
    </section>
  }

  const strictData = report.evidence?.strict_data_coverage

  return <section className="product-acceptance-panel">
    <header>
      <div>
        <span className="eyebrow">MACHINE-VERIFIED PRODUCT ACCEPTANCE</span>
        <h3>能力、策略认证与盈利证据</h3>
        <p>能力分只衡量系统机制；当前策略与盈利证据必须独立通过严格门槛。</p>
      </div>
      <dl>
        <div><dt>系统能力</dt><dd>{report.capability_score}</dd></div>
        <div><dt>策略认证</dt><dd>{report.strategy_certification_status === 'certified' ? '通过' : 'HOLD'}</dd></div>
        <div><dt>盈利证据</dt><dd>{report.profitability_evidence_score}</dd></div>
      </dl>
    </header>
    <div className="product-acceptance-grid">
      {strictData && <article>
        <div>
          <strong>严格历史数据合同</strong>
          <b className={strictData.passed ? 'positive' : 'negative'}>
            {strictData.score}/100
          </b>
        </div>
        <ul>
          {Object.entries(strictData.feature_scores).map(([feature, score]) => <li key={feature}>
            <span><strong>{feature}</strong><small>仅计入完整通过的市场/周期单元</small></span>
            <b>{score}</b>
          </li>)}
        </ul>
      </article>}
      {report.dimensions.map(dimension => <article key={dimension.code}>
        <div>
          <strong>{dimension.label}</strong>
          <b className={dimension.score === 100 ? 'positive' : 'negative'}>
            {dimension.score}/100
          </b>
        </div>
        <ul>
          {dimension.checks.map(check => <li key={check.code}>
            {check.passed ? <CheckCircle2 className="positive" /> : <XCircle className="negative" />}
            <span><strong>{check.label}</strong><small>{check.evidence}</small></span>
            <b>{check.weight}</b>
          </li>)}
        </ul>
      </article>)}
    </div>
    <footer>
      <span>范围 {report.scope}</span>
      <span>生成于 {new Date(report.generated_at).toLocaleString('zh-CN')}</span>
      <strong>不承诺未来盈利</strong>
    </footer>
  </section>
}
