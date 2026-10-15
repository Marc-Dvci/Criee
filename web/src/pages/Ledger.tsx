import { useEffect, useState } from 'react'
import { api } from '../api'
import { Spinner } from '../components'
import { useLedger } from '../hooks'

type Ev = { seq: number; at: string; type: string; body: any; prev: string; hash: string; sig: string }

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
  const [verify, setVerify] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')
  const load = async () => {
    const head = await api.get<{ events: Ev[]; head: [number, string] }>('/api/ledger?after=0&limit=1')
    const after = Math.max(0, head.head[0] - 150)
    const r = await api.get<{ events: Ev[] }>(`/api/ledger?after=${after}&limit=150`)
    setEvents(r.events.reverse())
  }
  useEffect(() => { load() }, [])
  useLedger(async (p) => {
    const r = await api.get<{ events: Ev[] }>(`/api/ledger?after=${p.seq - 1}&limit=1`)
    setEvents((cur) => (cur.some((e) => e.seq === p.seq) ? cur : [...r.events, ...cur].slice(0, 600)))
  })
  const run = async () => { setBusy(true); setVerify(await api.get('/api/ledger/verify')); setBusy(false) }
  const shown = events.filter((e) => !filter || e.type.startsWith(filter))
  return (
    <main className="page">
      <div className="page-head">
        <div><h1>Ledger</h1><p className="lede">Every wish, tick, match and PayPal step is an event. Each one commits to the hash of the one before and carries Criée's Ed25519 signature, so any match can be replayed and checked.</p></div>
        <button className="btn primary" onClick={run} disabled={busy}>{busy ? <Spinner /> : null} Verify the whole chain</button>
      </div>
      {verify && (
        <div className={`notice ${verify.ok ? 'ok' : 'bad'} stack tight fade-in`} style={{ marginBottom: 18 }}>
          <b>{verify.ok ? `Chain verified: ${verify.events} events, every link and signature checks out.` : `Broken at event ${verify.seq}: ${verify.reason}`}</b>
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
