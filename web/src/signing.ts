import { api, type Account } from './api'

const b64uToBuf = (s: string) => {
  const pad = s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)
  return Uint8Array.from(atob(pad), (c) => c.charCodeAt(0)).buffer
}
const bufToB64u = (b: ArrayBuffer) =>
  btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const hexToBuf = (h: string) => new Uint8Array(h.match(/.{2}/g)!.map((x) => parseInt(x, 16))).buffer

export const passkeysAvailable = () => typeof window.PublicKeyCredential !== 'undefined'

export async function registerPasskey(): Promise<string> {
  const o: any = await api.post('/api/passkey/register/options')
  const cred = (await navigator.credentials.create({
    publicKey: {
      ...o,
      challenge: b64uToBuf(o.challenge),
      user: { ...o.user, id: b64uToBuf(o.user.id) },
      excludeCredentials: (o.excludeCredentials ?? []).map((c: any) => ({ ...c, id: b64uToBuf(c.id) })),
    },
  })) as PublicKeyCredential
  const r = cred.response as AuthenticatorAttestationResponse
  const out: any = await api.post('/api/passkey/register/verify', {
    id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
    response: { clientDataJSON: bufToB64u(r.clientDataJSON), attestationObject: bufToB64u(r.attestationObject),
                transports: r.getTransports?.() ?? [] },
    clientExtensionResults: cred.getClientExtensionResults(),
  })
  return out.credential_id
}

export async function passkeySign(options: any): Promise<any> {
  const cred = (await navigator.credentials.get({
    publicKey: {
      ...options,
      challenge: b64uToBuf(options.challenge),
      allowCredentials: (options.allowCredentials ?? []).map((c: any) => ({ ...c, id: b64uToBuf(c.id) })),
    },
  })) as PublicKeyCredential
  const r = cred.response as AuthenticatorAssertionResponse
  return {
    id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: bufToB64u(r.clientDataJSON), authenticatorData: bufToB64u(r.authenticatorData),
      signature: bufToB64u(r.signature), userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  }
}

// ------------------------------------------------------------------ device key (IndexedDB, non-extractable)

function idb(): Promise<IDBDatabase> {
  return new Promise((res, rej) => {
    const req = indexedDB.open('criee', 1)
    req.onupgradeneeded = () => req.result.createObjectStore('keys')
    req.onsuccess = () => res(req.result)
    req.onerror = () => rej(req.error)
  })
}
async function idbGet<T>(k: string): Promise<T | undefined> {
  const db = await idb()
  return new Promise((res) => {
    const r = db.transaction('keys').objectStore('keys').get(k)
    r.onsuccess = () => res(r.result as T)
    r.onerror = () => res(undefined)
  })
}
async function idbPut(k: string, v: unknown) {
  const db = await idb()
  return new Promise<void>((res, rej) => {
    const tx = db.transaction('keys', 'readwrite')
    tx.objectStore('keys').put(v, k)
    tx.oncomplete = () => res()
    tx.onerror = () => rej(tx.error)
  })
}

export async function deviceKey(account: Account): Promise<{ id: string; key: CryptoKey }> {
  const stored = await idbGet<{ id: string; key: CryptoKey }>(`device:${account.id}`)
  if (stored && account.credentials.some((c) => c.id === stored.id)) return stored
  const pair = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify'])
  const jwk = await crypto.subtle.exportKey('jwk', pair.publicKey)
  const out: any = await api.post('/api/devicekey/register', { jwk: { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y } })
  const rec = { id: out.credential_id, key: pair.privateKey }
  await idbPut(`device:${account.id}`, rec)
  return rec
}

export async function deviceSign(account: Account, digestHex: string) {
  const { id, key } = await deviceKey(account)
  const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key, hexToBuf(digestHex))
  return { kind: 'device', credential_id: id, signature: bufToB64u(sig) }
}
