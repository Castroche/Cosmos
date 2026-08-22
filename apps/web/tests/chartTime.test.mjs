import assert from 'node:assert/strict'
import test from 'node:test'
import { TickMarkType } from 'lightweight-charts'

process.env.TZ = 'Asia/Shanghai'

const {
  formatChartDateTime,
  formatChartTick,
  localUtcOffsetLabel,
} = await import('../src/chartTime.ts')

const CANDLE_TIME = Date.parse('2026-07-14T11:00:00Z') / 1000

test('formats the same candle instant in the selected chart time zone', () => {
  assert.equal(formatChartDateTime(CANDLE_TIME, 'local'), '2026-07-14 19:00')
  assert.equal(formatChartDateTime(CANDLE_TIME, 'utc'), '2026-07-14 11:00')
})

test('uses the selected time zone for chart axis ticks', () => {
  assert.equal(formatChartTick(CANDLE_TIME, TickMarkType.Time, 'local'), '19:00')
  assert.equal(formatChartTick(CANDLE_TIME, TickMarkType.Time, 'utc'), '11:00')
  assert.equal(formatChartTick(CANDLE_TIME, TickMarkType.DayOfMonth, 'local'), '07-14')
})

test('reports the browser local UTC offset explicitly', () => {
  assert.equal(localUtcOffsetLabel(CANDLE_TIME * 1000), 'UTC+08:00')
})
