export const CHART_TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '1w'] as const

export const PRIMARY_CHART_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d'] as const

export function timeframeLabel(value: string) {
  return value.replace('h', 'H').replace('d', 'D').replace('w', 'W')
}
