import { Link } from 'react-router-dom'
import type { Session } from '../App'
import { CATEGORY, MODE, eur, type Lot } from '../api'
import { PriceFlap, Sim, StatusChip } from '../components'
import { useLive } from '../hooks'

export default function Home({ session }: { session: Session }) {
  const { data } = useLive<{ lots: Lot[]; totals: any }>('/api/board')
  const open = (data?.lots ?? []).filter((l) => l.status === 'open' || l.status === 'sold_out').slice(0, 5)
  return (
    <main className="page">
      <section className="stack loose" style={{ paddingTop: 18 }}>
        <div className="upper muted">Criée · Strasbourg</div>
        <h1 className="hero-title">What small sellers have left at closing, sold to people who <em>already asked for it</em>.</h1>
        <p className="lede">
          People leave standing wishes, by chat or through their own AI agent, and sign them once with a passkey.
          Bakers and grocers film their shelf before closing. Criée clears the two like a market, and PayPal holds,
          captures and pays out each match the moment it clears.
        </p>
        <div className="row wrap">
          <Link to="/play" className="btn signal big">Play both sides</Link>
          <Link to="/board" className="btn big">Watch the board</Link>
          {session.status && <span className="small muted">PayPal {session.status.paypal} · {session.status.ledger.events} signed ledger events</span>}
        </div>
      </section>

      <section className="card" style={{ marginTop: 34, padding: 0, overflow: 'hidden' }}>
        <div className="card-head" style={{ padding: '16px 18px 0' }}>
          <h2>Clearing now</h2>
          <Link to="/board" className="small muted">Full board →</Link>
        </div>
        {open.length === 0 ? <div className="empty" style={{ margin: 18 }}>The next lots open shortly.</div> : (
          <table className="board-table" style={{ padding: '0 10px 10px' }}>
            <tbody>
              {open.map((l) => (
                <tr key={l.id} className={l.status === 'sold_out' ? 'sold' : ''}>
                  <td><div className="board-name" style={{ fontSize: 17 }}>{l.name}<small>{l.shop.name} · {CATEGORY[l.category]}</small></div></td>
                  <td className="hide-sm small muted">{MODE[l.mode].en}</td>
                  <td><PriceFlap cents={l.price} tone={l.status === 'sold_out' ? 'amber' : undefined} size={18} /></td>
                  <td className="small">{l.mode === 'threshold' && l.threshold ? `${l.threshold.committed} / ${l.threshold.target}` : `${l.remaining} left`}</td>
                  <td><StatusChip status={l.status} /> <Sim on={l.simulated} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <div className="doors">
        <Link to="/wish" className="card door">
          <span className="k">01 · Buyer</span>
          <h2>Wish once</h2>
          <p className="muted small">"A gluten-free loaf, twice a week, under €5, after six." Read it back, sign with your fingerprint, save PayPal once. Your agent can do it for you over MCP.</p>
        </Link>
        <Link to="/sell" className="card door">
          <span className="k">02 · Seller</span>
          <h2>Film the shelf</h2>
          <p className="muted small">A photo and a sentence. Criée reads the labels, keeps only facts it can point to, and opens a falling clock, a closing auction or a group order.</p>
        </Link>
        <Link to="/board" className="card door">
          <span className="k">03 · Everyone</span>
          <h2>Watch it clear</h2>
          <p className="muted small">The departures board of the city's leftovers: live prices, wishes waiting, and every hold, capture and payout as it lands.</p>
        </Link>
      </div>

      <section className="grid-2" style={{ marginTop: 34 }}>
        <div className="card">
          <h2 style={{ marginBottom: 14 }}>How a match settles</h2>
          <div className="step-list">
            <div><div><b>A signed limit order.</b> <span className="muted">Your wish is an intent mandate: item, conditions, price ceiling, weekly cap, pickup area and hours, signed with your passkey.</span></div></div>
            <div><div><b>A hold, with nobody at the screen.</b> <span className="muted">When a lot clears, PayPal authorises your saved wallet for the exact amount of the match. Each hold carries an idempotency key, so a retry never takes a second payment.</span></div></div>
            <div><div><b>Captured at the counter.</b> <span className="muted">The seller scans your pickup code: the hold is captured and the pickup attached to the PayPal order.</span></div></div>
            <div><div><b>The seller is paid the same minute.</b> <span className="muted">PayPal Payouts sends the takings minus a {session.status ? (session.status.fee_bps / 100).toFixed(0) : 4}% fee. A no-show releases the hold and the item goes back on the clock.</span></div></div>
          </div>
        </div>
        <div className="card">
          <h2 style={{ marginBottom: 14 }}>Where the AI works, and where it stops</h2>
          <div className="stack">
            <p className="muted small" style={{ margin: 0 }}>Gemini reads words, shelves and voice notes. Deterministic code decides who matches and what they pay. PayPal moves the money.</p>
            <dl className="kv">
              <dt>Wish</dt><dd>model drafts → code renders the sentence you sign</dd>
              <dt>Shelf</dt><dd>model reads → facts kept only with an image crop</dd>
              <dt>Clearing</dt><dd>no model: typed fields, seeded and replayable</dd>
              <dt>Advice</dt><dd>code computes → model writes, every number checked</dd>
              <dt>Disputes</dt><dd>evidence from the signed ledger only</dd>
            </dl>
            <p className="small faint" style={{ margin: 0 }}>Totals this week: {data ? `${data.totals.units_week} items, ${eur(data.totals.value_week)}` : '…'} · simulated market labelled as such.</p>
          </div>
        </div>
      </section>
    </main>
  )
}
