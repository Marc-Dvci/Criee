import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { Session } from '../App'
import { Spinner } from '../components'
import { toast } from '../hooks'
import { api } from '../api'

export default function Play({ session }: { session: Session }) {
  const [busy, setBusy] = useState(false)
  const ready = !!session.account?.roles.includes('seller') && !!session.account?.shop && session.account.vaulted
  const go = async () => {
    setBusy(true)
    try { await api.post('/api/play'); await session.reload(); toast('You are a buyer and a seller. The market is open.') } catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  return (
    <main className="page narrow stack loose">
      <div className="page-head"><div><h1>Play both sides</h1>
        <p className="lede">Be the buyer and the baker in the same browser. Your wishes clear against your own lots and a simulated Strasbourg market, and every hold, capture and payout runs on the PayPal sandbox.</p></div></div>
      {!ready ? (
        <div className="card stack">
          <p style={{ margin: 0 }}>One click creates your account with a demo PayPal wallet already saved, a stall on Place Kléber, and an open market of bakeries, a traiteur and a farm's group order.</p>
          <button className="btn signal big" onClick={go} disabled={busy} style={{ alignSelf: 'flex-start' }}>{busy ? <Spinner /> : null} Start</button>
        </div>
      ) : (
        <div className="stack">
          <div className="card stack">
            <span className="upper muted">Step 1 · buyer</span>
            <h2>Make a wish</h2>
            <p className="small muted" style={{ margin: 0 }}>Type what you would buy and your price. Read the sentence back and sign it. Try: "a 10 kg vegetable box from a local farm, up to €28": the farm's group order is three commitments short.</p>
            <Link className="btn primary" to="/wish" style={{ alignSelf: 'flex-start' }}>Open the buyer app</Link>
          </div>
          <div className="card stack">
            <span className="upper muted">Step 2 · seller</span>
            <h2>Sell to your own wish</h2>
            <p className="small muted" style={{ margin: 0 }}>Photograph any food with its label or a price written on paper. Criée lists it; start a falling clock and watch your wish clear it. Scan the pickup code from the buyer app on the Live tab.</p>
            <Link className="btn primary" to="/sell" style={{ alignSelf: 'flex-start' }}>Open the seller app</Link>
          </div>
          <div className="card stack">
            <span className="upper muted">Step 3 · everyone</span>
            <h2>Watch it settle</h2>
            <div className="row wrap"><Link className="btn" to="/board">Board</Link><Link className="btn" to="/console">Console</Link><Link className="btn" to="/ledger">Ledger</Link></div>
          </div>
          <p className="small faint">Simulated buyers and sellers are labelled on every screen. Their matches settle on the same PayPal sandbox path, on a shared demo wallet.</p>
        </div>
      )}
    </main>
  )
}
