import { useEffect, useRef, useState } from 'react'
import { getSettings } from './api'
import type { ConfigurationView } from './types'

export default function BootstrapGate({ onReady }: { onReady: (view: ConfigurationView) => void }) {
  const [attempt, setAttempt] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const onReadyRef = useRef(onReady)
  useEffect(() => { onReadyRef.current = onReady }, [onReady])

  useEffect(() => {
    let stopped = false
    let retry: number | undefined
    const load = async () => {
      try {
        const settings = await getSettings()
        if (!stopped) onReadyRef.current(settings)
      } catch (reason) {
        if (stopped) return
        setError(reason instanceof Error ? reason.message : '无法连接本机服务')
        if (attempt < 4) {
          retry = window.setTimeout(() => setAttempt(value => value + 1), Math.min(2000 * 2 ** attempt, 15_000))
        }
      }
    }
    void load()
    return () => { stopped = true; window.clearTimeout(retry) }
  }, [attempt])

  return <div className="boot"><div className="boot-mark">C</div>
    <span>{error ? '暂时无法连接本机服务' : '正在读取本机配置…'}</span>
    {error && <p role="alert">{error}</p>}
    {error && <button type="button" onClick={() => { setError(null); setAttempt(value => value + 1) }}>重新连接</button>}
  </div>
}
