import { useEffect, useRef, useState, type ReactNode } from 'react'
import { eur, type Match } from './api'

/** Split-flap digits. Each tile flips when its character changes. */
export function Flap({ text, tone, label }: { text: string; tone?: 'amber' | 'dim'; label?: string }) {
  const prev = useRef(text)
  const [flipped, setFlipped] = useState<boolean[]>([])
  useEffect(() => {
    const p = prev.current
    setFlipped(text.split('').map((c, i) => p[i] !== c))
    prev.current = text
    const id = window.setTimeout(() => setFlipped([]), 400)
    return () => window.clearTimeout(id)
  }, [text])
  return (
    <span className={`flap ${tone ?? ''}`} role="text" aria-label={label ?? text}>
      {text.split('').map((c, i) => (
        <span key={i} className={`t ${c === '.' || c === ':' ? 'sep' : ''} ${flipped[i] ? 'flip' : ''}`} aria-hidden>
          {c}
        </span>
      ))}
    </span>
  )
}

export function PriceFlap({ cents, tone, size }: { cents: number; tone?: 'amber' | 'dim'; size?: number }) {
  const s = (cents / 100).toFixed(2).padStart(5, ' ')
  return (
    <span style={{ fontSize: size }}>
      <Flap text={s} tone={tone} label={`${eur(cents)}`} />
    </span>
  )
}

const MATCH_STEPS: [string, string][] = [['matched', 'Matched'], ['held', 'PayPal hold'], ['captured', 'Paid at pickup']]

export function MatchTimeline({ m }: { m: Match }) {
  const order = ['pending', 'held', 'captured']
  if (['voided', 'no_show', 'failed'].includes(m.status)) {
    const label = m.status === 'failed' ? 'Hold refused' : m.status === 'no_show' ? 'Not collected' : 'Hold released'
    return (
      <div className="timeline" aria-label={`status ${m.status}`}>
        <span className="done">Matched</span><span className="bad">{label}</span>
      </div>
    )
  }
  const reached = m.status === 'refunded' ? 2 : Math.max(0, order.indexOf(m.status))
  return (
    <div className="timeline" aria-label={`status ${m.status}`}>
      {MATCH_STEPS.map(([k, label], i) => (
        <span key={k} className={i <= reached ? 'done' : ''}>{label}</span>
      ))}
      {m.paypal.refunded > 0 && <span className="done">Refunded {eur(m.paypal.refunded)}</span>}
    </div>
  )
}

export function StatusChip({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    open: ['live', 'ok'], upcoming: ['soon', 'info'], sold_out: ['sold', 'sold'], threshold_met: ['go', 'sold'],
    deadline: ['closed', ''], threshold_failed: ['called off', 'bad'], withdrawn: ['withdrawn', ''],
    pending: ['matched', 'info'], held: ['held', 'ok'], captured: ['paid', 'sold'], voided: ['released', ''],
    no_show: ['no-show', 'warn'], failed: ['refused', 'bad'], refunded: ['refunded', 'warn'],
    active: ['active', 'ok'], paused: ['paused', 'warn'], revoked: ['revoked', ''],
  }
  const [t, c] = map[status] ?? [status, '']
  return <span className={`chip ${c}`}>{t}</span>
}

export function Sim({ on }: { on: boolean }) {
  return on ? <span className="chip sim" title="Part of the simulated demo market">simulated</span> : null
}

export function Section({ title, aside, children }: { title: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <div className="card-head"><h2>{title}</h2>{aside}</div>
      {children}
    </section>
  )
}

export function Spinner() { return <span className="spinner" aria-label="loading" /> }

export function Mark({ size = 26 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 64 64" aria-hidden>
      <rect width="64" height="64" rx="14" fill="#1e2730" />
      <rect x="10" y="14" width="44" height="16" rx="3" fill="#2b3640" />
      <rect x="10" y="34" width="44" height="16" rx="3" fill="#ffc23d" />
      <path d="M10 32h44" stroke="#0f1419" strokeWidth="2" />
    </svg>
  )
}
