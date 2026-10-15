import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { Session } from '../App'
import { ATTR, CATEGORY, MODE, NEIGHBOURHOODS, api, eur, hhmm, type Draft, type Wish } from '../api'
import { MatchTimeline, Section, Spinner, StatusChip } from '../components'
import { toast, useLive } from '../hooks'
import { deviceSign, passkeySign, passkeysAvailable, registerPasskey } from '../signing'

const EXAMPLES = [
  'An organic sourdough loaf twice a week, under €5, after 18:00 on weekdays',
  'Deux croissants le samedi matin, 1,20 € maximum pièce',
  'A vegetarian dinner for one, up to €6, any evening near Krutenau',
  'A 10 kg vegetable box from a local farm, up to €28',
]

export function TermChips({ t }: { t: any }) {
  return (
    <div className="row wrap" style={{ gap: 6 }}>
      <span className="chip">{CATEGORY[t.category]}</span>
      <span className="chip">≤ {eur(t.limit_cents)} each</span>
      <span className="chip">{t.units} per match</span>
      <span className="chip">≤ {t.max_matches_per_week}/week · {eur(t.budget_cents_per_week)}</span>
      <span className="chip">{(t.radius_m / 1000).toFixed(1)} km</span>
      {t.constraints.map((c: any) => (
        <span key={c.attr + c.op} className="chip info">{ATTR[c.attr] ?? c.attr}{c.op === 'is' ? (c.value ? '' : ': no') : ` ${c.op === 'gte' ? '≥' : '≤'} ${c.value}`}</span>
      ))}
      {t.modes.map((m: string) => <span key={m} className="chip">{MODE[m].en}</span>)}
    </div>
  )
}

export async function signDraft(session: Session, draft: Draft, method: 'passkey' | 'device'): Promise<Wish> {
  const acc = session.account!
  if (method === 'device') {
    const sig = await deviceSign(acc, draft.digest)
    return api.post<Wish>(`/api/drafts/${draft.id}/sign`, sig)
  }
  if (!acc.credentials.some((c) => c.kind === 'passkey')) {
    await registerPasskey()
    await session.reload()
  }
  const fresh = await api.get<Draft>(`/api/drafts/${draft.id}`)
  const assertion = await passkeySign(fresh.passkey)
  return api.post<Wish>(`/api/drafts/${draft.id}/sign`, { kind: 'passkey', assertion })
}

function PayPalSetup({ session }: { session: Session }) {
  const acc = session.account!
  const [busy, setBusy] = useState('')
  if (acc.vaulted) {
    return (
      <div className="row between wrap">
        <div className="row"><span className="dot" /><span><b>{acc.vault_label ?? 'PayPal saved'}</b> <span className="muted small">· charged only when a match clears, inside what you signed</span></span></div>
      </div>
    )
  }
  const start = async () => {
    setBusy('paypal')
    try {
      const r = await api.post<{ approve_url: string }>('/api/vault/start')
      window.location.href = r.approve_url
    } catch (e: any) { toast(e.message, true); setBusy('') }
  }
  const demo = async () => {
    setBusy('demo')
    try { await api.post('/api/vault/demo'); await session.reload(); toast('Demo PayPal wallet saved') } catch (e: any) { toast(e.message, true) }
    setBusy('')
  }
  return (
    <div className="stack">
      <div><b>Save PayPal once.</b> <span className="muted">Criée takes a hold only when a match clears, within the limits you sign.</span></div>
      <div className="row wrap">
        <button className="btn primary" onClick={start} disabled={!!busy}>{busy === 'paypal' ? <Spinner /> : null} Save my PayPal (sandbox)</button>
        <button className="btn" onClick={demo} disabled={!!busy}>Use the demo wallet</button>
      </div>
      <div className="small faint">Save my PayPal opens the PayPal sandbox: log in with any sandbox personal account.</div>
    </div>
  )
}

function Composer({ onDraft }: { onDraft: (d: Draft) => void }) {
  const [text, setText] = useState('')
  const [place, setPlace] = useState(NEIGHBOURHOODS[1])
  const [busy, setBusy] = useState(false)
  const [questions, setQuestions] = useState<string[]>([])
  const locate = () => {
    navigator.geolocation?.getCurrentPosition((p) => {
      const near = [...NEIGHBOURHOODS].sort((a, b) => Math.hypot(a[1] - p.coords.latitude, a[2] - p.coords.longitude) - Math.hypot(b[1] - p.coords.latitude, b[2] - p.coords.longitude))[0]
      setPlace(['my location', p.coords.latitude, p.coords.longitude])
      toast(`Using your location (near ${near[0]})`)
    }, () => toast('Location not available', true))
  }
  const go = async () => {
    if (text.trim().length < 3) return
    setBusy(true); setQuestions([])
    try {
      const r = await api.post<any>('/api/wishes/compile', { text, lat: place[1], lon: place[2], place: place[0] })
      if (r.questions) setQuestions(r.questions)
      else onDraft(r as Draft)
    } catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  return (
    <div className="stack">
      <label className="field">What would you buy, at what price, and when can you collect?
        <textarea className="input" value={text} onChange={(e) => setText(e.target.value)} maxLength={600}
          placeholder="An organic sourdough loaf twice a week, under €5, after 18:00"
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) go() }} />
      </label>
      <div className="examples">{EXAMPLES.map((x) => <button key={x} onClick={() => setText(x)}>{x}</button>)}</div>
      {questions.length > 0 && <div className="notice warn">{questions.join(' ')}</div>}
      <div className="row wrap between">
        <div className="row">
          <label className="sr-only" htmlFor="place">Pickup area</label>
          <select id="place" className="input" style={{ width: 'auto' }} value={place[0]}
            onChange={(e) => setPlace(NEIGHBOURHOODS.find((n) => n[0] === e.target.value) ?? NEIGHBOURHOODS[0])}>
            {place[0] === 'my location' && <option value="my location">My location</option>}
            {NEIGHBOURHOODS.map((n) => <option key={n[0]} value={n[0]}>Near {n[0]}</option>)}
          </select>
          <button className="btn ghost small" onClick={locate}>Use my location</button>
        </div>
        <button className="btn primary" onClick={go} disabled={busy || text.trim().length < 3}>{busy ? <Spinner /> : null} Read it back</button>
      </div>
    </div>
  )
}

export function DraftCard({ session, draft, onSigned, onEdit }: { session: Session; draft: Draft; onSigned: (w: Wish) => void; onEdit?: () => void }) {
  const [busy, setBusy] = useState('')
  const sign = async (method: 'passkey' | 'device') => {
    setBusy(method)
    try { const w = await signDraft(session, draft, method); toast('Signed. Your wish is standing.'); onSigned(w) } catch (e: any) { toast(e.message ?? 'Not signed', true) }
    setBusy('')
  }
  return (
    <div className="card hero fade-in stack">
      <div className="row between"><span className="upper muted">Read it back · this is what you sign</span>
        <span className="chip" title="Where the draft came from">{draft.via === 'mcp' ? 'drafted by your agent' : draft.source === 'rules' ? 'parsed by rules' : 'drafted by Gemini'}</span></div>
      <p className="readback" style={{ margin: 0 }}>{draft.readback}</p>
      <TermChips t={draft.terms} />
      <div className="small faint mono" style={{ overflowWrap: 'anywhere' }}>mandate sha-256 {draft.digest}</div>
      <div className="row wrap">
        {passkeysAvailable() && <button className="btn signal big" onClick={() => sign('passkey')} disabled={!!busy || !session.account}>{busy === 'passkey' ? <Spinner /> : null} Sign with passkey</button>}
        <button className="btn big" onClick={() => sign('device')} disabled={!!busy || !session.account}>{busy === 'device' ? <Spinner /> : null} Sign with this browser's key</button>
        {onEdit && <button className="btn ghost" onClick={onEdit}>Change</button>}
      </div>
    </div>
  )
}

function WishCard({ w, reload }: { w: Wish; reload: () => void }) {
  const [open, setOpen] = useState(false)
  const act = async (a: string) => { try { await api.post(`/api/wishes/${w.id}/${a}`); reload() } catch (e: any) { toast(e.message, true) } }
  const live = w.matches.filter((m) => ['pending', 'held'].includes(m.status))
  const pct = Math.min(100, (100 * w.week.spent) / w.week.budget)
  return (
    <div className="card stack">
      <div className="row between">
        <div className="stack tight">
          <h3>{w.label}</h3>
          <span className="small muted">≤ {eur(w.terms.limit_cents)} · {w.week.matches}/{w.week.max_matches} this week</span>
        </div>
        <StatusChip status={w.status} />
      </div>
      <div className="progress" title={`${eur(w.week.spent)} of ${eur(w.week.budget)} this week`}><i style={{ width: `${pct}%` }} /></div>
      {live.map((m) => (
        <div key={m.id} className="notice ok stack tight fade-in">
          <div className="row between wrap"><b>{m.units} × {m.name} · {m.shop.name}</b><span className="mono">{eur(m.total)}</span></div>
          <div className="row between wrap">
            <div className="pickup-code" aria-label={`pickup code ${m.pickup_code}`}>{m.pickup_code}</div>
            <div className="small muted">collect by {hhmm(m.pickup_by ?? m.pickup_until)}</div>
          </div>
          <MatchTimeline m={m} />
        </div>
      ))}
      {w.commitments.map((c) => (
        <div key={c.lot_id} className="notice stack tight fade-in">
          <div className="row between wrap"><b>Committed to {c.name} · {c.shop}</b><span className="mono">{eur(c.price)}</span></div>
          <div className="progress"><i style={{ width: `${Math.min(100, (100 * c.committed) / c.target)}%` }} /></div>
          <span className="small muted">{c.committed} of {c.target} committed. When it reaches {c.target}, PayPal takes every hold in one burst; if it does not by {new Date(c.closes_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'long' })}, nobody pays.</span>
        </div>
      ))}
      {w.status === 'active' && live.length === 0 && w.commitments.length === 0 && (
        <div className="small muted">
          Watching {w.watching.open_lots} open lot{w.watching.open_lots === 1 ? '' : 's'} of this kind
          {w.watching.reasons.length > 0 && ': ' + w.watching.reasons.slice(0, 3).map((r) => `${r.lots} ${r.text}`).join(', ')}.
        </div>
      )}
      <div className="row wrap between">
        <button className="btn ghost small" onClick={() => setOpen(!open)}>{open ? 'Hide details' : `History and signature (${w.matches.length})`}</button>
        <div className="row">
          {w.status === 'active' && <button className="btn small" onClick={() => act('pause')}>Pause</button>}
          {w.status === 'paused' && <button className="btn small" onClick={() => act('resume')}>Resume</button>}
          {w.status !== 'revoked' && <button className="btn small" onClick={() => act('revoke')}>Revoke</button>}
        </div>
      </div>
      {open && (
        <div className="stack fade-in">
          <p className="small" style={{ margin: 0 }}>{w.readback}</p>
          <dl className="kv">
            <dt>Signed</dt><dd>{new Date(w.signed_at).toLocaleString('en-GB')} · {w.signature.kind}</dd>
            <dt>Mandate hash</dt><dd>{w.terms_hash}</dd>
          </dl>
          {w.matches.map((m) => (
            <div key={m.id} className="row between wrap small" style={{ borderTop: '1px solid var(--line)', paddingTop: 8 }}>
              <span>{m.units} × {m.name} · {m.shop.name} · {eur(m.total)} <span className="faint">({MODE[m.rule].en.toLowerCase()})</span></span>
              <span className="row"><StatusChip status={m.status} />{m.paypal.order_id && <span className="mono tiny faint">PayPal {m.paypal.order_id}</span>}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function AgentPanel() {
  const [tok, setTok] = useState<{ token: string; mcp_url: string } | null>(null)
  const make = async () => { try { setTok(await api.post('/api/agent-token')) } catch (e: any) { toast(e.message, true) } }
  const cfg = tok ? JSON.stringify({ mcpServers: { criee: { type: 'http', url: tok.mcp_url, headers: { Authorization: `Bearer ${tok.token}` } } } }, null, 2) : ''
  return (
    <Section title="Let your agent hold wishes">
      <div className="stack">
        <p className="small muted" style={{ margin: 0 }}>Any MCP client can draft, list, pause and revoke your wishes and read your pickup codes. It cannot sign: every new wish comes back to you as a link to approve with your passkey.</p>
        {!tok ? <button className="btn" onClick={make} style={{ alignSelf: 'flex-start' }}>Create an agent token</button> : (
          <>
            <pre className="input mono" style={{ fontSize: 12, whiteSpace: 'pre-wrap', margin: 0 }}>{cfg}</pre>
            <button className="btn small" style={{ alignSelf: 'flex-start' }} onClick={() => { navigator.clipboard.writeText(cfg); toast('Copied') }}>Copy MCP config</button>
          </>
        )}
      </div>
    </Section>
  )
}

export default function Buyer({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams()
  const [draft, setDraft] = useState<Draft | null>(null)
  const { data, reload } = useLive<{ wishes: Wish[] }>(session.account ? '/api/wishes' : null)
  useEffect(() => {
    const v = params.get('vault')
    if (v === 'ok') { toast('PayPal saved for future matches'); session.reload() }
    if (v === 'failed') toast(`PayPal did not save: ${params.get('reason') ?? ''}`, true)
    if (v) { params.delete('vault'); setParams(params, { replace: true }) }
  }, [])
  const start = async () => { await api.post('/api/session', { role: 'buyer' }); await session.reload() }
  if (!session.account) {
    return (
      <main className="page narrow stack loose">
        <div className="page-head"><div><h1>Wish once</h1><p className="lede">Say what you would buy and at what price. Criée watches every shop nearby and buys it the moment a seller clears below your limit.</p></div></div>
        <button className="btn signal big" onClick={start} style={{ alignSelf: 'flex-start' }}>Start</button>
      </main>
    )
  }
  return (
    <main className="page narrow stack loose">
      <div className="page-head"><div><h1>Your standing wishes</h1><p className="lede">Each one is a limit order you sign once. Criée buys only inside it, and PayPal settles at pickup.</p></div></div>
      <Section title="PayPal"><PayPalSetup session={session} /></Section>
      {draft ? <DraftCard session={session} draft={draft} onSigned={() => { setDraft(null); reload() }} onEdit={() => setDraft(null)} />
        : <Section title="New wish"><Composer onDraft={setDraft} /></Section>}
      <div className="stack">
        {(data?.wishes ?? []).map((w) => <WishCard key={w.id} w={w} reload={reload} />)}
        {data && data.wishes.length === 0 && !draft && <div className="empty">No wish yet. Describe one above.</div>}
      </div>
      <AgentPanel />
    </main>
  )
}
