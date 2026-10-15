import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { Session } from '../App'
import { api, type Draft, type Wish } from '../api'
import { Spinner } from '../components'
import { DraftCard } from './Buyer'

export default function Sign({ session }: { session: Session }) {
  const { draftId } = useParams()
  const [draft, setDraft] = useState<Draft | null>(null)
  const [error, setError] = useState('')
  const [signed, setSigned] = useState<Wish | null>(null)
  useEffect(() => {
    api.get<Draft>(`/api/drafts/${draftId}`).then(setDraft).catch((e) => setError(e.message))
  }, [draftId, session.account?.id])
  return (
    <main className="page narrow stack loose">
      <div className="page-head"><div><h1>Your agent drafted a wish</h1>
        <p className="lede">Nothing is bought until you sign. Read the sentence: it is generated from the exact terms your signature covers.</p></div></div>
      {error && <div className="notice bad">{error}</div>}
      {!draft && !error && <Spinner />}
      {!session.account && draft && <div className="notice warn">Open this link in the browser where you use Criée, so the wish is signed by you.</div>}
      {draft && !signed && <DraftCard session={session} draft={draft} onSigned={setSigned} />}
      {signed && (
        <div className="card stack fade-in">
          <h2>Signed</h2>
          <p className="muted" style={{ margin: 0 }}>{signed.readback}</p>
          <Link to="/wish" className="btn primary" style={{ alignSelf: 'flex-start' }}>See your wishes</Link>
        </div>
      )}
    </main>
  )
}
