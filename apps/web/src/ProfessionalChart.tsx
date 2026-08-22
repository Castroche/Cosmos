import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { BetweenHorizontalStart, CalendarRange, ChartNoAxesCombined, ChevronDown, Clock3, Fullscreen, LoaderCircle, Minimize2, MousePointer2, MoveHorizontal, MoveVertical, ScanLine, Square, SquareFunction, Trash2, TrendingUp, Undo2, X } from 'lucide-react'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  HistogramSeries,
  LineSeries,
  LineStyle,
  PriceScaleMode,
  TickMarkType,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { Candle, Plan } from './types'
import { PRIMARY_CHART_TIMEFRAMES, timeframeLabel } from './timeframes'
import { chartTimeZoneLabel, formatChartDateTime, formatChartTick, localUtcOffsetLabel, type ChartTimeZone } from './chartTime'

type DrawingTool = 'cursor' | 'trend' | 'horizontal' | 'vertical' | 'rectangle' | 'fibonacci'
type IndicatorKey = 'volume' | 'ema20' | 'ema50' | 'sma20' | 'bollinger' | 'vwap' | 'rsi' | 'macd'
type Point = { time: number; price: number }
type Drawing = { id: string; tool: Exclude<DrawingTool, 'cursor'>; points: Point[] }
type Ohlcv = { open: number; high: number; low: number; close: number; volume: number; time: number }
type ScaleMode = 'normal' | 'log' | 'percent'
type VisibleBars = 50 | 100 | 200 | 'all'
type LineIndicatorKey = 'ema20' | 'ema50' | 'sma20' | 'vwap' | 'bb-upper' | 'bb-basis' | 'bb-lower' | 'rsi' | 'macd' | 'macd-signal'
type HistogramIndicatorKey = 'volume' | 'macd-histogram'

const DEFAULT_INDICATORS: Record<IndicatorKey, boolean> = {
  volume: true,
  ema20: true,
  ema50: true,
  sma20: false,
  bollinger: false,
  vwap: false,
  rsi: false,
  macd: false,
}

const INDICATOR_LABELS: Array<{ key: IndicatorKey; label: string; detail: string }> = [
  { key: 'volume', label: '成交量', detail: 'VOL' },
  { key: 'ema20', label: '指数均线', detail: 'EMA 20' },
  { key: 'ema50', label: '指数均线', detail: 'EMA 50' },
  { key: 'sma20', label: '简单均线', detail: 'SMA 20' },
  { key: 'bollinger', label: '布林带', detail: 'BB 20 2' },
  { key: 'vwap', label: '量价均线', detail: 'VWAP' },
  { key: 'rsi', label: '相对强弱', detail: 'RSI 14' },
  { key: 'macd', label: '趋势动量', detail: 'MACD 12 26 9' },
]

const SCALE_MODES: Array<{ key: ScaleMode; label: string; mode: PriceScaleMode }> = [
  { key: 'normal', label: '普通坐标', mode: PriceScaleMode.Normal },
  { key: 'log', label: '对数坐标', mode: PriceScaleMode.Logarithmic },
  { key: 'percent', label: '百分比', mode: PriceScaleMode.Percentage },
]

const VISIBLE_BAR_OPTIONS: Array<{ value: VisibleBars; label: string }> = [
  { value: 50, label: '最近 50 根' },
  { value: 100, label: '最近 100 根' },
  { value: 200, label: '最近 200 根' },
  { value: 'all', label: '全部数据' },
]

const TOOL_LABELS: Array<{ key: DrawingTool; label: string }> = [
  { key: 'cursor', label: '光标 / 平移' },
  { key: 'trend', label: '趋势线' },
  { key: 'horizontal', label: '水平线' },
  { key: 'vertical', label: '垂直线' },
  { key: 'rectangle', label: '矩形区间' },
  { key: 'fibonacci', label: '斐波那契回撤' },
]

function ToolIcon({ tool }: { tool: DrawingTool }) {
  const icons = {
    cursor: MousePointer2,
    trend: TrendingUp,
    horizontal: MoveHorizontal,
    vertical: MoveVertical,
    rectangle: Square,
    fibonacci: BetweenHorizontalStart,
  }
  const Icon = icons[tool]
  return <Icon aria-hidden="true" />
}

function toTime(timestamp: string): UTCTimestamp {
  return Math.floor(Date.parse(timestamp) / 1000) as UTCTimestamp
}

function uniqueCandles(candles: Candle[]) {
  const byTime = new Map<number, Candle>()
  candles.forEach(candle => byTime.set(Number(toTime(candle.timestamp)), candle))
  return [...byTime.entries()].sort((a, b) => a[0] - b[0])
}

function priceData(candles: Candle[]): CandlestickData<Time>[] {
  return uniqueCandles(candles).map(([time, candle]) => ({
    time: time as UTCTimestamp,
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
  }))
}

function lineData(values: Array<number | null>, candles: Candle[]): LineData<Time>[] {
  const rows = uniqueCandles(candles)
  return rows.flatMap(([time], index) => values[index] == null ? [] : [{ time: time as UTCTimestamp, value: values[index] as number }])
}

function ema(values: number[], period: number) {
  const multiplier = 2 / (period + 1)
  let current = values[0] ?? 0
  return values.map((value, index) => {
    current = index === 0 ? value : value * multiplier + current * (1 - multiplier)
    return index < period - 1 ? null : current
  })
}

function sma(values: number[], period: number) {
  let sum = 0
  return values.map((value, index) => {
    sum += value
    if (index >= period) sum -= values[index - period]
    return index < period - 1 ? null : sum / period
  })
}

function bollinger(values: number[], period = 20, deviations = 2) {
  const basis = sma(values, period)
  const upper: Array<number | null> = []
  const lower: Array<number | null> = []
  values.forEach((_, index) => {
    if (index < period - 1 || basis[index] == null) {
      upper.push(null)
      lower.push(null)
      return
    }
    const window = values.slice(index - period + 1, index + 1)
    const mean = basis[index] as number
    const deviation = Math.sqrt(window.reduce((total, value) => total + (value - mean) ** 2, 0) / period)
    upper.push(mean + deviation * deviations)
    lower.push(mean - deviation * deviations)
  })
  return { basis, upper, lower }
}

function rsi(values: number[], period = 14) {
  const output: Array<number | null> = Array(values.length).fill(null)
  if (values.length <= period) return output
  let gains = 0
  let losses = 0
  for (let index = 1; index <= period; index += 1) {
    const change = values[index] - values[index - 1]
    gains += Math.max(change, 0)
    losses += Math.max(-change, 0)
  }
  let averageGain = gains / period
  let averageLoss = losses / period
  output[period] = averageLoss === 0 ? 100 : 100 - 100 / (1 + averageGain / averageLoss)
  for (let index = period + 1; index < values.length; index += 1) {
    const change = values[index] - values[index - 1]
    averageGain = (averageGain * (period - 1) + Math.max(change, 0)) / period
    averageLoss = (averageLoss * (period - 1) + Math.max(-change, 0)) / period
    output[index] = averageLoss === 0 ? 100 : 100 - 100 / (1 + averageGain / averageLoss)
  }
  return output
}

function vwap(candles: Candle[]) {
  let volumeTotal = 0
  let valueTotal = 0
  return uniqueCandles(candles).map(([, candle]) => {
    const volume = Number(candle.volume)
    const typical = (Number(candle.high) + Number(candle.low) + Number(candle.close)) / 3
    volumeTotal += volume
    valueTotal += typical * volume
    return volumeTotal ? valueTotal / volumeTotal : typical
  })
}

function macd(values: number[]) {
  const fast = ema(values, 12)
  const slow = ema(values, 26)
  const line = values.map((_, index) => fast[index] == null || slow[index] == null ? null : (fast[index] as number) - (slow[index] as number))
  const dense = line.map(value => value ?? 0)
  const signalRaw = ema(dense, 9)
  const signal = signalRaw.map((value, index) => line[index] == null || index < 33 ? null : value)
  const histogram = line.map((value, index) => value == null || signal[index] == null ? null : value - (signal[index] as number))
  return { line, signal, histogram }
}

function formatPrice(value: number) {
  return new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
}

function formatVolume(value: number) {
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 }).format(value)
}

function lastNumber(values: Array<number | null>) {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (values[index] != null) return values[index] as number
  }
  return null
}

function drawingStorageKey(symbol: string, timeframe: string) {
  return `cosmos-chart-drawings:${symbol}:${timeframe}`
}

function readDrawings(symbol: string, timeframe: string): Drawing[] {
  try {
    const value = localStorage.getItem(drawingStorageKey(symbol, timeframe))
    return value ? JSON.parse(value) as Drawing[] : []
  } catch {
    return []
  }
}

function readChartTimeZone(): ChartTimeZone {
  return localStorage.getItem('cosmos-chart-time-zone') === 'utc' ? 'utc' : 'local'
}

function ProfessionalChart({ candles, plan, exchange, symbol, timeframe, timeframes, busy, showPlan, onTimeframeChange }: {
  candles: Candle[]
  plan: Plan | null
  exchange: string
  symbol: string
  timeframe: string
  timeframes: string[]
  busy: boolean
  showPlan: boolean
  onTimeframeChange: (timeframe: string) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const candleByTimeRef = useRef<Map<number, Candle>>(new Map())
  const lineSeriesRef = useRef<Partial<Record<LineIndicatorKey, ISeriesApi<'Line'>>>>({})
  const histogramSeriesRef = useRef<Partial<Record<HistogramIndicatorKey, ISeriesApi<'Histogram'>>>>({})
  const priceLinesRef = useRef<IPriceLine[]>([])
  const initialFitRef = useRef(true)
  const dataScopeRef = useRef('')
  const [tool, setTool] = useState<DrawingTool>('cursor')
  const [indicatorMenu, setIndicatorMenu] = useState(false)
  const [timeframeMenu, setTimeframeMenu] = useState(false)
  const [rangeMenu, setRangeMenu] = useState(false)
  const [scaleMenu, setScaleMenu] = useState(false)
  const [indicators, setIndicators] = useState(DEFAULT_INDICATORS)
  const [visibleBars, setVisibleBars] = useState<VisibleBars>('all')
  const [scaleMode, setScaleMode] = useState<ScaleMode>('normal')
  const [chartTimeZone, setChartTimeZone] = useState<ChartTimeZone>(readChartTimeZone)
  const drawingScope = drawingStorageKey(symbol, timeframe)
  const [drawingsByScope, setDrawingsByScope] = useState<Record<string, Drawing[]>>({})
  const storedDrawings = useMemo(() => readDrawings(symbol, timeframe), [symbol, timeframe])
  const drawings = drawingsByScope[drawingScope] ?? storedDrawings
  const setDrawings = (update: Drawing[] | ((current: Drawing[]) => Drawing[])) => setDrawingsByScope(scopes => {
    const current = scopes[drawingScope] ?? readDrawings(symbol, timeframe)
    const next = typeof update === 'function' ? update(current) : update
    return { ...scopes, [drawingScope]: next }
  })
  const [pending, setPending] = useState<{ scope: string; tool: Exclude<DrawingTool, 'cursor'>; point: Point } | null>(null)
  const [drawingRevision, setDrawingRevision] = useState(0)
  const [drawingNodes, setDrawingNodes] = useState<ReactNode[]>([])
  const [crosshair, setCrosshair] = useState<Ohlcv | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const latest = candles[candles.length - 1]
  const selected = crosshair ?? (latest ? {
    open: Number(latest.open), high: Number(latest.high), low: Number(latest.low), close: Number(latest.close), volume: Number(latest.volume),
    time: Number(toTime(latest.timestamp)),
  } : null)
  const indicatorSignature = Object.entries(indicators).filter(([, enabled]) => enabled).map(([key]) => key).join('|')
  const primaryTimeframes = timeframes.filter(value => PRIMARY_CHART_TIMEFRAMES.includes(value as typeof PRIMARY_CHART_TIMEFRAMES[number]))
  const extraTimeframes = timeframes.filter(value => !PRIMARY_CHART_TIMEFRAMES.includes(value as typeof PRIMARY_CHART_TIMEFRAMES[number]))
  const scaleLabel = SCALE_MODES.find(item => item.key === scaleMode)?.label ?? '普通坐标'
  const indicatorValues = useMemo(() => {
    const closes = uniqueCandles(candles).map(([, candle]) => Number(candle.close))
    const bands = bollinger(closes)
    return {
      ema20: lastNumber(ema(closes, 20)),
      ema50: lastNumber(ema(closes, 50)),
      sma20: lastNumber(sma(closes, 20)),
      vwap: lastNumber(vwap(candles)),
      bollinger: lastNumber(bands.basis),
    }
  }, [candles])

  useEffect(() => {
    localStorage.setItem(drawingStorageKey(symbol, timeframe), JSON.stringify(drawings))
  }, [drawings, symbol, timeframe])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    initialFitRef.current = true
    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: '#0a0d10' },
        textColor: '#7f8996',
        fontFamily: '"IBM Plex Mono", "Microsoft YaHei", monospace',
        fontSize: 12,
        panes: { separatorColor: '#20262d', separatorHoverColor: '#343d47', enableResize: true },
      },
      grid: { vertLines: { color: '#151a20' }, horzLines: { color: '#151a20' } },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: '#64748b', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#34404c' },
        horzLine: { color: '#64748b', width: 1, style: LineStyle.Dashed, labelBackgroundColor: '#34404c' },
      },
      rightPriceScale: { borderColor: '#20262d', scaleMargins: { top: 0.08, bottom: 0.18 } },
      timeScale: {
        borderColor: '#20262d', timeVisible: true, secondsVisible: false, rightOffset: 8, barSpacing: 7, minBarSpacing: 2,
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => formatChartTick(time, tickMarkType, chartTimeZone),
      },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
      handleScroll: { horzTouchDrag: true, mouseWheel: true, pressedMouseMove: true, vertTouchDrag: false },
      localization: {
        locale: 'zh-CN',
        priceFormatter: formatPrice,
        timeFormatter: (time: Time) => formatChartDateTime(time, chartTimeZone),
      },
    })
    chartRef.current = chart
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#19b394', downColor: '#ef5b68', borderVisible: false,
      wickUpColor: '#43c7ad', wickDownColor: '#ef7680', priceLineColor: '#e8a94b', priceLineWidth: 1,
    })
    candleSeriesRef.current = candleSeries

    const rows = uniqueCandles(candles)
    candleByTimeRef.current = new Map(rows)
    const closes = rows.map(([, candle]) => Number(candle.close))
    candleSeries.setData(priceData(candles))
    const addLine = (key: LineIndicatorKey, values: Array<number | null>, color: string, width: 1 | 2 = 1, paneIndex = 0, title = '') => {
      const series = chart.addSeries(LineSeries, { color, lineWidth: width, priceLineVisible: false, lastValueVisible: false, title }, paneIndex)
      series.setData(lineData(values, candles))
      lineSeriesRef.current[key] = series
      return series
    }
    let paneIndex = 1
    if (indicators.volume) {
      const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, paneIndex)
      const volumeRows: HistogramData<Time>[] = rows.map(([time, candle]) => ({
        time: time as UTCTimestamp,
        value: Number(candle.volume),
        color: Number(candle.close) >= Number(candle.open) ? '#1d6e61aa' : '#853944aa',
      }))
      volume.setData(volumeRows)
      histogramSeriesRef.current.volume = volume
      paneIndex += 1
    }
    if (indicators.ema20) addLine('ema20', ema(closes, 20), '#f2b84b', 1, 0, 'EMA 20')
    if (indicators.ema50) addLine('ema50', ema(closes, 50), '#5aa6ff', 1, 0, 'EMA 50')
    if (indicators.sma20) addLine('sma20', sma(closes, 20), '#c084fc', 1, 0, 'SMA 20')
    if (indicators.vwap) addLine('vwap', vwap(candles), '#ff8f5c', 1, 0, 'VWAP')
    if (indicators.bollinger) {
      const bands = bollinger(closes)
      addLine('bb-upper', bands.upper, '#7389a3', 1, 0, 'BB Upper')
      addLine('bb-basis', bands.basis, '#9caec2', 1, 0, 'BB Basis')
      addLine('bb-lower', bands.lower, '#7389a3', 1, 0, 'BB Lower')
    }
    if (indicators.rsi) {
      const series = addLine('rsi', rsi(closes), '#b68cff', 2, paneIndex, 'RSI 14')
      series.createPriceLine({ price: 70, color: '#ef5b6866', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: '' })
      series.createPriceLine({ price: 30, color: '#19b39466', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: '' })
      paneIndex += 1
    }
    if (indicators.macd) {
      const values = macd(closes)
      addLine('macd', values.line, '#5aa6ff', 1, paneIndex, 'MACD')
      addLine('macd-signal', values.signal, '#f2b84b', 1, paneIndex, 'Signal')
      const histogram = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, paneIndex)
      histogram.setData(rows.flatMap(([time], index) => values.histogram[index] == null ? [] : [{
        time: time as UTCTimestamp,
        value: values.histogram[index] as number,
        color: (values.histogram[index] as number) >= 0 ? '#19b39499' : '#ef5b6899',
      }]))
      histogramSeriesRef.current['macd-histogram'] = histogram
    }

    const sizePanes = () => {
      const panes = chart.panes()
      const secondaryCount = Math.max(panes.length - 1, 0)
      const secondaryHeight = Math.max(82, Math.min(108, Math.floor(container.clientHeight * .16)))
      const primaryHeight = Math.max(320, container.clientHeight - secondaryHeight * secondaryCount)
      panes.forEach((pane, index) => pane.setHeight(index === 0 ? primaryHeight : secondaryHeight))
    }
    sizePanes()
    chart.timeScale().fitContent()
    chart.subscribeCrosshairMove(param => {
      const data = param.seriesData.get(candleSeries)
      if (data && 'open' in data) {
        const volumeRow = candleByTimeRef.current.get(Number(param.time))
        setCrosshair({ open: data.open, high: data.high, low: data.low, close: data.close, volume: Number(volumeRow?.volume ?? 0), time: Number(param.time) })
      } else {
        setCrosshair(null)
      }
    })
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => setDrawingRevision(value => value + 1))
    const observer = new ResizeObserver(() => {
      sizePanes()
      setDrawingRevision(value => value + 1)
    })
    observer.observe(container)
    return () => {
      observer.disconnect()
      lineSeriesRef.current = {}
      histogramSeriesRef.current = {}
      priceLinesRef.current = []
      candleSeriesRef.current = null
      chartRef.current = null
      chart.remove()
    }
  // Rebuild only when the selected indicator pane structure changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indicatorSignature])

  useEffect(() => {
    const series = candleSeriesRef.current
    const chart = chartRef.current
    if (!series || !chart) return
    const dataScope = `${symbol}:${timeframe}`
    const scopeChanged = dataScopeRef.current !== dataScope
    series.setData(priceData(candles))
    const rows = uniqueCandles(candles)
    candleByTimeRef.current = new Map(rows)
    const closes = rows.map(([, candle]) => Number(candle.close))
    const updateLine = (key: LineIndicatorKey, values: Array<number | null>) => lineSeriesRef.current[key]?.setData(lineData(values, candles))
    updateLine('ema20', ema(closes, 20))
    updateLine('ema50', ema(closes, 50))
    updateLine('sma20', sma(closes, 20))
    updateLine('vwap', vwap(candles))
    const bands = bollinger(closes)
    updateLine('bb-upper', bands.upper)
    updateLine('bb-basis', bands.basis)
    updateLine('bb-lower', bands.lower)
    updateLine('rsi', rsi(closes))
    const macdValues = macd(closes)
    updateLine('macd', macdValues.line)
    updateLine('macd-signal', macdValues.signal)
    histogramSeriesRef.current.volume?.setData(rows.map(([time, candle]) => ({
      time: time as UTCTimestamp,
      value: Number(candle.volume),
      color: Number(candle.close) >= Number(candle.open) ? '#1d6e61aa' : '#853944aa',
    })))
    histogramSeriesRef.current['macd-histogram']?.setData(rows.flatMap(([time], index) => macdValues.histogram[index] == null ? [] : [{
      time: time as UTCTimestamp,
      value: macdValues.histogram[index] as number,
      color: (macdValues.histogram[index] as number) >= 0 ? '#19b39499' : '#ef5b6899',
    }]))
    if (initialFitRef.current || scopeChanged) {
      if (visibleBars === 'all') {
        chart.timeScale().fitContent()
      } else {
        chart.timeScale().setVisibleLogicalRange({
          from: Math.max(rows.length - visibleBars, 0),
          to: Math.max(rows.length - 1, 0) + 6,
        })
      }
      initialFitRef.current = false
    }
    dataScopeRef.current = dataScope
  }, [candles, symbol, timeframe, visibleBars])

  useEffect(() => {
    localStorage.setItem('cosmos-chart-time-zone', chartTimeZone)
    chartRef.current?.applyOptions({
      localization: { timeFormatter: (time: Time) => formatChartDateTime(time, chartTimeZone) },
      timeScale: { tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => formatChartTick(time, tickMarkType, chartTimeZone) },
    })
  }, [chartTimeZone])

  useEffect(() => {
    const chart = chartRef.current
    const selection = SCALE_MODES.find(item => item.key === scaleMode)
    if (!chart || !selection) return
    chart.priceScale('right').applyOptions({ mode: selection.mode, autoScale: true })
  }, [scaleMode, indicatorSignature])

  useEffect(() => {
    const series = candleSeriesRef.current
    if (!series) return
    priceLinesRef.current.forEach(line => series.removePriceLine(line))
    priceLinesRef.current = []
    if (!showPlan || !plan) return
    priceLinesRef.current = [
      series.createPriceLine({ price: (Number(plan.entry.zone.lower) + Number(plan.entry.zone.upper)) / 2, color: '#e8a94b', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'ENTRY' }),
      series.createPriceLine({ price: Number(plan.exit.stop_loss), color: '#ef5b68', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'STOP' }),
      ...plan.exit.take_profits.map((target, index) => series.createPriceLine({ price: Number(target.price), color: '#19b394', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: `TP${index + 1}` })),
    ]
  }, [plan, indicatorSignature, showPlan])

  useEffect(() => {
    const onFullscreen = () => setFullscreen(document.fullscreenElement === containerRef.current?.closest('.chart-workbench'))
    document.addEventListener('fullscreenchange', onFullscreen)
    return () => document.removeEventListener('fullscreenchange', onFullscreen)
  }, [])

  useEffect(() => {
    const cancel = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setPending(null)
        setTool('cursor')
        setIndicatorMenu(false)
        setTimeframeMenu(false)
        setRangeMenu(false)
        setScaleMenu(false)
      }
    }
    window.addEventListener('keydown', cancel)
    return () => window.removeEventListener('keydown', cancel)
  }, [])

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const chart = chartRef.current
      const series = candleSeriesRef.current
      const width = containerRef.current?.clientWidth ?? 0
      const height = containerRef.current?.clientHeight ?? 0
      if (!chart || !series) {
        setDrawingNodes([])
        return
      }
      const point = (value: Point) => ({ x: chart.timeScale().timeToCoordinate(value.time as UTCTimestamp), y: series.priceToCoordinate(value.price) })
      const nodes = drawings.flatMap(drawing => {
        const first = point(drawing.points[0])
        const second = drawing.points[1] ? point(drawing.points[1]) : first
        if (first.x == null || first.y == null || second.x == null || second.y == null) return []
        const firstX = Number(first.x)
        const firstY = Number(first.y)
        const secondX = Number(second.x)
        const secondY = Number(second.y)
        if (drawing.tool === 'horizontal') return [<line key={drawing.id} x1="0" x2={width} y1={firstY} y2={firstY} className="drawing-line" />]
        if (drawing.tool === 'vertical') return [<line key={drawing.id} x1={firstX} x2={firstX} y1="0" y2={height} className="drawing-line" />]
        if (drawing.tool === 'rectangle') return [<rect key={drawing.id} x={Math.min(firstX, secondX)} y={Math.min(firstY, secondY)} width={Math.abs(secondX - firstX)} height={Math.abs(secondY - firstY)} className="drawing-rectangle" />]
        if (drawing.tool === 'fibonacci') {
          const ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]
          return [<g key={drawing.id} className="drawing-fib">{ratios.map(ratio => {
            const y = firstY + (secondY - firstY) * ratio
            return <g key={ratio}><line x1={Math.min(firstX, secondX)} x2={width} y1={y} y2={y} /><text x={Math.min(firstX, secondX) + 6} y={y - 4}>{ratio.toFixed(3)}</text></g>
          })}</g>]
        }
        return [<g key={drawing.id} className="drawing-trend"><line x1={firstX} y1={firstY} x2={secondX} y2={secondY} /><circle cx={firstX} cy={firstY} r="3" /><circle cx={secondX} cy={secondY} r="3" /></g>]
      })
      setDrawingNodes(nodes)
    })
    return () => cancelAnimationFrame(frame)
  }, [drawings, drawingRevision])

  const placeDrawing = (event: React.PointerEvent<SVGSVGElement>) => {
    if (tool === 'cursor') return
    const chart = chartRef.current
    const series = candleSeriesRef.current
    if (!chart || !series) return
    const bounds = event.currentTarget.getBoundingClientRect()
    const x = event.clientX - bounds.left
    const y = event.clientY - bounds.top
    if (y > (chart.panes()[0]?.getHeight() ?? bounds.height)) return
    const time = chart.timeScale().coordinateToTime(x)
    const price = series.coordinateToPrice(y)
    if (typeof time !== 'number' || price == null) return
    const nextPoint = { time, price: Number(price) }
    if (tool === 'horizontal' || tool === 'vertical') {
      setDrawings(current => [...current, { id: crypto.randomUUID(), tool, points: [nextPoint] }])
      return
    }
    const activePending = pending?.scope === drawingScope ? pending : null
    if (!activePending || activePending.tool !== tool) {
      setPending({ scope: drawingScope, tool, point: nextPoint })
    } else {
      setDrawings(current => [...current, { id: crypto.randomUUID(), tool, points: [activePending.point, nextPoint] }])
      setPending(null)
    }
  }

  const toggleFullscreen = async () => {
    const workbench = containerRef.current?.closest('.chart-workbench') as HTMLElement | null
    if (!workbench) return
    if (document.fullscreenElement) await document.exitFullscreen()
    else await workbench.requestFullscreen()
  }

  const focusVisibleBars = (next: VisibleBars) => {
    const chart = chartRef.current
    const length = uniqueCandles(candles).length
    setVisibleBars(next)
    setRangeMenu(false)
    if (!chart) return
    if (next === 'all') chart.timeScale().fitContent()
    else chart.timeScale().setVisibleLogicalRange({ from: Math.max(length - next, 0), to: Math.max(length - 1, 0) + 6 })
  }

  const chooseTimeframe = (value: string) => {
    setTimeframeMenu(false)
    onTimeframeChange(value)
  }

  return (
    <div className="chart-workbench" role="region" aria-label={`${symbol} ${timeframe.toUpperCase()} 专业行情图表`}>
      <div className="chart-commandbar">
        <div className="timeframes" aria-label="K线周期">
          <span className="timeframe-label">周期</span>
          {primaryTimeframes.map(value => <button type="button" key={value} aria-pressed={timeframe === value} className={timeframe === value ? 'active' : ''} onClick={() => chooseTimeframe(value)} disabled={busy}>{timeframeLabel(value)}</button>)}
          {!!extraTimeframes.length && <div className="command-menu-anchor timeframe-menu-anchor">
            <button type="button" className={`timeframe-more ${extraTimeframes.includes(timeframe) ? 'active' : ''}`} aria-expanded={timeframeMenu} onClick={() => { setTimeframeMenu(value => !value); setIndicatorMenu(false); setRangeMenu(false); setScaleMenu(false) }} disabled={busy}>
              {extraTimeframes.includes(timeframe) ? timeframeLabel(timeframe) : '更多'} <ChevronDown aria-hidden="true" />
            </button>
            {timeframeMenu && <div className="timeframe-menu" role="menu" aria-label="更多K线周期">
              <header><strong>更多周期</strong><span>短线到周线</span></header>
              <div>{extraTimeframes.map(value => <button type="button" role="menuitem" key={value} className={timeframe === value ? 'active' : ''} onClick={() => chooseTimeframe(value)}>{timeframeLabel(value)}<small>{value.endsWith('m') ? '分钟' : value.endsWith('h') ? '小时' : value.endsWith('d') ? '日线' : '周线'}</small></button>)}</div>
            </div>}
          </div>}
        </div>
        <span className="toolbar-divider" />
        <div className="command-menu-anchor">
          <button type="button" className="command-button" onClick={() => { setIndicatorMenu(value => !value); setTimeframeMenu(false); setRangeMenu(false); setScaleMenu(false) }} aria-expanded={indicatorMenu}>
            <SquareFunction aria-hidden="true" /> 指标 <small>{Object.values(indicators).filter(Boolean).length}</small>
          </button>
          {indicatorMenu && <div className="indicator-menu">
            <header><div><strong>技术指标</strong><span>叠加到当前图表</span></div><button type="button" aria-label="关闭指标面板" onClick={() => setIndicatorMenu(false)}><X aria-hidden="true" /></button></header>
            {INDICATOR_LABELS.map(item => <label key={item.key}><span><strong>{item.label}</strong><small>{item.detail}</small></span><input type="checkbox" checked={indicators[item.key]} onChange={() => setIndicators(current => ({ ...current, [item.key]: !current[item.key] }))} /></label>)}
          </div>}
        </div>
        <button type="button" className="command-button" onClick={() => chartRef.current?.timeScale().fitContent()}><ScanLine aria-hidden="true" />自动缩放</button>
        <div className="command-menu-anchor">
          <button type="button" className="command-button" aria-expanded={rangeMenu} onClick={() => { setRangeMenu(value => !value); setIndicatorMenu(false); setTimeframeMenu(false); setScaleMenu(false) }}><CalendarRange aria-hidden="true" />{visibleBars === 'all' ? '全部' : `${visibleBars} 根`}<ChevronDown aria-hidden="true" /></button>
          {rangeMenu && <div className="compact-command-menu range-menu" role="menu" aria-label="图表可见范围">{VISIBLE_BAR_OPTIONS.map(option => <button type="button" role="menuitem" key={String(option.value)} className={visibleBars === option.value ? 'active' : ''} onClick={() => focusVisibleBars(option.value)}>{option.label}</button>)}</div>}
        </div>
        <div className="command-menu-anchor scale-menu-anchor">
          <button type="button" className="command-button" aria-expanded={scaleMenu} onClick={() => { setScaleMenu(value => !value); setIndicatorMenu(false); setTimeframeMenu(false); setRangeMenu(false) }}><ChartNoAxesCombined aria-hidden="true" />{scaleLabel}<ChevronDown aria-hidden="true" /></button>
          {scaleMenu && <div className="compact-command-menu scale-menu" role="menu" aria-label="价格坐标模式">{SCALE_MODES.map(option => <button type="button" role="menuitem" key={option.key} className={scaleMode === option.key ? 'active' : ''} onClick={() => { setScaleMode(option.key); setScaleMenu(false) }}>{option.label}</button>)}</div>}
        </div>
        <button
          type="button"
          className="command-button chart-time-zone"
          aria-label={`图表时区：${chartTimeZoneLabel(chartTimeZone)}；点击切换${chartTimeZone === 'local' ? ' UTC' : '本地时区'}`}
          title={`当前图表时区：${chartTimeZoneLabel(chartTimeZone)}`}
          onClick={() => setChartTimeZone(current => current === 'local' ? 'utc' : 'local')}
        >
          <Clock3 aria-hidden="true" />{chartTimeZone === 'local' ? localUtcOffsetLabel() : 'UTC'}<small>{chartTimeZone === 'local' ? '本地' : '标准'}</small>
        </button>
        <div className="ohlc-strip">
          {selected && <><span className="ohlc-time">{formatChartDateTime(selected.time, chartTimeZone)}</span><span>开 <b>{formatPrice(selected.open)}</b></span><span>高 <b>{formatPrice(selected.high)}</b></span><span>低 <b>{formatPrice(selected.low)}</b></span><span>收 <b className={selected.close >= selected.open ? 'positive' : 'negative'}>{formatPrice(selected.close)}</b></span><span>量 <b>{formatVolume(selected.volume)}</b></span><span>涨跌 <b className={selected.close >= selected.open ? 'positive' : 'negative'}>{selected.open ? `${((selected.close / selected.open - 1) * 100).toFixed(2)}%` : '—'}</b></span></>}
        </div>
        <button type="button" className="icon-command" aria-label={fullscreen ? '退出全屏' : '图表全屏'} data-tooltip={fullscreen ? '退出全屏' : '全屏'} onClick={() => void toggleFullscreen()}>{fullscreen ? <Minimize2 aria-hidden="true" /> : <Fullscreen aria-hidden="true" />}</button>
      </div>
      <div className="chart-stage">
        <aside className="drawing-toolbar" aria-label="绘图工具">
          {TOOL_LABELS.map(item => <button type="button" key={item.key} aria-pressed={tool === item.key} className={tool === item.key ? 'active' : ''} data-tooltip={item.label} aria-label={item.label} onClick={() => { setTool(item.key); setPending(null) }}><ToolIcon tool={item.key} /></button>)}
          <span />
          <button type="button" data-tooltip="撤销绘图" aria-label="撤销绘图" disabled={!drawings.length} onClick={() => setDrawings(current => current.slice(0, -1))}><Undo2 aria-hidden="true" /></button>
          <button type="button" data-tooltip="清空绘图" aria-label="清空绘图" disabled={!drawings.length} onClick={() => { if (window.confirm('清空当前品种与周期的全部绘图？')) setDrawings([]) }}><Trash2 aria-hidden="true" /></button>
        </aside>
        <div className="chart-canvas-shell">
          <div className="chart-watermark"><strong>{symbol}</strong><span>{timeframe.toUpperCase()} · {exchange.toUpperCase()} · {chartTimeZoneLabel(chartTimeZone)} · {showPlan && plan ? '策略点位已叠加' : '行情观察（当前无可执行计划）'}</span></div>
          <div className="chart-indicator-legend">
            {indicators.ema20 && <span className="ema20">EMA 20 <b>{indicatorValues.ema20 == null ? '—' : formatPrice(indicatorValues.ema20)}</b></span>}{indicators.ema50 && <span className="ema50">EMA 50 <b>{indicatorValues.ema50 == null ? '—' : formatPrice(indicatorValues.ema50)}</b></span>}{indicators.sma20 && <span className="sma20">SMA 20 <b>{indicatorValues.sma20 == null ? '—' : formatPrice(indicatorValues.sma20)}</b></span>}{indicators.bollinger && <span>BB 20 2 <b>{indicatorValues.bollinger == null ? '—' : formatPrice(indicatorValues.bollinger)}</b></span>}{indicators.vwap && <span>VWAP <b>{indicatorValues.vwap == null ? '—' : formatPrice(indicatorValues.vwap)}</b></span>}
          </div>
          <div ref={containerRef} className="professional-chart" />
          <svg className={`drawing-overlay ${tool !== 'cursor' ? 'is-drawing' : ''}`} onPointerDown={placeDrawing} aria-label="图表绘图层">{drawingNodes}</svg>
          {pending?.scope === drawingScope && <div className="drawing-hint">选择第二个点 · Esc 取消</div>}
          {busy && <div className="chart-loading" role="status"><LoaderCircle aria-hidden="true" /><span>正在加载周期行情</span></div>}
          <a className="tradingview-attribution" href="https://www.tradingview.com/" target="_blank" rel="noreferrer">图表技术由 TradingView 提供</a>
        </div>
      </div>
    </div>
  )
}

export default ProfessionalChart
