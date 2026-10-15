export type Shop = { name: string; place?: string | null; lat?: number; lon?: number }

export type Fact = { attr: string; value: boolean | number | null; evidence: string | null }
export type Crop = { id: string; image_id: string; box: number[]; label: string }

export type Lot = {
  id: string; name: string; name_fr?: string | null; unit: string; category: string; mode: 'call' | 'clock' | 'threshold'
  shop: Shop; seller_id: string; quantity: number; remaining: number; sold: number; price: number; start: number; floor: number
  opens_at: string; closes_at: string; pickup_from: string; pickup_until: string; status: string; waiting: number
  simulated: boolean; facts: Fact[]; image_id?: string | null; crops: Crop[]
  clock?: { round: number; rounds: number; step_s: number; step_cents: number; next_at: string | null; next_price: number | null }
  threshold?: { target: number; committed: number; reached: boolean; burst: Burst | null }
  matches?: Match[]
  seed?: string | null
}

export type Burst = { lot_id: string; holds: number; authorized: number; wall_ms: number; p50_ms: number | null; max_ms: number | null }

export type Match = {
  id: string; lot_id: string; wish_id: string; name: string; unit: string; shop: Shop; units: number; unit_cents: number
  total: number; rule: string; round: number; at: string; status: string; pickup_by: string | null; pickup_from: string
  pickup_until: string; scanned: boolean; simulated: boolean; pickup_code?: string
  paypal: { order_id: string | null; authorization_id: string | null; capture_id: string | null; refunded: number; payout_id: string | null; auth_ms: number | null }
}

export type Wish = {
  id: string; label: string; readback: string; status: string; terms: any; terms_hash: string
  signature: { kind: string; credential_id: string }; signed_at: string; matches: Match[]
  week: { matches: number; spent: number; max_matches: number; budget: number }
  watching: { open_lots: number; reasons: { code: string; text: string; lots: number }[] }
  commitments: { lot_id: string; name: string; shop: string; price: number; committed: number; target: number; closes_at: string; status: string }[]
}

export type Account = {
  id: string; handle: string; roles: string[]; vaulted: boolean; shop: Shop | null
  credentials: { id: string; kind: string }[]; vault_label: string | null
}

export type Draft = {
  id: string; terms: any; label: string; readback: string; place: string; source: string; via: string; digest: string
  passkey: any
}

export type Status = {
  paypal: string; currency: string; fee_bps: number
  ai: { provider: string; model: string; live_until: string | null; calls_today: number; daily_cap: number }
  ledger: { events: number; head: string }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

async function call<T>(method: string, path: string, body?: unknown, form?: FormData): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: form ? undefined : body !== undefined ? { 'content-type': 'application/json' } : undefined,
    body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
  })
  const text = await res.text()
  const data = text ? JSON.parse(text) : {}
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((d: any) => d.msg).join('; ') : res.statusText
    throw new ApiError(res.status, detail)
  }
  return data as T
}

export const api = {
  get: <T>(p: string) => call<T>('GET', p),
  post: <T>(p: string, b?: unknown) => call<T>('POST', p, b ?? {}),
  form: <T>(p: string, f: FormData) => call<T>('POST', p, undefined, f),
}

export const eur = (cents: number | null | undefined) =>
  cents == null ? '–' : `€${(cents / 100).toFixed(2)}`

export const hhmm = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Paris' }) : '–'

export const ago = (iso: string) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.round(s)} s ago`
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  return `${Math.round(s / 3600)} h ago`
}

export const NEIGHBOURHOODS: [string, number, number][] = [
  ['Centre', 48.5834, 7.7458], ['Krutenau', 48.5795, 7.7555], ['Petite France', 48.581, 7.74], ['Neudorf', 48.57, 7.76],
  ['Esplanade', 48.579, 7.768], ['Gare', 48.585, 7.735], ['Contades', 48.59, 7.755], ['Bourse', 48.576, 7.747],
  ['Orangerie', 48.59, 7.77], ['Cronenbourg', 48.595, 7.72], ['Koenigshoffen', 48.58, 7.715],
]

export const CATEGORY: Record<string, string> = {
  bread: 'Bread', pastry: 'Pastry', produce: 'Fruit & veg', dairy: 'Dairy', prepared_meal: 'Prepared meal',
  flowers: 'Flowers', grocery_box: 'Grocery box', service_slot: 'Appointment',
}

export const MODE: Record<string, { en: string; fr: string; hint: string }> = {
  clock: { en: 'Falling clock', fr: 'Criée descendante', hint: 'The price steps down until every unit is sold.' },
  call: { en: 'Closing auction', fr: 'Vente à la clôture', hint: 'One price for everyone at the deadline: the highest losing bid.' },
  threshold: { en: 'Group order', fr: 'Commande groupée', hint: 'Happens only if enough people commit by the deadline.' },
}

export const ATTR: Record<string, string> = {
  organic: 'Organic', vegetarian: 'Vegetarian', vegan: 'Vegan', gluten_free: 'Gluten-free', contains_nuts: 'Contains nuts',
  contains_dairy: 'Contains dairy', baked_today: 'Baked today', local: 'Local', weight_g: 'Weight (g)',
  best_before_hours: 'Good for (h)', servings: 'Servings', duration_min: 'Duration (min)',
}

export function factText(f: Fact): string {
  const name = ATTR[f.attr] ?? f.attr
  if (f.value === null) return `${name}: unknown`
  if (typeof f.value === 'boolean') return f.value ? name : `Not ${name.toLowerCase()}`
  return `${name}: ${f.value}`
}
