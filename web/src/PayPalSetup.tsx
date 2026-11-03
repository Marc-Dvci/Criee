import { createElement, useEffect, useRef, useState } from 'react'
import type { Session } from './App'
import { api } from './api'
import { Spinner } from './components'
import { toast } from './hooks'

type SaveSession = { start: (options: { presentationMode: string }, token: Promise<{ vaultSetupToken: string }>) => Promise<void> }
type SDK = {
  createInstance: (options: { clientToken: string; components: string[]; pageType: string }) => Promise<{
    findEligibleMethods: (options: { currencyCode: string; paymentFlow: string }) => Promise<{ isEligible: (method: string) => boolean }>
    createPayPalSavePaymentSession: (callbacks: {
      onApprove: (data: { vaultSetupToken: string }) => Promise<void>; onCancel: () => void; onError: () => void
    }) => SaveSession
  }>
}
let sdkPromise: Promise<SDK> | null = null
function loadSDK(): Promise<SDK> {
  if (sdkPromise) return sdkPromise
  sdkPromise = new Promise<SDK>((resolve, reject) => {
    const script = document.createElement('script')
    const timer = window.setTimeout(() => fail(), 12000)
    const fail = () => {
      window.clearTimeout(timer); script.remove(); sdkPromise = null
      reject(new Error('PayPal’s button could not load. You can open PayPal directly below.'))
    }
    script.src = 'https://www.sandbox.paypal.com/web-sdk/v6/core'
    script.async = true
    script.onload = () => {
      window.clearTimeout(timer)
      const sdk = (window as Window & { paypal?: SDK }).paypal
      if (sdk) resolve(sdk)
      else fail()
    }
    script.onerror = fail
    document.head.appendChild(script)
  })
  return sdkPromise
}

export default function PayPalSetup({ session }: { session: Session }) {
  const [busy, setBusy] = useState('')
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  const button = useRef<HTMLElement>(null)
  const save = useRef<SaveSession | null>(null)
  const latest = useRef(session)
  latest.current = session
  const sandbox = session.status?.paypal === 'sandbox'
  useEffect(() => {
    if (!sandbox || !session.account) return
    let active = true
    setReady(false); setError('')
    const init = async () => {
      try {
        const [sdk, auth] = await Promise.all([loadSDK(), api.get<{ client_token: string; currency: string }>('/api/paypal/client-token')])
        if (!active) return
        const instance = await sdk.createInstance({ clientToken: auth.client_token, components: ['paypal-payments'], pageType: 'checkout' })
        const methods = await instance.findEligibleMethods({ currencyCode: auth.currency, paymentFlow: 'VAULT_WITHOUT_PAYMENT' })
        if (!methods.isEligible('paypal')) throw new Error('PayPal’s button is unavailable for this session. Open PayPal directly below.')
        const payment = instance.createPayPalSavePaymentSession({
          onApprove: async ({ vaultSetupToken }) => {
            try {
              await api.post('/api/vault/complete', { setup_token: vaultSetupToken })
              await latest.current.reload()
              if (active) toast('PayPal approved and saved. No purchase was made.')
            } catch (e: unknown) { if (active) setError((e as Error).message) }
            finally { if (active) setBusy('') }
          },
          onCancel: () => { if (active) { setBusy(''); toast('PayPal approval cancelled. Your current wallet is still available.') } },
          onError: () => { if (active) { setBusy(''); setError('PayPal approval did not finish. Try again or open PayPal directly.') } },
        })
        if (active) { save.current = payment; setReady(true) }
      } catch (e: unknown) { if (active) setError((e as Error).message) }
    }
    void init()
    return () => { active = false; save.current = null }
  }, [sandbox, session.account?.id])
  useEffect(() => {
    const element = button.current
    if (!ready || !element) return
    const click = () => {
      if (busy || !save.current) return
      setBusy('paypal'); setError('')
      // Start synchronously in the click handler to retain browser popup permission.
      const token = api.post<{ setup_token: string }>('/api/vault/start').then((r) => ({ vaultSetupToken: r.setup_token }))
      void save.current.start({ presentationMode: 'auto' }, token).catch(() => {
        setBusy(''); setError('PayPal could not open. Use the direct approval link below.')
      })
    }
    element.addEventListener('click', click)
    return () => element.removeEventListener('click', click)
  }, [ready, busy])
  const direct = async () => {
    setBusy('direct'); setError('')
    try {
      const r = await api.post<{ approve_url: string }>('/api/vault/start')
      if (!r.approve_url) throw new Error('PayPal did not return an approval link.')
      window.location.assign(r.approve_url)
    } catch (e: unknown) { setError((e as Error).message); setBusy('') }
  }
  const demo = async () => {
    setBusy('demo')
    try { await api.post('/api/vault/demo'); await session.reload(); toast('Demo wallet saved') }
    catch (e: unknown) { setError((e as Error).message) }
    finally { setBusy('') }
  }
  return (
    <div className="stack">
      {session.account?.vaulted && <div className="row"><span className="dot" /><b>{session.account.vault_label ?? 'PayPal saved'}</b></div>}
      <p className="small muted" style={{ margin: 0 }}>Save PayPal once. You approve on PayPal’s own screen; no purchase is made here. Later, Criée holds only the amount of a match inside your signed limits.</p>
      {sandbox && <div className="paypal-save" aria-busy={!!busy}>
        {ready ? createElement('paypal-button', { ref: button, 'aria-label': 'Save PayPal for future matches', disabled: busy ? true : undefined })
          : !error && <span className="small muted"><Spinner /> Loading PayPal’s save button…</span>}
      </div>}
      {error && <div className="notice warn small" role="status">{error}</div>}
      <div className="row wrap">
        <button className="btn small" onClick={direct} disabled={!!busy || !sandbox}>{busy === 'direct' && <Spinner />} Open PayPal approval (sandbox)</button>
        {!session.account?.vaulted && <button className="btn small" onClick={demo} disabled={!!busy}>Use the demo wallet</button>}
      </div>
      <span className="small faint">{sandbox ? 'PayPal JavaScript SDK v6 · Save PayPal · Vault v3. A sandbox personal account is needed for approval; the shared demo wallet needs no login.' : 'Offline PayPal double. Hosted PayPal approval becomes available when sandbox credentials are configured.'}</span>
    </div>
  )
}
