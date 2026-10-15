import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { MODE, eur, hhmm, type Lot } from '../api'
import { Flap, Mark, PriceFlap } from '../components'
import { useLive, useNow } from '../hooks'

type BoardData = {
  now: string; lots: Lot[]
  recent: { id: string; lot: string; shop: string; units: number; unit_cents: number; rule: string; at: string; status: string }[]
  totals: { wishes: number; lots_open: number; units_week: number; value_week: number; ledger_events: number }
}

const STATUS: Record<string, [string, string, string]> = {
  open: ['EN VENTE', 'ON SALE', 'live'], upcoming: ['BIENTÔT', 'SOON', 'wait'], sold_out: ['VENDU', 'SOLD', 'sold'],
  threshold_met: ['VALIDÉ', 'GO', 'sold'], deadline: ['CLOS', 'CLOSED', ''], threshold_failed: ['ANNULÉ', 'OFF', ''],
  withdrawn: ['RETIRÉ', 'WITHDRAWN', ''],
}

function countdown(iso: string | null | undefined, now: number) {
  if (!iso) return '--:--'
  const s = Math.max(0, Math.round((new Date(iso).getTime() - now) / 1000))
  const h = Math.floor(s / 3600)
  if (h >= 24) return `${Math.floor(h / 24)}d ${String(h % 24).padStart(2, '0')}h`
  if (h > 0) return `${h}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}`
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

function NextCell({ l, now }: { l: Lot; now: number }) {
  if (l.mode === 'threshold' && l.threshold) {
    const pct = Math.min(100, (100 * l.threshold.committed) / l.threshold.target)
    return (
      <div className="stack tight" style={{ minWidth: 150 }}>
        <div className="row between small"><span className="mono">{Math.min(l.threshold.committed, l.threshold.target)} / {l.threshold.target}{l.threshold.committed > l.threshold.target ? ` +${l.threshold.committed - l.threshold.target} waitlist` : ''}</span>
          <span className="muted">{countdown(l.closes_at, now)}</span></div>
        <div className="progress"><i style={{ width: `${pct}%` }} /></div>
        {l.threshold.burst && <span className="tiny" style={{ color: 'var(--sold)' }}>{l.threshold.burst.authorized} PayPal holds in {(l.threshold.burst.wall_ms / 1000).toFixed(1)} s</span>}
      </div>
    )
  }
  if (l.mode === 'clock' && l.clock?.next_at && l.status === 'open') {
    return <span className="small"><span className="muted">next </span><span className="mono">{eur(l.clock.next_price)}</span><span className="muted"> in </span><span className="mono">{countdown(l.clock.next_at, now)}</span></span>
  }
  if (l.mode === 'call' && !['deadline', 'sold_out'].includes(l.status)) {
    return <span className="small"><span className="muted">clears at </span><span className="mono">{hhmm(l.closes_at)}</span></span>
  }
  return <span className="small muted">closes {hhmm(l.closes_at)}</span>
}

export default function Board() {
  const { data } = useLive<BoardData>('/api/board', { pollMs: 5000 })
  const now = useNow(1000)
  const [announce, setAnnounce] = useState('')
  const prev = useRef<Record<string, number>>({})
  useEffect(() => {
    if (!data) return
    const msgs: string[] = []
    for (const l of data.lots) {
      const p = prev.current[l.id]
      if (p !== undefined && p !== l.price) msgs.push(`${l.name} at ${l.shop.name} now ${eur(l.price)}`)
      prev.current[l.id] = l.price
    }
    if (msgs.length) setAnnounce(msgs.slice(0, 3).join('. '))
  }, [data])
  const t = new Date(now).toLocaleTimeString('en-GB', { timeZone: 'Europe/Paris' })
  const lots = data?.lots ?? []
  return (
    <main className="board">
      <div className="board-head">
        <div className="row" style={{ gap: 16 }}>
          <Link to="/" aria-label="Criée home"><Mark size={44} /></Link>
          <div className="board-title">Criée de Strasbourg<small>Ce soir · Tonight</small></div>
        </div>
        <div className="row" style={{ gap: 28 }}>
          <div className="stack tight small hide-sm" style={{ textAlign: 'right' }}>
            <span><span className="muted">standing wishes </span><span className="mono">{data?.totals.wishes ?? '–'}</span></span>
            <span><span className="muted">cleared this week </span><span className="mono">{data ? `${data.totals.units_week} · ${eur(data.totals.value_week)}` : '–'}</span></span>
            <span><span className="muted">signed ledger events </span><span className="mono">{data?.totals.ledger_events ?? '–'}</span></span>
          </div>
          <div className="board-clock"><Flap text={t} label={`Time ${t}`} /></div>
        </div>
      </div>
      <div aria-live="polite" className="sr-only">{announce}</div>
      <table className="board-table">
        <thead>
          <tr>
            <th>Article<span>Item</span></th>
            <th className="hide-sm">Mode<span>Clearing</span></th>
            <th>Prix<span>Price</span></th>
            <th>Reste<span>Left</span></th>
            <th className="hide-sm">En attente<span>Wishes waiting</span></th>
            <th className="hide-sm">Suite<span>Next</span></th>
            <th>Statut<span>Status</span></th>
          </tr>
        </thead>
        <tbody>
          {lots.map((l) => {
            const [fr, en, cls] = STATUS[l.status] ?? [l.status.toUpperCase(), '', '']
            const done = ['deadline', 'threshold_failed', 'withdrawn'].includes(l.status)
            return (
              <tr key={l.id} className={cls === 'sold' ? 'sold' : done ? 'closed' : ''}>
                <td><div className="board-name">{l.name_fr || l.name}<small>{l.name_fr ? `${l.name} · ` : ''}{l.shop.name}{l.simulated ? ' · simulated' : ''}</small></div></td>
                <td className="hide-sm"><div className="small">{MODE[l.mode].fr}<div className="muted tiny">{MODE[l.mode].en}</div></div></td>
                <td><PriceFlap cents={l.price} tone={cls === 'sold' ? 'amber' : undefined} /></td>
                <td><Flap text={String(Math.max(0, l.mode === 'threshold' ? l.quantity - (l.threshold?.committed ?? 0) : l.remaining)).padStart(2, ' ').slice(-3)} tone={l.remaining === 0 ? 'dim' : undefined} label={`${l.remaining} left`} /></td>
                <td className="hide-sm mono">{l.waiting}</td>
                <td className="hide-sm"><NextCell l={l} now={now} /></td>
                <td><span className={`status-flap ${cls}`}>{fr}</span><div className="tiny muted" style={{ marginTop: 3 }}>{en}</div></td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {lots.length === 0 && <div className="empty" style={{ margin: 30 }}>No lot on the board yet.</div>}
      <div className="ticker" aria-label="Latest matches">
        {(data?.recent ?? []).slice(0, 12).map((r) => (
          <span key={r.id}><b>{r.status === 'captured' ? 'PAID' : r.status === 'held' ? 'HELD' : r.status.toUpperCase()}</b> {r.units} × {r.lot} · {r.shop} · {eur(r.unit_cents)} · {hhmm(r.at)}</span>
        ))}
      </div>
    </main>
  )
}
