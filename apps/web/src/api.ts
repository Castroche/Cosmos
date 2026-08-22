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
  AgentControlState,
  AgentForwardPerformance,
  AgentReadiness,
  AgentGoal,
  AgentRunTrace,
  AgentPolicy,
  RiskControlState,
} from './types'

async function api<T>(path: string, init?: RequestInit, timeout = 15_000): Promise<T> {
  const response = await fetch(path, { ...init, signal: AbortSignal.timeout(timeout) })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try {
      const body = await response.json() as { detail?: string | { message?: string } }
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail?.message) message = body.detail.message
    } catch {
      // The HTTP status remains the authoritative fallback.
    }
    throw new Error(message)
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

export function approveStrategy(plan: Plan): Promise<Plan> {
  return api<Plan>(`/api/strategies/${plan.id}/approve`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ confirmation: `APPROVE ${plan.fingerprint}` }),
  })
}

export function activateStrategy(plan: Plan): Promise<Plan> {
  return api<Plan>(`/api/strategies/${plan.id}/activate`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ confirmation: `ACTIVATE ${plan.symbol}` }),
  })
}

export async function executeStrategy(plan: Plan): Promise<void> {
  await api(`/api/strategies/${plan.id}/execute`, { method: 'POST' })
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

export function getAgentState(): Promise<AgentControlState> {
  return api<AgentControlState>('/api/agent/state')
}

export function getAgentReadiness(): Promise<AgentReadiness> {
  return api<AgentReadiness>('/api/agent/readiness')
}

export function getAgentForwardPerformance(): Promise<AgentForwardPerformance> {
  return api<AgentForwardPerformance>('/api/agent/performance?autonomy_mode=sandbox')
}

export function getAgentRunTrace(runId: string): Promise<AgentRunTrace> {
  return api<AgentRunTrace>(`/api/agent/runs/${encodeURIComponent(runId)}`)
}

export function saveAgentPolicy(policy: AgentPolicy, confirmation: string): Promise<AgentPolicy> {
  return api<AgentPolicy>(`/api/agent/policies/${encodeURIComponent(policy.id)}`, {
    method: 'PUT', headers: jsonHeaders, body: JSON.stringify({ policy, confirmation }),
  })
}

export function createAgentGoal(goal: Omit<AgentGoal, 'id' | 'status' | 'created_at' | 'updated_at'>): Promise<AgentGoal> {
  return api<AgentGoal>('/api/agent/goals', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(goal),
  })
}

export function updateAgentGoalScope(
  goal: AgentGoal,
  update: Pick<AgentGoal, 'objective' | 'symbols' | 'timeframes' | 'poll_seconds'>,
): Promise<AgentGoal> {
  return api<AgentGoal>(`/api/agent/goals/${encodeURIComponent(goal.id)}`, {
    method: 'PUT', headers: jsonHeaders,
    body: JSON.stringify({ ...update, confirmation: `UPDATE AGENT GOAL ${goal.id}` }),
  })
}

export function setAgentGoalStatus(goal: AgentGoal, status: 'active' | 'paused'): Promise<AgentGoal> {
  const verb = status === 'active' ? 'RESUME' : 'PAUSE'
  return api<AgentGoal>(`/api/agent/goals/${goal.id}/status`, {
    method: 'PUT', headers: jsonHeaders,
    body: JSON.stringify({ status, confirmation: `${verb} AGENT ${goal.id}` }),
  })
}

export async function triggerAgentGoal(goalId: string): Promise<void> {
  await api(`/api/agent/goals/${goalId}/trigger`, {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify({ event_type: 'manual_triggered', payload: {} }),
  })
}

export async function runAgentOnce(goalId: string): Promise<void> {
  await api(`/api/agent/goals/${goalId}/run-once`, { method: 'POST' }, 180_000)
}
