import { useEffect, useRef, useState } from 'react'
import type { Session } from '../App'
import { ATTR, CATEGORY, MODE, NEIGHBOURHOODS, api, eur, factText, hhmm, type Lot, type Match } from '../api'
import { MatchTimeline, PriceFlap, Section, Spinner, StatusChip } from '../components'
import { toast, useLive, useNow } from '../hooks'

type DraftLot = {
  name: string; name_fr: string | null; category: string; quantity: number; unit: string; price: number | null
  price_source: string; image_id: string | null; box: number[] | null; facts: any[]; crops: any[]; dropped: string[]
}
type ReadResult = { source: string; transcript: string; floor: number | null; close_hhmm: string | null; mode_hint: string | null; image_ids: string[]; lots: DraftLot[] }

function ShopSetup({ session }: { session: Session }) {
  const [name, setName] = useState('')
  const [hood, setHood] = useState('Krutenau')
  const [email, setEmail] = useState('')
  const save = async () => {
    const n = NEIGHBOURHOODS.find((x) => x[0] === hood)!
    try {
      await api.post('/api/shop', { name, place: hood, lat: n[1], lon: n[2], payout_email: email || null })
      await session.reload()
    } catch (e: any) { toast(e.message, true) }
  }
  return (
    <Section title="Your shop">
      <div className="stack">
        <label className="field">Shop name<input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Boulangerie de la Krutenau" /></label>
        <label className="field">Where buyers collect
          <select className="input" value={hood} onChange={(e) => setHood(e.target.value)}>{NEIGHBOURHOODS.map((n) => <option key={n[0]}>{n[0]}</option>)}</select></label>
        <label className="field">PayPal email for payouts <span className="faint">(empty: the sandbox seller account)</span>
          <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@business.example.com" /></label>
        <button className="btn primary" disabled={name.trim().length < 2} onClick={save}>Save shop</button>
      </div>
    </Section>
  )
}

function useRecorder() {
  const [rec, setRec] = useState<MediaRecorder | null>(null)
  const [blob, setBlob] = useState<Blob | null>(null)
  const [secs, setSecs] = useState(0)
  const timer = useRef<number | null>(null)
  const start = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mime = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/mp4'
      const r = new MediaRecorder(stream, { mimeType: mime })
      const chunks: BlobPart[] = []
      r.ondataavailable = (e) => chunks.push(e.data)
      r.onstop = () => { setBlob(new Blob(chunks, { type: mime })); stream.getTracks().forEach((t) => t.stop()) }
      r.start(); setRec(r); setSecs(0); setBlob(null)
      timer.current = window.setInterval(() => setSecs((s) => s + 1), 1000)
    } catch { toast('Microphone not available', true) }
  }
  const stop = () => { rec?.stop(); setRec(null); if (timer.current) window.clearInterval(timer.current) }
  return { recording: !!rec, blob, secs, start, stop, clear: () => setBlob(null) }
}

function BoxedImage({ imageId, lots }: { imageId: string; lots: DraftLot[] }) {
  return (
    <div style={{ position: 'relative' }}>
      <img className="shelf-img" src={`/api/images/${imageId}.jpg`} alt="Your shelf" />
      {lots.filter((l) => l.box && l.image_id === imageId).map((l, i) => {
        const [y0, x0, y1, x1] = l.box!
        return (
          <div key={i} style={{ position: 'absolute', top: `${y0 / 10}%`, left: `${x0 / 10}%`, width: `${(x1 - x0) / 10}%`, height: `${(y1 - y0) / 10}%`, border: '2px solid var(--sold)', borderRadius: 6, boxShadow: '0 0 0 2000px rgba(0,0,0,0)' }}>
            <span style={{ position: 'absolute', top: -22, left: -2, background: 'var(--sold)', color: 'var(--sold-ink)', fontSize: 11.5, fontWeight: 700, padding: '2px 6px', borderRadius: 5, whiteSpace: 'nowrap' }}>{i + 1} · {l.name}</span>
          </div>
        )
      })}
    </div>
  )
}

function LotEditor({ d, index, defaults, onListed }: { d: DraftLot; index: number; defaults: { floor: number | null; mode: string | null }; onListed: () => void }) {
  const [name, setName] = useState(d.name)
  const [qty, setQty] = useState(d.quantity)
  const [category, setCategory] = useState(d.category)
  const [price, setPrice] = useState(((d.price ?? 400) / 100).toFixed(2))
  const [floor, setFloor] = useState((((defaults.floor ?? Math.round((d.price ?? 400) * 0.5))) / 100).toFixed(2))
  const [mode, setMode] = useState<string>(defaults.mode ?? 'clock')
  const [minutes, setMinutes] = useState(30)
  const [threshold, setThreshold] = useState(Math.max(2, Math.round(d.quantity * 0.8)))
  const [confirm, setConfirm] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const unknown = d.dropped.map((x) => x.split(':')[0]).filter((a) => ATTR[a] && !d.facts.some((f) => f.attr === a && f.value !== null))
  const list = async () => {
    setBusy(true)
    const toCents = (s: string) => Math.round(parseFloat(s.replace(',', '.')) * 100)
    const facts = [...d.facts, ...confirm.map((a) => ({ attr: a, value: ['weight_g', 'best_before_hours', 'servings', 'duration_min'].includes(a) ? null : true, evidence: null }))]
    try {
      await api.post('/api/lots', {
        name, name_fr: d.name_fr, unit: d.unit, category, quantity: qty, mode, start: toCents(price),
        floor: mode === 'threshold' ? toCents(price) : Math.min(toCents(floor), toCents(price)),
        duration_min: mode === 'threshold' ? 60 * 24 * 7 : minutes, pickup_after_min: mode === 'threshold' ? 60 * 24 * 8 : 0,
        pickup_window_min: mode === 'threshold' ? 240 : minutes + 60, threshold: mode === 'threshold' ? threshold : null,
        facts, crops: d.crops, image_id: d.image_id, confirm_facts: confirm,
      })
      setDone(true); toast(`${name} is on the board`); onListed()
    } catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  if (done) return <div className="notice ok">{name}: listed. Follow it in Live.</div>
  return (
    <div className="card stack fade-in">
      <div className="row between"><h3>{index + 1} · {d.name_fr || d.name}</h3><span className="chip">{d.price_source === 'none' ? 'price to set' : `price from ${d.price_source}`}</span></div>
      <div className="grid-2">
        <label className="field">Name<input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field">Kind<select className="input" value={category} onChange={(e) => setCategory(e.target.value)}>{Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      </div>
      {(d.facts.length > 0 || unknown.length > 0) && (
        <div className="stack tight">
          <span className="small muted">Facts buyers can rely on</span>
          <div className="crops">
            {d.facts.filter((f) => f.value !== null).map((f) => {
              const crop = d.crops.find((c) => c.id === f.evidence)
              return (
                <div key={f.attr} className="crop">
                  {crop ? <img src={`/api/images/${crop.id}.jpg`} alt={`Label showing ${crop.label}`} /> : <span className="chip">said by you</span>}
                  <span className="chip ok">{factText(f)}</span>
                </div>
              )
            })}
          </div>
          {unknown.length > 0 && (
            <div className="notice small">
              Not shown on a label, so stored as unknown: {unknown.map((a) => (
                <label key={a} className="chip" style={{ cursor: 'pointer', marginRight: 6 }}>
                  <input type="checkbox" checked={confirm.includes(a)} onChange={(e) => setConfirm(e.target.checked ? [...confirm, a] : confirm.filter((x) => x !== a))} /> I confirm: {ATTR[a]}
                </label>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="seg" role="group" aria-label="Clearing mode">
        {Object.keys(MODE).map((m) => <button key={m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>{MODE[m].en}</button>)}
      </div>
      <span className="small muted">{MODE[mode].hint}</span>
      <div className="grid-3">
        <label className="field">Units<input className="input mono" type="number" min={1} value={qty} onChange={(e) => setQty(+e.target.value)} /></label>
        <label className="field">{mode === 'threshold' ? 'Price' : 'Start price €'}<input className="input mono" inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} /></label>
        {mode === 'threshold'
          ? <label className="field">Goes ahead at<input className="input mono" type="number" min={2} max={qty} value={threshold} onChange={(e) => setThreshold(+e.target.value)} /></label>
          : <label className="field">Lowest € <input className="input mono" inputMode="decimal" value={floor} onChange={(e) => setFloor(e.target.value)} /></label>}
      </div>
      {mode !== 'threshold' && (
        <label className="field">Sold out within <b className="mono" style={{ color: 'var(--chalk)' }}>{minutes} min</b>
          <input type="range" min={5} max={120} step={5} value={minutes} onChange={(e) => setMinutes(+e.target.value)} /></label>
      )}
      <button className="btn signal big block" disabled={busy || !name} onClick={list}>{busy ? <Spinner /> : null} {mode === 'clock' ? 'Start the clock' : mode === 'call' ? 'Open the auction' : 'Open the group order'}</button>
    </div>
  )
}

function ShelfTab({ onListed }: { onListed: () => void }) {
  const [files, setFiles] = useState<File[]>([])
  const [result, setResult] = useState<ReadResult | null>(null)
  const [busy, setBusy] = useState(false)
  const mic = useRecorder()
  const previews = files.map((f) => URL.createObjectURL(f))
  const read = async () => {
    setBusy(true)
    const fd = new FormData()
    files.forEach((f) => fd.append('images', f))
    if (mic.blob) fd.append('audio', mic.blob, 'note.webm')
    try { setResult(await api.form<ReadResult>('/api/supply/read', fd)) } catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  if (result) {
    return (
      <div className="stack loose">
        <div className="row between"><h2>{result.lots.length} item{result.lots.length === 1 ? '' : 's'} found</h2>
          <button className="btn ghost small" onClick={() => { setResult(null); setFiles([]); mic.clear() }}>New shelf</button></div>
        {result.image_ids[0] && <BoxedImage imageId={result.image_ids[0]} lots={result.lots} />}
        {result.transcript && <div className="notice small"><span className="muted">You said: </span>“{result.transcript}”</div>}
        <span className="small faint">Read by {result.source === 'manual' ? 'you (no model available)' : result.source === 'recorded' ? 'Gemini (recorded run)' : 'Gemini'}. Facts are kept only when they point to a label crop or to your own words.</span>
        {result.lots.map((d, i) => <LotEditor key={i} d={d} index={i} defaults={{ floor: result.floor, mode: result.mode_hint }} onListed={onListed} />)}
      </div>
    )
  }
  return (
    <div className="stack loose">
      <div className="card stack">
        <h2>Film what is left</h2>
        <p className="small muted" style={{ margin: 0 }}>Take one to four photos of the shelf, with the price signs in view. Then say the prices or anything a label does not show.</p>
        <label className="btn primary big block" style={{ cursor: 'pointer' }}>
          <input type="file" accept="image/*" capture="environment" multiple hidden onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, 4))} />
          {files.length ? `Retake (${files.length} photo${files.length > 1 ? 's' : ''})` : 'Take photos'}
        </label>
        {previews.length > 0 && <div className="row wrap">{previews.map((p, i) => <img key={i} src={p} alt="" style={{ height: 84, borderRadius: 8, border: '1px solid var(--line)' }} />)}</div>}
        <div className="row between">
          {mic.recording
            ? <button className="btn big" onClick={mic.stop}><span className="rec" /> Stop · {mic.secs}s</button>
            : <button className="btn big" onClick={mic.start}>{mic.blob ? 'Record again' : 'Add a voice note'}</button>}
          {mic.blob && !mic.recording && <span className="chip ok">voice note ready</span>}
        </div>
        <button className="btn signal big block" disabled={busy || files.length === 0} onClick={read}>{busy ? <><Spinner /> Reading the shelf…</> : 'Read my shelf'}</button>
      </div>
    </div>
  )
}

function Keypad({ onScanned }: { onScanned: (m: Match) => void }) {
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const press = (k: string) => setCode((c) => (k === '⌫' ? c.slice(0, -1) : c.length < 6 ? c + k : c))
  const go = async () => {
    setBusy(true)
    try { const m = await api.post<Match>('/api/pickups/scan', { code }); setCode(''); onScanned(m) } catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  return (
    <div className="stack">
      <div className="code-display" aria-live="polite">{code.padEnd(6, '·')}</div>
      <div className="keypad">{['1', '2', '3', '4', '5', '6', '7', '8', '9', '⌫', '0', 'OK'].map((k) => (
        <button key={k} onClick={() => (k === 'OK' ? go() : press(k))} disabled={k === 'OK' && (code.length !== 6 || busy)} aria-label={k === '⌫' ? 'delete' : k}>{k}</button>
      ))}</div>
    </div>
  )
}

function LiveTab() {
  const { data, reload } = useLive<{ lots: Lot[] }>('/api/lots/mine')
  const now = useNow(1000)
  const [last, setLast] = useState<Match | null>(null)
  const open = (data?.lots ?? []).filter((l) => !['deadline', 'threshold_failed', 'withdrawn'].includes(l.status))
  const sales = (data?.lots ?? []).flatMap((l) => l.matches ?? []).sort((a, b) => b.at.localeCompare(a.at))
  const withdraw = async (id: string) => { try { await api.post(`/api/lots/${id}/withdraw`); reload() } catch (e: any) { toast(e.message, true) } }
  return (
    <div className="stack loose">
      {open.map((l) => {
        const left = l.clock?.next_at ? Math.max(0, Math.round((new Date(l.clock.next_at).getTime() - now) / 1000)) : null
        return (
          <div key={l.id} className="card stack">
            <div className="row between"><div><h3>{l.name}</h3><span className="small muted">{MODE[l.mode].en} · closes {hhmm(l.closes_at)}</span></div><StatusChip status={l.status} /></div>
            <div className="row between wrap">
              <PriceFlap cents={l.price} size={44} tone={l.status === 'sold_out' ? 'amber' : undefined} />
              <div className="stack tight" style={{ textAlign: 'right' }}>
                <span className="mono">{l.mode === 'threshold' && l.threshold ? `${l.threshold.committed} / ${l.threshold.target} committed` : `${l.sold} sold · ${l.remaining} left`}</span>
                <span className="small muted">{l.waiting} wish{l.waiting === 1 ? '' : 'es'} watching{left !== null && l.clock?.next_price != null ? ` · ${eur(l.clock.next_price)} in ${left}s` : ''}</span>
              </div>
            </div>
            {l.threshold?.burst && <div className="notice ok small">{l.threshold.burst.authorized} PayPal holds taken in {(l.threshold.burst.wall_ms / 1000).toFixed(1)} s, then captured.</div>}
            <button className="btn ghost small" style={{ alignSelf: 'flex-end' }} onClick={() => withdraw(l.id)}>Withdraw</button>
          </div>
        )
      })}
      {open.length === 0 && <div className="empty">Nothing clearing. List a shelf to start.</div>}
      <Section title="Pickup">
        {last && <div className="notice ok stack tight fade-in" style={{ marginBottom: 12 }}><b>{last.units} × {last.name} · {eur(last.total)}</b><span className="small">Code accepted. PayPal captures the hold and pays you out now.</span></div>}
        <Keypad onScanned={(m) => { setLast(m); reload() }} />
      </Section>
      <Section title="Today's sales">
        <div className="stack">
          {sales.slice(0, 20).map((m) => (
            <div key={m.id} className="stack tight" style={{ borderBottom: '1px solid var(--line)', paddingBottom: 10 }}>
              <div className="row between small"><span>{m.units} × {m.name}{m.simulated ? ' · simulated buyer' : ''}</span><span className="mono">{eur(m.total)}</span></div>
              <MatchTimeline m={m} />
            </div>
          ))}
          {sales.length === 0 && <span className="muted small">No sale yet.</span>}
        </div>
      </Section>
    </div>
  )
}

function Curve({ curve, qty }: { curve: [number, number][]; qty: number }) {
  if (!curve.length) return null
  const W = 460, H = 200, P = 34
  const maxP = Math.max(...curve.map((c) => c[0])), minP = Math.min(...curve.map((c) => c[0]))
  const maxU = Math.max(qty, ...curve.map((c) => c[1]))
  const x = (u: number) => P + (u / maxU) * (W - P - 10)
  const y = (p: number) => H - P - ((p - minP) / Math.max(1, maxP - minP)) * (H - P - 14)
  const pts = curve.map(([p, u]) => `${x(u)},${y(p)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Units wanted at each price" style={{ width: '100%', height: 'auto' }}>
      <line x1={P} y1={H - P} x2={W - 6} y2={H - P} stroke="var(--line-strong)" />
      <line x1={P} y1={8} x2={P} y2={H - P} stroke="var(--line-strong)" />
      <line x1={x(qty)} y1={8} x2={x(qty)} y2={H - P} stroke="var(--sold)" strokeDasharray="4 4" />
      <text x={x(qty) + 4} y={18} fill="var(--sold)" fontSize="11">your {qty}</text>
      <polyline points={pts} fill="none" stroke="var(--chalk)" strokeWidth="2" />
      {curve.map(([p, u]) => <circle key={p} cx={x(u)} cy={y(p)} r="2.5" fill="var(--chalk)" />)}
      <text x={P} y={H - 10} fill="var(--muted)" fontSize="11">units wanted at that price or more →</text>
      <text x={4} y={14} fill="var(--muted)" fontSize="11">{eur(maxP)}</text>
      <text x={4} y={H - P} fill="var(--muted)" fontSize="11">{eur(minP)}</text>
    </svg>
  )
}

function DemandTab() {
  const [category, setCategory] = useState('bread')
  const [qty, setQty] = useState(12)
  const { data } = useLive<{ sentence: string; source: string; numbers: any; curve: [number, number][]; hidden: boolean }>(`/api/advice?category=${category}&quantity=${qty}`, { filter: (e) => e.type.startsWith('wish.') })
  const [written, setWritten] = useState<{ key: string; sentence: string; source: string } | null>(null)
  useEffect(() => {
    const key = `${category}:${qty}`
    const id = window.setTimeout(() => {
      api.get<{ sentence: string; source: string }>(`/api/advice?category=${category}&quantity=${qty}&sentence=true`)
        .then((r) => setWritten({ key, sentence: r.sentence, source: r.source })).catch(() => {})
    }, 600)
    return () => window.clearTimeout(id)
  }, [category, qty])
  const shown = written && written.key === `${category}:${qty}` ? written : data ? { sentence: data.sentence, source: data.source } : null
  return (
    <div className="stack loose">
      <div className="row wrap">
        <select className="input" style={{ width: 'auto' }} value={category} onChange={(e) => setCategory(e.target.value)}>{Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <label className="row small muted">units on the shelf <input className="input mono" style={{ width: 80 }} type="number" min={1} value={qty} onChange={(e) => setQty(Math.max(1, +e.target.value))} /></label>
      </div>
      {!data ? <Spinner /> : (
        <div className="card stack">
          <p style={{ margin: 0, fontSize: 17 }} aria-live="polite">{shown?.sentence}</p>
          <span className="tiny faint">{shown?.source === 'computed' ? 'Written from the computed figures' : 'Sentence by Gemini; every number checked against the computed figures'}</span>
          {data.hidden ? <div className="notice small">The curve appears once five different buyers are waiting, so no single buyer can be read from it.</div> : <Curve curve={data.curve} qty={qty} />}
          <dl className="kv">
            <dt>Standing wishes</dt><dd>{data.numbers.standing_wishes} from {data.numbers.distinct_buyers} buyers</dd>
            <dt>One price</dt><dd>{data.numbers.single_price_eur ? `€${data.numbers.single_price_eur} sells ${data.numbers.units_cleared_at_single_price}` : '–'}</dd>
            <dt>Falling clock</dt><dd>{data.numbers.falling_clock_start_eur ? `€${data.numbers.falling_clock_start_eur} → €${data.numbers.falling_clock_floor_eur}, about €${data.numbers.revenue_falling_clock_eur}` : '–'}</dd>
            <dt>Tomorrow evening</dt><dd>{data.numbers.wishes_for_tomorrow_evening} wishes already cover it</dd>
          </dl>
        </div>
      )}
    </div>
  )
}

type Msg = { role: 'user' | 'model'; text: string; tools?: { name: string; via: string; ok?: boolean }[]; pending?: any; images?: string[] }

function AssistantTab() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs])
  const send = async (t: string) => {
    if (!t.trim()) return
    const next = [...msgs, { role: 'user' as const, text: t.trim() }]
    setMsgs(next); setText(''); setBusy(true)
    try {
      const r = await api.post<any>('/api/assistant', { messages: next.map(({ role, text }) => ({ role, text })) })
      setMsgs([...next, { role: 'model', text: r.reply, tools: r.tools, pending: r.pending, images: r.images }])
    } catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  const listen = () => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SR) return toast('Voice input is not available in this browser', true)
    const r = new SR(); r.lang = navigator.language || 'fr-FR'; r.interimResults = false
    r.onresult = (e: any) => send(e.results[0][0].transcript)
    r.start()
  }
  const confirm = async (p: any, i: number) => {
    try {
      await api.post('/api/refunds', { match_id: p.match_id, cents: p.cents, reason: p.reason })
      setMsgs((m) => m.map((x, j) => (j === i ? { ...x, pending: null } : x)).concat({ role: 'model', text: `Refunded ${eur(p.cents)} through PayPal.` }))
    } catch (e: any) { toast(e.message, true) }
  }
  return (
    <div className="card stack">
      <div className="chat" aria-live="polite">
        {msgs.length === 0 && <div className="small muted">Try: “How much did I take today?”, “Refund 2 euros to code 123456, one croissant was missing”, “Invoice Café Brant for 20 baguettes at 1.10 and show me the QR code”, “Any open disputes?”</div>}
        {msgs.map((m, i) => (
          <div key={i} className={`bubble ${m.role === 'user' ? 'me' : 'bot'}`}>
            {m.text}
            {m.tools && m.tools.length > 0 && <div className="toolchips">{m.tools.map((t, k) => <span key={k} className={`chip ${t.via === 'paypal_agent_toolkit' ? 'info' : ''}`}>{t.via === 'paypal_agent_toolkit' ? 'PayPal Agent Toolkit' : t.via === 'paypal_rest' ? 'PayPal REST' : 'Criée'} · {t.name}</span>)}</div>}
            {m.images?.map((src, k) => <img key={k} src={`data:image/png;base64,${src}`} alt="PayPal invoice QR code" style={{ display: 'block', width: 180, marginTop: 8, borderRadius: 8, background: '#fff', padding: 6 }} />)}
            {m.pending && <div className="row" style={{ marginTop: 8 }}><button className="btn signal small" onClick={() => confirm(m.pending, i)}>Confirm: {m.pending.summary}</button></div>}
          </div>
        ))}
        {busy && <div className="bubble bot"><Spinner /></div>}
        <div ref={end} />
      </div>
      <div className="row">
        <button className="btn" onClick={listen} aria-label="Speak">🎙</button>
        <input className="input grow" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send(text)} placeholder="Ask about sales, refunds, payouts, invoices…" />
        <button className="btn primary" onClick={() => send(text)} disabled={busy}>Send</button>
      </div>
    </div>
  )
}

export default function Seller({ session }: { session: Session }) {
  const [tab, setTab] = useState<'shelf' | 'live' | 'demand' | 'assistant'>('shelf')
  const start = async () => { await api.post('/api/session', { role: 'seller' }); await session.reload() }
  if (!session.account) {
    return (
      <main className="page phone stack loose">
        <div><h1>Sell what is left</h1><p className="lede">Film the shelf before closing. Criée sells it to the people already waiting nearby, and PayPal pays you as each buyer collects.</p></div>
        <button className="btn signal big" onClick={start}>Start selling</button>
      </main>
    )
  }
  if (!session.account.roles.includes('seller') || !session.account.shop) return <main className="page phone"><ShopSetup session={session} /></main>
  return (
    <main className="page phone">
      <div className="row between" style={{ marginBottom: 12 }}><h1 style={{ fontSize: 24 }}>{session.account.shop.name}</h1><span className="chip">{session.account.shop.place}</span></div>
      <div className="tabs" role="tablist">
        {(['shelf', 'live', 'demand', 'assistant'] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>
            {{ shelf: 'Shelf', live: 'Live & pickup', demand: 'Demand', assistant: 'Assistant' }[t]}
          </button>
        ))}
      </div>
      {tab === 'shelf' && <ShelfTab onListed={() => setTab('live')} />}
      {tab === 'live' && <LiveTab />}
      {tab === 'demand' && <DemandTab />}
      {tab === 'assistant' && <AssistantTab />}
    </main>
  )
}
