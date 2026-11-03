import { lazy, Suspense, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import type { Session } from '../App'
import { api, eur, type Lot, type Match } from '../api'
import { Spinner } from '../components'
import { useLive } from '../hooks'
import Buyer from './Buyer'
import Seller from './Seller'
import Ledger from './Ledger'

const Console = lazy(() => import('./Console'))
const STEPS = [
  { title: 'Wish & PayPal', role: 'Buyer', text: 'Read the prefilled wish back, then sign with a passkey or this browser’s key. The farm starts with 39 simulated commitments: yours can complete the group of 40. The shared demo wallet needs no login; the PayPal button lets you try your own sandbox account.' },
  { title: 'Read a shelf', role: 'Seller', text: 'You are now the seller. Choose “Use the sample shelf photo”, then “Read my shelf”. Inspect the label crops and prices, then start a clock on an item. The AI proposes; you confirm what goes on sale.' },
  { title: 'Find your pickup', role: 'Buyer', text: 'Back as the buyer, make a wish for an item on the board, or inspect an existing purchase. A held purchase shows a six-digit pickup code. Group orders capture when the target is reached, so they do not need this pickup step.' },
  { title: 'Collect & get paid', role: 'Seller', text: 'Enter a pickup code for a sale at your stall and press OK. PayPal captures the hold, adds collection tracking, then pays the seller minus the platform fee. If you only tried the group order, continue to its payment proof in the next step.' },
  { title: 'Inspect the proof', role: 'Auditor', text: 'Your console contains your stall’s sales, PayPal identifiers and payouts. Download an evidence PDF for a sale. The ledger also lets you check recent signatures quickly or audit the entire chain with live progress.' },
]
const ACTIONS = ['Make your wish', 'Read the shelf', 'See your purchases', 'Open the pickup keypad', 'Inspect the console']

export default function Tour({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams()
  const value = Number(params.get('step') ?? 0)
  const step = Number.isInteger(value) && value >= 0 && value < STEPS.length ? value : 0
  const current = STEPS[step]
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const ready = !!session.account?.roles.includes('seller') && !!session.account.shop && session.account.vaulted
  const { data: market } = useLive<{ lots: Lot[] }>('/api/board', { pollMs: 4000 })
  const { data: purchases } = useLive<{ matches: Match[] }>(ready ? '/api/matches' : null, { pollMs: 4000 })
  const { data: stall } = useLive<{ lots: Lot[] }>(ready ? '/api/lots/mine' : null, { pollMs: 4000 })
  const farm = market?.lots.find((l) => l.mode === 'threshold' && l.status === 'open' && !l.threshold?.reached)
  const group = purchases?.matches.find((m) => m.rule === 'threshold')
  const myLots = new Set(stall?.lots.map((l) => l.id))
  const ownPickup = purchases?.matches.find((m) => m.status === 'held' && myLots.has(m.lot_id) && m.pickup_code)
  const held = purchases?.matches.find((m) => m.status === 'held' && m.pickup_code)
  const pickup = ownPickup ?? held
  const burst = market?.lots.find((l) => l.id === group?.lot_id)?.threshold?.burst
  useEffect(() => {
    document.title = `${current.role}: ${current.title} · Criée tour`
    return () => { document.title = 'Criée' }
  }, [current])
  const go = (next: number) => { setParams({ step: String(next) }); window.scrollTo({ top: 0, behavior: 'instant' }) }
  const start = async () => {
    setBusy(true); setError('')
    try { await api.post('/api/play'); await session.reload() }
    catch (e: unknown) { setError((e as Error).message) }
    finally { setBusy(false) }
  }
  return <>
    <section className="tour-guide" aria-label="Guided judge tour">
      <div className="row between wrap">
        <span className="upper">Guided judge tour · {step + 1} / {STEPS.length}</span>
        <Link className="small muted" to="/play">Explore freely →</Link>
      </div>
      <ol className="tour-steps">{STEPS.map((s, i) => <li key={s.title}>
        <button onClick={() => go(i)} aria-current={i === step ? 'step' : undefined} className={i === step ? 'on' : ''}><span>{i + 1}</span>{s.title}</button>
      </li>)}</ol>
      <div className="row wrap"><span className="chip sold">You are the {current.role.toLowerCase()}</span><h1>{current.title}</h1></div>
      <p className="tour-instruction">{current.text}</p>
      {ready && step === 0 && farm?.threshold && <div className="notice small">Live group order: <b>{farm.threshold.committed} / {farm.threshold.target}</b> committed at {eur(farm.price)}. The count comes from the running market.</div>}
      {group && (step === 0 || step === 4) && <div className={`notice small ${['held', 'captured'].includes(group.status) ? 'ok' : group.status === 'failed' ? 'bad' : ''}`} role="status">Your group purchase: <b>{group.status}</b> · {eur(group.total)}{group.paypal.order_id && <> · PayPal order <span className="mono">{group.paypal.order_id}</span></>}{burst && <> · {burst.authorized}/{burst.holds} holds in {(burst.wall_ms / 1000).toFixed(2)} s · {session.status?.paypal} this run</>}</div>}
      {group?.paypal.capture_id && (step === 0 || step === 4) && <div className="row wrap"><span className="chip ok">Capture {group.paypal.capture_id}</span><a className="btn small" href={`/api/evidence/${group.id}.pdf`} target="_blank" rel="noreferrer">Open this purchase’s evidence PDF</a></div>}
      {step === 3 && pickup && <div className="notice small">{ownPickup ? 'Code for your stall' : `Code for ${pickup.shop.name}; only that seller can scan it`}: <b className="mono">{pickup.pickup_code}</b> · {pickup.name}</div>}
      {error && <div className="notice bad" role="alert">{error}</div>}
      {!ready ? <div className="stack">
        <p className="small muted">Start creates a buyer and seller session with a stall. Simulated traders are labelled. Payments use {session.status?.paypal ?? 'the configured PayPal mode'}.</p>
        <button className="btn signal" onClick={start} disabled={busy} style={{ alignSelf: 'flex-start' }}>{busy && <Spinner />} Start the guided tour</button>
      </div> : <div className="row between wrap">
        <button className="btn small" onClick={() => go(step - 1)} disabled={step === 0}>← Back</button>
        <a className="btn signal" href="#tour-app">{ACTIONS[step]} ↓</a>
        {step < STEPS.length - 1 ? <button className="btn small" onClick={() => go(step + 1)}>Next: {STEPS[step + 1].title} →</button> : <Link className="btn small" to="/board">Finish on the live board →</Link>}
      </div>}
    </section>
    {ready && <div className="tour-content" id="tour-app" key={step}>
      {step === 0 && <Buyer session={session} initialText="A 10 kg vegetable box from a local farm, up to €28" />}
      {step === 1 && <Seller session={session} />}
      {step === 2 && <Buyer session={session} />}
      {step === 3 && <Seller session={session} initialTab="live" />}
      {step === 4 && <><Suspense fallback={<div className="page"><Spinner /></div>}><Console session={session} /></Suspense><Ledger /></>}
    </div>}
  </>
}
