import { lazy, Suspense, useEffect, useState } from 'react'
import { NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api, type Account, type Status } from './api'
import { Mark, Spinner } from './components'
import { useConnected, useToastHost } from './hooks'
import Home from './pages/Home'
import Board from './pages/Board'
import Buyer from './pages/Buyer'
import Seller from './pages/Seller'
import Sign from './pages/Sign'
import Ledger from './pages/Ledger'
import Play from './pages/Play'

const Console = lazy(() => import('./pages/Console'))

export type Session = { account: Account | null; status: Status | null; reload: () => Promise<void> }

export function useSession(): Session {
  const [account, setAccount] = useState<Account | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const reload = async () => {
    const [me, st] = await Promise.all([api.get<{ account: Account | null }>('/api/me'), api.get<Status>('/api/status')])
    setAccount(me.account)
    setStatus(st)
  }
  useEffect(() => { reload() }, [])
  return { account, status, reload }
}

function TopBar({ session }: { session: Session }) {
  const live = useConnected()
  const st = session.status
  return (
    <header className="topbar">
      <NavLink to="/" className="brand"><Mark /> Criée</NavLink>
      <nav className="nav" aria-label="Main">
        <NavLink to="/wish">Wish</NavLink>
        <NavLink to="/sell">Sell</NavLink>
        <NavLink to="/board">Board</NavLink>
        <NavLink to="/console">Console</NavLink>
        <NavLink to="/ledger">Ledger</NavLink>
      </nav>
      <div className="status-pill" title={st ? `PayPal ${st.paypal} · ledger ${st.ledger.events} events` : ''}>
        <span className={`dot ${live ? '' : 'off'}`} />
        <span className="hide-sm">{st ? `PayPal ${st.paypal}` : 'connecting'}</span>
        {session.account && <span className="chip">{session.account.handle}</span>}
      </div>
    </header>
  )
}

export default function App() {
  const session = useSession()
  const toast = useToastHost()
  const loc = useLocation()
  const bare = loc.pathname.startsWith('/board')
  return (
    <>
      {!bare && <TopBar session={session} />}
      <Suspense fallback={<div className="page"><Spinner /></div>}>
        <Routes>
          <Route path="/" element={<Home session={session} />} />
          <Route path="/wish" element={<Buyer session={session} />} />
          <Route path="/sign/:draftId" element={<Sign session={session} />} />
          <Route path="/sell" element={<Seller session={session} />} />
          <Route path="/board" element={<Board />} />
          <Route path="/console" element={<Console session={session} />} />
          <Route path="/ledger" element={<Ledger />} />
          <Route path="/play" element={<Play session={session} />} />
          <Route path="*" element={<Home session={session} />} />
        </Routes>
      </Suspense>
      {toast && <div className={`toast ${toast.bad ? 'bad' : ''}`} role="status">{toast.text}</div>}
    </>
  )
}
