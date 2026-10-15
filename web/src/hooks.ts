import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'

export type LedgerPing = { seq: number; type: string; at: string; lot_id?: string; match_id?: string; hash: string }

type Listener = (e: LedgerPing) => void
const listeners = new Set<Listener>()
let source: EventSource | null = null
let connected = false
const connListeners = new Set<(c: boolean) => void>()

function ensureSource() {
  if (source) return
  source = new EventSource('/api/stream')
  source.addEventListener('ledger', (e) => {
    const data = JSON.parse((e as MessageEvent).data) as LedgerPing
    listeners.forEach((l) => l(data))
  })
  source.addEventListener('hello', () => { connected = true; connListeners.forEach((l) => l(true)) })
  source.onerror = () => { connected = false; connListeners.forEach((l) => l(false)) }
}

export function useLedger(onEvent: Listener) {
  const ref = useRef(onEvent)
  ref.current = onEvent
  useEffect(() => {
    ensureSource()
    const l: Listener = (e) => ref.current(e)
    listeners.add(l)
    return () => { listeners.delete(l) }
  }, [])
}

export function useConnected() {
  const [c, setC] = useState(connected)
  useEffect(() => { ensureSource(); connListeners.add(setC); return () => { connListeners.delete(setC) } }, [])
  return c
}

/** Fetch a resource, refetch when the ledger moves (debounced) and every `pollMs`. */
export function useLive<T>(path: string | null, opts: { pollMs?: number; filter?: (e: LedgerPing) => boolean } = {}) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)
  const load = useCallback(async () => {
    if (!path) return
    try { setData(await api.get<T>(path)); setError(null) } catch (e: any) { setError(e.message) }
  }, [path])
  useEffect(() => { setData(null); load() }, [load])
  useLedger((e) => {
    if (opts.filter && !opts.filter(e)) return
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(load, 250)
  })
  useEffect(() => {
    if (!opts.pollMs) return
    const id = window.setInterval(load, opts.pollMs)
    return () => window.clearInterval(id)
  }, [load, opts.pollMs])
  return { data, error, reload: load, setData }
}

export function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), ms); return () => window.clearInterval(id) }, [ms])
  return now
}

let toastSet: ((t: { text: string; bad?: boolean } | null) => void) | null = null
export function toast(text: string, bad = false) { toastSet?.({ text, bad }) }
export function useToastHost() {
  const [t, setT] = useState<{ text: string; bad?: boolean } | null>(null)
  useEffect(() => { toastSet = setT; return () => { toastSet = null } }, [])
  useEffect(() => { if (!t) return; const id = window.setTimeout(() => setT(null), 3800); return () => window.clearTimeout(id) }, [t])
  return t
}
