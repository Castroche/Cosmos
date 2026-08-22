import { TickMarkType, type Time } from 'lightweight-charts'

export type ChartTimeZone = 'local' | 'utc'

const LOCALE = 'zh-CN'
const FORMATTER_CACHE = new Map<string, Intl.DateTimeFormat>()

export const LOCAL_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local'

function chartTimestamp(time: Time): number {
  if (typeof time === 'number') return time
  if (typeof time === 'string') return Math.floor(Date.parse(time) / 1000)
  return Math.floor(Date.UTC(time.year, time.month - 1, time.day) / 1000)
}

function formatter(
  zone: ChartTimeZone,
  options: Intl.DateTimeFormatOptions,
) {
  const key = `${zone}:${JSON.stringify(options)}`
  const cached = FORMATTER_CACHE.get(key)
  if (cached) return cached
  const value = new Intl.DateTimeFormat(LOCALE, {
    ...options,
    ...(zone === 'utc' ? { timeZone: 'UTC' } : {}),
  })
  FORMATTER_CACHE.set(key, value)
  return value
}

export function formatChartDateTime(time: Time | number, zone: ChartTimeZone) {
  const timestamp = typeof time === 'number' ? time : chartTimestamp(time)
  return formatter(zone, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(new Date(timestamp * 1000)).replaceAll('/', '-')
}

export function formatChartTick(time: Time, tickMarkType: TickMarkType, zone: ChartTimeZone) {
  const date = new Date(chartTimestamp(time) * 1000)
  if (tickMarkType === TickMarkType.Year) {
    return formatter(zone, { year: 'numeric' }).format(date)
  }
  if (tickMarkType === TickMarkType.Month) {
    return formatter(zone, { month: 'short' }).format(date)
  }
  if (tickMarkType === TickMarkType.DayOfMonth) {
    return formatter(zone, { month: '2-digit', day: '2-digit' }).format(date).replaceAll('/', '-')
  }
  return formatter(zone, {
    hour: '2-digit',
    minute: '2-digit',
    ...(tickMarkType === TickMarkType.TimeWithSeconds ? { second: '2-digit' as const } : {}),
    hourCycle: 'h23',
  }).format(date)
}

export function localUtcOffsetLabel(timestamp = Date.now()) {
  const offsetMinutes = -new Date(timestamp).getTimezoneOffset()
  const sign = offsetMinutes >= 0 ? '+' : '-'
  const absolute = Math.abs(offsetMinutes)
  const hours = String(Math.floor(absolute / 60)).padStart(2, '0')
  const minutes = String(absolute % 60).padStart(2, '0')
  return `UTC${sign}${hours}:${minutes}`
}

export function chartTimeZoneLabel(zone: ChartTimeZone) {
  return zone === 'utc' ? 'UTC' : `${LOCAL_TIME_ZONE} (${localUtcOffsetLabel()})`
}
