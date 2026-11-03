import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Spinner } from '../components'
import { useConnected, useLedger } from '../hooks'

type Ev = { seq: number; at: string; type: string; body: any; prev: string; hash: string; sig: string }
type Verification = {
  id: string
  done: boolean; ok?: boolean; checked: number; total: number; scope: 'recent' | 'full'
  from_seq: number; to_seq: number; head?: string; public_key?: string; seq?: number; reason?: string
}

function summary(e: Ev): string {
  const b = e.body
  switch (e.type) {
    case 'wish.signed': return `${b.label ?? 'wish'} · limit €${(b.terms.limit_cents / 100).toFixed(2)}${b.simulated ? ' · simulated' : ''}`
    case 'lot.listed': return `${b.listing?.name ?? 'lot'} · ${b.terms?.mode ?? ''} · seed commit ${String(b.seed_commit).slice(0, 10)}…`
    case 'clock.tick': return `round ${b.round} · €${(b.unit_cents / 100).toFixed(2)}`
    case 'match.made': return `${b.fill.units} unit(s) at €${(b.fill.unit_cents / 100).toFixed(2)} · ${b.fill.rule} round ${b.fill.round}`
    case 'settlement.authorized': return `PayPal order ${b.order_id} · authorization ${b.authorization_id} · ${b.ms} ms`
    case 'settlement.captured': return `PayPal capture ${b.capture_id} · €${(b.cents / 100).toFixed(2)}`
    case 'settlement.voided': return `hold released${b.paypal ? ' on PayPal' : ''}`
    case 'payout.sent': return `net €${(b.net / 100).toFixed(2)} after fee €${(b.fee / 100).toFixed(2)} · batch ${b.batch_id ?? '–'}`
    case 'burst.measured': return `${b.authorized}/${b.holds} holds in ${b.wall_ms} ms`
    case 'lot.closed': return `${b.reason} · seed revealed ${String(b.seed ?? '').slice(0, 10)}…`
    case 'threshold.committed': return `commitment #${b.order + 1}`
    default: return Object.entries(b).filter(([k]) => !['signature', 'terms', 'listing'].includes(k)).slice(0, 3).map(([k, v]) => `${k} ${typeof v === 'object' ? '…' : v}`).join(' · ')
  }
}

export default function Ledger() {
  const [events, setEvents] = useState<Ev[]>([])
  const [verify, setVerify] = useState<Verification | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [elapsed, setElapsed] = useState(0)
  const poll = useRef<number | null>(null)
  const generation = useRef(0)
  const refresh = useRef<number | null>(null)
  const live = useConnected()
  const started = useRef(0)
  const [filter, setFilter] = useState('')
  const load = async () => {
    const head = await api.get<{ events: Ev[]; head: [number, string] }>('/api/ledger?after=0&limit=1')
    const after = Math.max(0, head.head[0] - 150)
    const r = await api.get<{ events: Ev[] }>(`/api/ledger?after=${after}&limit=150`)
    setEvents(r.events.reverse())
  }
  useEffect(() => {
    void load().catch((e: Error) => setError(e.message))
    return () => {
      generation.current += 1
      if (poll.current !== null) window.clearTimeout(poll.current)
      if (refresh.current !== null) window.clearTimeout(refresh.current)
    }
  }, [])
  useEffect(() => {
    if (live) return
    const timer = window.setInterval(() => { void load().catch((e: Error) => setError(e.message)) }, 4000)
    return () => window.clearInterval(timer)
  }, [live])
  useEffect(() => {
    if (!busy) return
    const timer = window.setInterval(() => setElapsed((performance.now() - started.current) / 1000), 100)
    return () => window.clearInterval(timer)
  }, [busy])
  useLedger(() => {
    // One refresh for a settlement burst, rather than one HTTP request for each event.
    if (refresh.current !== null) return
    refresh.current = window.setTimeout(() => {
      refresh.current = null
      void load().catch((e: Error) => setError(e.message))
    }, 250)
  })
  const run = (full: boolean) => {
    if (poll.current !== null) window.clearTimeout(poll.current)
    const request = ++generation.current
    setBusy(true); setVerify(null); setError(''); setElapsed(0)
    started.current = performance.now()
    const update = async (path: string, start = false) => {
      try {
        const result = start ? await api.post<Verification>(path) : await api.get<Verification>(path)
        if (generation.current !== request) return
        setVerify(result)
        if (result.done) { setBusy(false); setElapsed((performance.now() - started.current) / 1000) }
        else poll.current = window.setTimeout(() => { void update(`/api/ledger/verify/jobs/${result.id}`) }, start ? 0 : 350)
      } catch (e: unknown) {
        if (generation.current !== request) return
        setBusy(false); setError(`Verification connection interrupted. Please retry; no result has been certified. ${(e as Error).message}`)
      }
    }
    void update(`/api/ledger/verify/jobs${full ? '' : '?recent=200'}`, true)
  }
  const shown = events.filter((e) => !filter || e.type.startsWith(filter))
  return (
    <main className="page">
      <div className="page-head">
        <div><h1>Ledger</h1><p className="lede">Every wish, tick, match and PayPal step is an event. Each one commits to the hash of the one before and carries Criée's Ed25519 signature, so any match can be replayed and checked.</p></div>
        <div className="row wrap">
          <button className="btn primary" onClick={() => run(false)} disabled={busy}>Quick check · latest 200</button>
          <button className="btn" onClick={() => run(true)} disabled={busy}>Verify the whole chain</button>
        </div>
      </div>
      <p className="small muted">Quick check verifies up to 200 recent events against their signed predecessor. Full audit starts at genesis and verifies every event through a fixed snapshot of the head. The public event bodies below are redacted; verification runs over the original records on the server.</p>
      {error && <div className="notice bad" role="alert" style={{ marginBottom: 18 }}>{error}</div>}
      {busy && <div className="notice stack" role="status" aria-live="polite" style={{ marginBottom: 18 }}>
        <div className="row"><Spinner /><b>{verify ? `${verify.checked.toLocaleString()} / ${verify.total.toLocaleString()} events checked` : 'Opening a ledger snapshot…'}</b><span className="mono small">{elapsed.toFixed(1)} s</span></div>
        <progress className="verify-progress" max={verify?.total || 1} value={verify?.checked ?? 0} aria-label="Ledger verification progress" />
        <span className="small muted">Recomputing SHA-256 links and verifying Ed25519 signatures. The market keeps running.</span>
      </div>}
      {verify?.done && (
        <div className={`notice ${verify.ok ? 'ok' : 'bad'} stack tight fade-in`} style={{ marginBottom: 18 }}>
          <b>{verify.ok ? `${verify.scope === 'full' ? 'Full chain verified' : 'Recent segment verified'}: ${verify.checked.toLocaleString()} events in ${elapsed.toFixed(2)} s.` : `Verification failed${verify.seq ? ` at event ${verify.seq}` : ''}: ${verify.reason}`}</b>
          {verify.ok && <span className="small">Events #{verify.from_seq}–#{verify.to_seq} · {verify.scope === 'full' ? 'every link and signature from genesis checks out' : 'earlier history was not checked; use the full audit to verify from genesis'}.</span>}
          {verify.head && <span className="hash">head {verify.head}</span>}
          <details><summary className="small">Public key</summary><pre className="hash" style={{ whiteSpace: 'pre-wrap' }}>{verify.public_key}</pre></details>
        </div>
      )}
      <div className="seg" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
        {[['', 'All'], ['wish', 'Wishes'], ['lot', 'Lots'], ['clock', 'Ticks'], ['match', 'Matches'], ['settlement', 'PayPal'], ['payout', 'Payouts'], ['dispute', 'Disputes']].map(([k, l]) => (
          <button key={k} className={filter === k ? 'on' : ''} onClick={() => setFilter(k)}>{l}</button>
        ))}
      </div>
      <div className="card" style={{ padding: 0 }}>
        {shown.map((e) => (
          <div key={e.seq} className="ledger-row fade-in">
            <span className="mono faint">#{e.seq}</span>
            <span className="mono small">{e.type}</span>
            <span className="small" style={{ overflowWrap: 'anywhere' }}>{summary(e)}</span>
            <span className="hash hide-sm" title={`prev ${e.prev}`}>{e.hash.slice(0, 20)}…</span>
          </div>
        ))}
        {shown.length === 0 && <div className="empty" style={{ margin: 18 }}>No event yet.</div>}
      </div>
    </main>
  )
}
