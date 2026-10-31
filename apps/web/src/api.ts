import type {
  AIConfigurationUpdate,
  AIConfigurationView,
  AIUsageReport,
  AIProvider,
  AIProviderTestResult,
  AIProposalResponse,
  ConfigurationUpdate,
  ConfigurationView,
  ConnectionTestResult,
  BacktestResult,
  DashboardData,
  Plan,
  MarketStreamEvent,
  MarketWindow,
  StrategyGenerateRequest,
  BacktestJob,
  DriftReport,
  MLChallengerReport,
  PromotionDecision,
  ResearchDataset,
  ResearchCampaignReport,
  ResearchCampaignJob,
  ProductAcceptanceReport,
  PairBacktestReport,
  PairStrategySpec,
  StrategyCandidate,
  StrategyMarketFit,
  RiskControlState,
  StrategyLibraryPage,
  StrategyLibraryItem,
  AuditEventPage,
  ReviewOverview,
  ReviewEvidenceItem,
  ReviewJobView,
  Crypto5mDataContract,
  SandboxAdmissionView,
  DevelopmentAuditView,
  AIHealth,
  HumanLiveGrantPreviewV2,
  HumanLiveGrantV2,
  MarketStateV2,
  TradingIntelligenceOverview,
  TradeHistoryV2,
  EntryOrderRuntime,
  PositionRuntimeV2,
  WakeConditionV2,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function api<T>(path: string, init?: RequestInit, timeout = 15_000): Promise<T> {
  const signal = init?.signal
    ? AbortSignal.any([init.signal, AbortSignal.timeout(timeout)])
    : AbortSignal.timeout(timeout)
  const response = await fetch(path, { ...init, signal, credentials: 'same-origin' })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try {
      const body = await response.json() as { detail?: string | { message?: string } }
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail?.message) message = body.detail.message
    } catch {
      // The HTTP status remains the authoritative fallback.
    }
    throw new ApiError(message, response.status)
  }
  return response.json() as Promise<T>
}

const jsonHeaders = { 'Content-Type': 'application/json' }

export function getDashboard(): Promise<DashboardData> {
  return api<DashboardData>('/api/dashboard')
}

export function getMarketWindow(
  symbol: string,
  timeframe: string,
  limit = 300,
): Promise<MarketWindow> {
  const query = new URLSearchParams({ symbol, timeframe, limit: String(limit) })
  return api<MarketWindow>(`/api/market?${query}`)
}

export function getSettings(): Promise<ConfigurationView> {
  return api<ConfigurationView>('/api/settings')
}

export function getRiskState(): Promise<RiskControlState> {
  return api<RiskControlState>('/api/risk/state')
}

export function getAISettings(): Promise<AIConfigurationView> {
  return api<AIConfigurationView>('/api/ai/settings')
}

export function getAIUsage(days = 30, provider?: AIProvider): Promise<AIUsageReport> {
  const query = new URLSearchParams({ days: String(days) })
  if (provider) query.set('provider', provider)
  return api<AIUsageReport>(`/api/ai/usage?${query}`)
}

export function getAIHealth(): Promise<AIHealth> {
  return api<AIHealth>('/api/ai/health')
}

export function saveAISettings(update: AIConfigurationUpdate): Promise<AIConfigurationView> {
  return api<AIConfigurationView>('/api/ai/settings', {
    method: 'PUT', headers: jsonHeaders, body: JSON.stringify(update),
  })
}

export function testAIProvider(provider: AIProvider): Promise<AIProviderTestResult> {
  return api<AIProviderTestResult>(`/api/ai/providers/${provider}/test`, {
    method: 'POST',
  }, 180_000)
}

export function discoverAIModels(provider: AIProvider): Promise<string[]> {
  return api<string[]>(`/api/ai/providers/${provider}/discover-models`, {
    method: 'POST',
  }, 60_000)
}

export function createAIResearchCandidate(request: {
  hypothesis: string
  family: StrategyGenerateRequest['strategy_kind']
  symbol: string
  timeframe: string
}): Promise<AIProposalResponse> {
  return api<AIProposalResponse>('/api/research/ai-candidates', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(request),
  }, 180_000)
}

export function saveSettings(update: ConfigurationUpdate): Promise<ConfigurationView> {
  return api<ConfigurationView>('/api/settings', {
    method: 'PUT', headers: jsonHeaders, body: JSON.stringify(update),
  })
}

export function testConnection(update: ConfigurationUpdate): Promise<ConnectionTestResult> {
  return api<ConnectionTestResult>('/api/settings/test-connection', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(update),
  })
}

export function commissionProtection(symbol: string): Promise<NonNullable<ConfigurationView['commissioning']>> {
  return api<NonNullable<ConfigurationView['commissioning']>>('/api/settings/commission-protection', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ confirmation: `TEST PROTECTED ORDER ${symbol}` }),
  }, 45_000)
}

export function commissionAgentPositionManagement(symbol: string): Promise<NonNullable<ConfigurationView['commissioning']>> {
  return api<NonNullable<ConfigurationView['commissioning']>>('/api/settings/commission-agent-position-management', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ confirmation: `TEST AGENT REDUCTION ${symbol}` }),
  }, 180_000)
}

export function generateStrategy(request: StrategyGenerateRequest): Promise<Plan> {
  return api<Plan>('/api/strategies/generate', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(request),
  })
}

export function backtestStrategy(
  plan: Plan,
  onStatus?: (job: BacktestJob) => void,
): Promise<BacktestResult> {
  return backtestStrategyByPlanId(plan.id, onStatus)
}

export function backtestStrategyByPlanId(
  planId: string,
  onStatus?: (job: BacktestJob) => void,
): Promise<BacktestResult> {
  return queueBacktest(planId).then(job => {
    onStatus?.(job)
    return waitForBacktest(job, onStatus)
  })
}

function queueBacktest(planId: string): Promise<BacktestJob> {
  return api<BacktestJob>(`/api/research/backtests/${encodeURIComponent(planId)}`, { method: 'POST' })
}

async function waitForBacktest(
  job: BacktestJob,
  onStatus?: (job: BacktestJob) => void,
): Promise<BacktestResult> {
  const deadline = Date.now() + 120_000
  let current = job
  while (current.status === 'queued' || current.status === 'running') {
    if (Date.now() >= deadline) throw new Error('回测任务超时，请稍后重新打开策略')
    await new Promise(resolve => window.setTimeout(resolve, 500))
    current = await api<BacktestJob>(`/api/research/backtests/${current.id}`)
    onStatus?.(current)
  }
  if (current.status === 'failed' || !current.result) {
    throw new Error(current.error || '回测任务失败')
  }
  return current.result
}

export function getResearchCandidates(): Promise<StrategyCandidate[]> {
  return api<StrategyCandidate[]>('/api/research/candidates')
}

export function getResearchDatasets(): Promise<ResearchDataset[]> {
  return api<ResearchDataset[]>('/api/research/datasets')
}

export function getResearchCampaigns(): Promise<ResearchCampaignReport[]> {
  return api<ResearchCampaignReport[]>('/api/research/campaigns')
}

export function getProductAcceptance(): Promise<ProductAcceptanceReport> {
  return api<ProductAcceptanceReport>('/api/acceptance/product')
}

export function getStrategyLibrarySummary(): Promise<{
  entry_count: number
  archetype_count: number
}> {
  return api('/api/research/strategy-library')
}

export function getStrategySpecContract(): Promise<{
  schema: Record<string, unknown>
  defaults: Record<string, Record<string, unknown>>
}> {
  return api('/api/research/strategy-spec')
}

export function runResearchCampaign(
  datasetIds: string[],
  blindDatasetIds: string[] = [],
  candidateIds: string[] = [],
): Promise<ResearchCampaignReport> {
  return api<ResearchCampaignReport>('/api/research/campaigns/run', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({
      dataset_ids: datasetIds,
      blind_dataset_ids: blindDatasetIds,
      candidate_ids: candidateIds,
      max_backtests: 480,
    }),
  }, 900_000)
}

export function queueResearchCampaign(
  datasetIds: string[],
  blindDatasetIds: string[] = [],
  candidateIds: string[] = [],
): Promise<ResearchCampaignJob> {
  return api<ResearchCampaignJob>('/api/research/campaign-jobs', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({
      dataset_ids: datasetIds,
      blind_dataset_ids: blindDatasetIds,
      candidate_ids: candidateIds,
      max_backtests: 480,
    }),
  }, 30_000)
}

export function getResearchCampaignJob(jobId: string): Promise<ResearchCampaignJob> {
  return api<ResearchCampaignJob>(
    `/api/research/campaign-jobs/${encodeURIComponent(jobId)}`,
  )
}

export function getResearchCampaignJobs(): Promise<ResearchCampaignJob[]> {
  return api<ResearchCampaignJob[]>('/api/research/campaign-jobs?limit=20')
}

export function getResearchCampaign(campaignId: string): Promise<ResearchCampaignReport> {
  return api<ResearchCampaignReport>(
    `/api/research/campaigns/${encodeURIComponent(campaignId)}`,
  )
}

export function getRelativeValueLibrary(): Promise<{
  execution_enabled: false
  source: string
  profitability_claim: false
  entries: Array<{
    id: string
    spec: PairStrategySpec
    strategy_fingerprint: string
  }>
}> {
  return api('/api/research/relative-value/library')
}

export function runRelativeValueResearch(
  datasetAId: string,
  datasetBId: string,
): Promise<PairBacktestReport[]> {
  return api<PairBacktestReport[]>(
    '/api/research/relative-value/backtests',
    {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({
        dataset_a_id: datasetAId,
        dataset_b_id: datasetBId,
      }),
    },
    300_000,
  )
}

export function getRelativeValueReports(): Promise<PairBacktestReport[]> {
  return api<PairBacktestReport[]>(
    '/api/research/relative-value/backtests?limit=20',
  )
}

export function reviewStrategyGovernance(): Promise<{
  reviewed: number
  retired_candidate_ids: string[]
  health: Array<Record<string, unknown>>
}> {
  return api('/api/research/governance/review', { method: 'POST' }, 120_000)
}

export function evaluateResearchCandidate(candidateId: string, datasetId: string): Promise<StrategyCandidate> {
  const query = new URLSearchParams({ dataset_id: datasetId })
  return api<StrategyCandidate>(`/api/research/candidates/${encodeURIComponent(candidateId)}/evaluate?${query}`, {
    method: 'POST',
  }, 180_000)
}

export function promoteResearchCandidate(candidate: StrategyCandidate): Promise<PromotionDecision> {
  return api<PromotionDecision>(`/api/research/candidates/${encodeURIComponent(candidate.id)}/promote`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ confirmation: `PROMOTE ${candidate.spec_fingerprint}` }),
  }, 180_000)
}

export function getResearchDrift(): Promise<DriftReport> {
  return api<DriftReport>('/api/research/drift')
}

export function createMLChallenger(): Promise<MLChallengerReport> {
  return api<MLChallengerReport>('/api/research/ml-challenger', { method: 'POST' }, 60_000)
}

export function getLatestBacktest(plan: Plan): Promise<BacktestResult | null> {
  return getLatestBacktestByPlanId(plan.id)
}

export function getLatestBacktestByPlanId(planId: string): Promise<BacktestResult | null> {
  return api<BacktestResult | null>(`/api/strategies/${encodeURIComponent(planId)}/backtest`)
}

export function getStrategyMarketFit(plan: Plan): Promise<StrategyMarketFit> {
  return api<StrategyMarketFit>(`/api/strategies/${plan.id}/market-fit`)
}

export function openMarketStream(
  symbol: string,
  timeframe: string,
  onMarket: (event: MarketStreamEvent) => void,
  onState: (state: 'connected' | 'disconnected' | 'error') => void,
): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const query = new URLSearchParams({ symbol, timeframe })
  const socket = new WebSocket(`${protocol}//${window.location.host}/api/stream/market?${query}`)
  socket.addEventListener('open', () => onState('connected'))
  socket.addEventListener('message', message => {
    const event = JSON.parse(String(message.data)) as MarketStreamEvent | { type: string }
    if (event.type === 'market') onMarket(event as MarketStreamEvent)
  })
  socket.addEventListener('error', () => onState('error'))
  socket.addEventListener('close', () => onState('disconnected'))
  return socket
}

export async function cancelOrder(orderId: string): Promise<void> {
  await api(`/api/orders/${encodeURIComponent(orderId)}/cancel`, { method: 'POST' })
}

export async function haltTrading(reason: string): Promise<void> {
  await api('/api/risk/halt', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify({ reason }),
  })
}

export async function resumeTrading(): Promise<void> {
  await api('/api/risk/resume', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ confirmation: 'RESUME TRADING' }),
  })
}

export function getReviewOverview(): Promise<ReviewOverview> {
  return api<ReviewOverview>('/api/review/overview', undefined, 30_000)
}

export function getReviewEvidence(): Promise<ReviewEvidenceItem[]> {
  return api<ReviewEvidenceItem[]>('/api/review/evidence?limit=500', undefined, 30_000)
}

export function getReviewJobs(): Promise<ReviewJobView[]> {
  return api<ReviewJobView[]>('/api/review/jobs', undefined, 30_000)
}

export function retryReviewJob(jobId: string): Promise<ReviewJobView> {
  return api<ReviewJobView>(`/api/review/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: 'POST',
  }, 30_000)
}

export function reconcileReviewBacklog(): Promise<{
  queued: number
  migrated: number
  contract_rejected: number
  existing: number
  total_candidates: number
  legacy: number
  worker_status: string
  worker_activity: string
}> {
  return api('/api/review/backlog/reconcile', { method: 'POST' }, 60_000)
}

export function getCrypto5mDataContract(): Promise<Crypto5mDataContract> {
  return api<Crypto5mDataContract>('/api/research/data-contracts/crypto-5m-v1', undefined, 30_000)
}

export function getSandboxAdmissions(): Promise<SandboxAdmissionView[]> {
  return api<SandboxAdmissionView[]>('/api/research/sandbox-admissions', undefined, 30_000)
}

export function getDevelopmentAudits(): Promise<DevelopmentAuditView[]> {
  return api<DevelopmentAuditView[]>('/api/research/development-audits', undefined, 30_000)
}

export function getStrategyLibrary(params: {
  origin?: string
  family?: string
  status?: string
  includeHidden?: boolean
  search?: string
} = {}): Promise<StrategyLibraryPage> {
  const query = new URLSearchParams()
  if (params.origin) query.set('origin', params.origin)
  if (params.family) query.set('family', params.family)
  if (params.status) query.set('status', params.status)
  if (params.includeHidden) query.set('include_hidden', 'true')
  if (params.search) query.set('search', params.search)
  const suffix = query.size ? `?${query}` : ''
  return api<StrategyLibraryPage>(`/api/strategy-library${suffix}`, undefined, 30_000)
}

export function createPersonalStrategy(request: {
  name: string
  description: string
  strategy_spec: Record<string, unknown>
  origin_ref?: string
}): Promise<StrategyLibraryItem> {
  return api<StrategyLibraryItem>('/api/strategy-library', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(request),
  })
}

export function updatePersonalStrategy(
  itemId: string,
  request: { name?: string; description?: string; strategy_spec?: Record<string, unknown> },
): Promise<StrategyLibraryItem> {
  return api<StrategyLibraryItem>(`/api/strategy-library/${encodeURIComponent(itemId)}`, {
    method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(request),
  })
}

export function archiveStrategyLibraryItem(itemId: string): Promise<StrategyLibraryItem> {
  return api<StrategyLibraryItem>(`/api/strategy-library/${encodeURIComponent(itemId)}/archive`, {
    method: 'POST',
  })
}

export function restoreStrategyLibraryItem(itemId: string): Promise<StrategyLibraryItem> {
  return api<StrategyLibraryItem>(`/api/strategy-library/${encodeURIComponent(itemId)}/restore`, {
    method: 'POST',
  })
}

export async function deletePersonalStrategy(itemId: string): Promise<void> {
  await api(`/api/strategy-library/${encodeURIComponent(itemId)}`, { method: 'DELETE' })
}

export function getAuditEvents(params: {
  limit?: number
  cursor?: number
  category?: string
  severity?: string
  search?: string
} = {}): Promise<AuditEventPage> {
  const query = new URLSearchParams({ limit: String(params.limit ?? 50) })
  if (params.cursor) query.set('cursor', String(params.cursor))
  if (params.category) query.set('category', params.category)
  if (params.severity) query.set('severity', params.severity)
  if (params.search) query.set('search', params.search)
  return api<AuditEventPage>(`/api/audit/events?${query}`)
}

export function latestEntryOrders(entries: EntryOrderRuntime[] = []): EntryOrderRuntime[] {
  const latest = new Map<string, EntryOrderRuntime>()
  for (const entry of entries) {
    const previous = latest.get(entry.entry_id)
    const newer = previous && entry.version != null && previous.version != null && entry.version !== previous.version
      ? entry.version > previous.version
      : !previous || Date.parse(entry.updated_at) >= Date.parse(previous.updated_at)
    if (newer) {
      latest.set(entry.entry_id, entry)
    }
  }
  return [...latest.values()].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
}

export function isPendingEntry(entry: EntryOrderRuntime): boolean {
  return ['SUBMITTING', 'OPEN', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNCERTAIN'].includes(entry.status)
}

export function isActualPosition(position: PositionRuntimeV2): boolean {
  return !['FLAT', 'CLOSED', 'OPENING'].includes(position.state) && Number(position.remaining_quantity) > 0
}

export function entryStatusLabel(status: EntryOrderRuntime['status']): string {
  const labels: Record<EntryOrderRuntime['status'], string> = {
    SUBMITTING: '提交中', OPEN: '待成交', PARTIALLY_FILLED: '部分成交', FILLED: '已成交',
    CANCEL_PENDING: '撤单确认中', CANCELLED: '已撤单', REJECTED: '已拒绝',
    UNCERTAIN: '状态待核对', SHADOW_RECORDED: '影子记录（未下单）',
  }
  return labels[status] ?? status
}

export function describeWakeCondition(condition: WakeConditionV2): string {
  const value = String(condition.value ?? '—')
  const range = `${condition.lower ?? '—'} – ${condition.upper ?? '—'}`
  const metric = condition.path ?? '指标'
  const descriptions: Record<string, string> = {
    PRICE_CROSSES_ABOVE: `价格向上突破 ${value}`,
    PRICE_CROSSES_BELOW: `价格向下跌破 ${value}`,
    PRICE_IN_ZONE: `价格进入 ${range}`,
    CANDLE_CLOSE_ABOVE: `${condition.timeframe ?? ''} K 线收盘价高于 ${value}`,
    CANDLE_CLOSE_BELOW: `${condition.timeframe ?? ''} K 线收盘价低于 ${value}`,
    METRIC_GT: `${metric} > ${value}`, METRIC_LT: `${metric} < ${value}`,
    METRIC_CROSSES_ABOVE: `${metric} 向上突破 ${value}`,
    METRIC_CROSSES_BELOW: `${metric} 向下跌破 ${value}`,
    METRIC_IN_RANGE: `${metric} 进入 ${range}`,
    STRUCTURE_CHANGED: `市场结构变化${condition.value == null ? '' : `：${value}`}`,
    REGIME_CHANGED: `行情状态变化${condition.value == null ? '' : `：${value}`}`,
    EXPIRES_AT: `到期 ${condition.expires_at ?? value}`,
    ORDER_EVENT: `订单事件：${value}`, POSITION_EVENT: `持仓事件：${value}`,
    EXTERNAL_EVENT: `外部事件：${value}`,
  }
  if (condition.kind === 'ALL' || condition.kind === 'ANY') {
    return `${condition.kind === 'ALL' ? '全部满足' : '任一满足'}（${condition.children.map(describeWakeCondition).join('；')}）`
  }
  return descriptions[condition.kind] ?? `${condition.kind} ${metric} ${value}`
}

export function wakeActionLabel(action: WakeConditionV2['action']): string {
  return { RECALL_LLM: '重新评估策略', EXECUTE_ENTRY: '执行入场', CANCEL_PLAN: '取消计划', EXIT_POSITION: '退出持仓' }[action]
}

export async function getTradingIntelligenceOverview(): Promise<TradingIntelligenceOverview> {
  const overview = await api<TradingIntelligenceOverview>('/api/trading-intelligence/overview')
  // Each response replaces the snapshot, including cleared reasons and removed orders.
  return { ...overview, entry_orders: overview.entry_orders == null ? undefined : latestEntryOrders(overview.entry_orders) }
}

export function getTradingIntelligenceTradeHistory(
  symbol: MarketStateV2['symbol'] | null,
  days: number,
): Promise<TradeHistoryV2> {
  const query = new URLSearchParams({ days: String(days), limit: '1000' })
  if (symbol) query.set('symbol', symbol)
  return api<TradeHistoryV2>(`/api/trading-intelligence/trade-history?${query}`)
}

export function ingestTradingIntelligenceMarketState(state: MarketStateV2): Promise<MarketStateV2> {
  return api<MarketStateV2>('/api/trading-intelligence/market-state', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(state),
  })
}

export function refreshTradingIntelligenceMarketState(
  symbol: MarketStateV2['symbol'],
): Promise<MarketStateV2> {
  return api<MarketStateV2>(`/api/trading-intelligence/market-state/${encodeURIComponent(symbol)}/refresh`, {
    method: 'POST',
  }, 120_000)
}

export type AutomationCommissioningV2 = {
  eligible: boolean
  blockers: string[]
  mode?: 'SHADOW' | 'OKX_DEMO'
  equity?: string
  open_position_count?: number
  open_order_count?: number
  unknown_position_count?: number
  unknown_order_count?: number
  protection?: Record<string, boolean>
}

export function commissionTradingIntelligenceDemo(
  symbols: MarketStateV2['symbol'][],
): Promise<AutomationCommissioningV2> {
  return api<AutomationCommissioningV2>('/api/trading-intelligence/automation/commission', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify({ mode: 'OKX_DEMO', symbols }),
  }, 120_000)
}

export function startTradingIntelligenceDemo(
  symbols: MarketStateV2['symbol'][],
): Promise<TradingIntelligenceOverview['automation']> {
  return api<TradingIntelligenceOverview['automation']>('/api/trading-intelligence/automation/start', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify({ mode: 'OKX_DEMO', symbols }),
  }, 180_000)
}

export function stopTradingIntelligenceAutomation(): Promise<TradingIntelligenceOverview['automation']> {
  return api<TradingIntelligenceOverview['automation']>('/api/trading-intelligence/automation/stop', {
    method: 'POST',
  })
}

export type LiveGrantDraftV2 = {
  symbols: MarketStateV2['symbol'][]
  max_risk_per_trade: string
  max_portfolio_risk: string
  max_leverage: string
  max_daily_drawdown: string
  max_open_positions: number
  valid_hours: number
}

export function previewHumanLiveGrantV2(
  request: LiveGrantDraftV2,
): Promise<HumanLiveGrantPreviewV2> {
  return api<HumanLiveGrantPreviewV2>('/api/trading-intelligence/live-grants/preview', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(request),
  })
}

export function issueHumanLiveGrantV2(
  request: LiveGrantDraftV2 & {
    configuration_fingerprint: string
    risk_confirmation_one: boolean
    risk_confirmation_two: boolean
  },
): Promise<HumanLiveGrantV2> {
  return api<HumanLiveGrantV2>('/api/trading-intelligence/live-grants', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(request),
  })
}

export function revokeHumanLiveGrantV2(grantId: string): Promise<HumanLiveGrantV2> {
  return api<HumanLiveGrantV2>(`/api/trading-intelligence/live-grants/${encodeURIComponent(grantId)}`, {
    method: 'DELETE',
  })
}

export function getLearningData(signal?: AbortSignal) {
  const root = '/api/trading-intelligence/learning'
  return Promise.all([
    api<import('./types').LearningEvidence>(`${root}/snapshot`, { signal }),
    api<import('./types').LearningSettings>(`${root}/settings`, { signal }),
    api<import('./types').LearningPage<import('./types').LearningVersion>>(`${root}/versions`, { signal }),
    api<import('./types').LearningSummary>(`${root}/summary`, { signal }),
    api<import('./types').LearningPage<import('./types').LearningComparison>>(`${root}/comparisons`, { signal }),
  ])
}

export function updateLearningSettings(settings: import('./types').LearningSettings) {
  return api<import('./types').LearningSettings>('/api/trading-intelligence/learning/settings', {
    method: 'PUT', headers: jsonHeaders, body: JSON.stringify(settings),
  })
}
