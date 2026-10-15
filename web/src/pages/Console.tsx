import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { AllCommunityModule, ModuleRegistry, colorSchemeDark, themeQuartz, type ColDef, type ICellRendererParams } from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import { AgCharts } from 'ag-charts-react'
import { AllCommunityModule as ChartsCommunity, ModuleRegistry as ChartsRegistry, type AgCartesianChartOptions } from 'ag-charts-community'
import type { Session } from '../App'
import { CATEGORY, MODE, api, eur, hhmm, type Lot, type Match } from '../api'
import { StatusChip } from '../components'
import { toast, useLive } from '../hooks'

ModuleRegistry.registerModules([AllCommunityModule])
ChartsRegistry.registerModules([ChartsCommunity])

const theme = themeQuartz.withPart(colorSchemeDark).withParams({
  backgroundColor: '#161d24', foregroundColor: '#edeae3', headerBackgroundColor: '#1e2730', borderColor: '#2b3640',
  accentColor: '#ffc23d', fontFamily: 'IBM Plex Sans, system-ui, sans-serif', fontSize: 13.5, rowHeight: 40,
  headerFontWeight: 600, oddRowBackgroundColor: '#18202a', chromeBackgroundColor: '#1e2730', wrapperBorderRadius: 12,
})

type Payout = { payout_id: string; seller_id: string; match_ids: string[]; gross: number; fee: number; debit: number; net: number; batch_id: string | null; status: string; at: string | null }
type Dispute = { dispute_id: string; match_id: string | null; reason: string; status: string; at: string; evidence_sent: boolean; outcome: string | null; cents: number }
type Book = { category: string; limit: number; units: number; distance_band: string; conditions: string[]; modes: string[]; per_week: number; simulated: boolean }
type ConsoleData = { sales: Match[]; payouts: Payout[]; disputes: Dispute[]; orderbook: Book[]; lots: Lot[]; fee_bps: number }

const money = (p: { value: number }) => eur(p.value)
const statusCell = (p: ICellRendererParams) => <StatusChip status={p.value} />

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card flat stack tight" style={{ padding: 14 }}>
      <span className="upper muted">{label}</span>
      <span className="mono" style={{ fontSize: 24, fontWeight: 600 }}>{value}</span>
      {hint && <span className="tiny faint">{hint}</span>}
    </div>
  )
}

function DemandChart({ book, category }: { book: Book[]; category: string }) {
  const rows = useMemo(() => {
    const limits = book.filter((b) => b.category === category).map((b) => [b.limit, b.units] as const).sort((a, b) => b[0] - a[0])
    let acc = 0
    const out: { price: number; units: number }[] = []
    for (const [p, u] of limits) { acc += u; out.push({ price: p / 100, units: acc }) }
    return out
  }, [book, category])
  const options: AgCartesianChartOptions = {
    data: rows,
    background: { fill: '#161d24' },
    theme: { baseTheme: 'ag-default-dark', palette: { fills: ['#ffc23d'], strokes: ['#ffc23d'] } },
    series: [{ type: 'line', xKey: 'units', yKey: 'price', yName: 'Price', interpolation: { type: 'step', position: 'end' },
               marker: { size: 5 }, tooltip: { renderer: ({ datum }: any) => ({ content: `${datum.units} units wanted at €${datum.price.toFixed(2)} or more` }) } }],
    axes: {
      x: { type: 'number', position: 'bottom', title: { text: 'Units wanted at that price or more' } },
      y: { type: 'number', position: 'left', title: { text: 'Price (€)' }, label: { formatter: ({ value }: any) => `€${Number(value).toFixed(2)}` } },
    },
  } as unknown as AgCartesianChartOptions
  if (rows.length === 0) return <div className="empty">No standing wish for {CATEGORY[category]} covers your shop.</div>
  return <div style={{ height: 320 }}><AgCharts options={options} /></div>
}

export default function Console({ session }: { session: Session }) {
  const [tab, setTab] = useState<'sales' | 'payouts' | 'disputes' | 'book' | 'lots'>('sales')
  const [q, setQ] = useState('')
  const [cat, setCat] = useState('bread')
  const isSeller = !!session.account?.roles.includes('seller') && !!session.account?.shop
  const { data, reload } = useLive<ConsoleData>(isSeller ? '/api/console' : null)

  const refund = async (m: Match) => {
    const left = m.total - m.paypal.refunded
    const v = window.prompt(`Refund how much for ${m.units} × ${m.name}? (max €${(left / 100).toFixed(2)})`, (left / 100).toFixed(2))
    if (!v) return
    try { await api.post('/api/refunds', { match_id: m.id, cents: Math.round(parseFloat(v.replace(',', '.')) * 100), reason: 'Refund from console' }); toast('Refunded through PayPal'); reload() } catch (e: any) { toast(e.message, true) }
  }
  const sendEvidence = async (d: Dispute) => {
    try { const r: any = await api.post(`/api/disputes/${d.dispute_id}/evidence`); r.ok ? toast('Evidence pack sent to PayPal') : toast(`PayPal: ${r.issue}`, true); reload() } catch (e: any) { toast(e.message, true) }
  }

  const salesCols = useMemo<ColDef<Match>[]>(() => [
    { field: 'at', headerName: 'Time', valueFormatter: (p) => hhmm(p.value), width: 90, sort: 'desc' },
    { field: 'name', headerName: 'Item', flex: 1.2, minWidth: 150 },
    { field: 'units', width: 80 },
    { field: 'unit_cents', headerName: 'Unit price', valueFormatter: money, width: 110 },
    { field: 'total', valueFormatter: money, width: 100 },
    { field: 'rule', headerName: 'Cleared by', valueFormatter: (p) => MODE[p.value]?.en ?? p.value, width: 140 },
    { field: 'status', cellRenderer: statusCell, width: 120 },
    { field: 'paypal.authorization_id', headerName: 'PayPal authorization', width: 190, cellClass: 'mono' },
    { field: 'paypal.capture_id', headerName: 'PayPal capture', width: 180, cellClass: 'mono' },
    { field: 'paypal.refunded', headerName: 'Refunded', valueFormatter: money, width: 105 },
    { field: 'simulated', headerName: 'Buyer', valueFormatter: (p) => (p.value ? 'simulated' : 'person'), width: 110 },
    { headerName: '', width: 190, sortable: false, filter: false, cellRenderer: (p: ICellRendererParams<Match>) => (
      <div className="row" style={{ height: '100%' }}>
        {p.data?.status === 'captured' && p.data.total > p.data.paypal.refunded && <button className="btn small" onClick={() => refund(p.data!)}>Refund</button>}
        <a className="btn small ghost" href={`/api/evidence/${p.data?.id}.pdf`} target="_blank" rel="noreferrer">Evidence</a>
      </div>) },
  ], [])

  const payoutCols = useMemo<ColDef<Payout>[]>(() => [
    { field: 'at', headerName: 'Sent', valueFormatter: (p) => hhmm(p.value), width: 90, sort: 'desc' },
    { field: 'match_ids', headerName: 'Sales', valueFormatter: (p) => String(p.value.length), width: 90 },
    { field: 'gross', valueFormatter: money, width: 110 },
    { field: 'fee', headerName: 'Criée fee', valueFormatter: money, width: 110 },
    { field: 'debit', headerName: 'Refund netting', valueFormatter: money, width: 140 },
    { field: 'net', headerName: 'Paid to you', valueFormatter: money, width: 120 },
    { field: 'status', width: 120 },
    { field: 'batch_id', headerName: 'PayPal payout batch', flex: 1, cellClass: 'mono' },
  ], [])

  const disputeCols = useMemo<ColDef<Dispute>[]>(() => [
    { field: 'at', headerName: 'Opened', valueFormatter: (p) => hhmm(p.value), width: 100 },
    { field: 'dispute_id', headerName: 'PayPal dispute', width: 200, cellClass: 'mono' },
    { field: 'reason', flex: 1 },
    { field: 'cents', headerName: 'Amount', valueFormatter: money, width: 100 },
    { field: 'status', width: 170 },
    { field: 'outcome', width: 170 },
    { headerName: '', width: 230, cellRenderer: (p: ICellRendererParams<Dispute>) => (
      <div className="row" style={{ height: '100%' }}>
        {!p.data?.evidence_sent && p.data?.match_id && <button className="btn small signal" onClick={() => sendEvidence(p.data!)}>Send evidence pack</button>}
        {p.data?.match_id && <a className="btn small ghost" href={`/api/evidence/${p.data.match_id}.pdf?dispute=${p.data.dispute_id}`} target="_blank" rel="noreferrer">PDF</a>}
      </div>) },
  ], [])

  const bookCols = useMemo<ColDef<Book>[]>(() => [
    { field: 'category', valueFormatter: (p) => CATEGORY[p.value], width: 140 },
    { field: 'limit', headerName: 'Limit', valueFormatter: money, width: 100, sort: 'desc' },
    { field: 'units', width: 80 },
    { field: 'distance_band', headerName: 'Distance', width: 110 },
    { field: 'conditions', valueFormatter: (p) => p.value.join(', ') || '–', flex: 1 },
    { field: 'modes', valueFormatter: (p) => p.value.map((m: string) => MODE[m].en).join(', '), flex: 1 },
    { field: 'per_week', headerName: 'Per week', width: 100 },
    { field: 'simulated', valueFormatter: (p) => (p.value ? 'simulated' : 'person'), width: 110 },
  ], [])

  const lotCols = useMemo<ColDef<Lot>[]>(() => [
    { field: 'name', flex: 1 },
    { field: 'mode', valueFormatter: (p) => MODE[p.value].en, width: 140 },
    { field: 'price', headerName: 'Price now', valueFormatter: money, width: 110 },
    { field: 'floor', valueFormatter: money, width: 100 },
    { field: 'sold', width: 80 },
    { field: 'remaining', headerName: 'Left', width: 80 },
    { field: 'waiting', headerName: 'Wishes waiting', width: 130 },
    { field: 'status', cellRenderer: statusCell, width: 130 },
    { field: 'closes_at', headerName: 'Closes', valueFormatter: (p) => hhmm(p.value), width: 100 },
  ], [])

  if (!isSeller) {
    return (
      <main className="page narrow stack loose">
        <h1>Seller console</h1>
        <p className="lede">Sales, PayPal holds and captures, refunds, payouts, disputes and the anonymised order book around your shop.</p>
        <Link to="/sell" className="btn primary" style={{ alignSelf: 'flex-start' }}>Set up a shop first</Link>
      </main>
    )
  }
  const s = data?.sales ?? []
  const captured = s.filter((m) => m.status === 'captured').reduce((a, m) => a + m.total, 0)
  const held = s.filter((m) => m.status === 'held')
  const net = (data?.payouts ?? []).reduce((a, p) => a + p.net, 0)
  const refunded = s.reduce((a, m) => a + m.paypal.refunded, 0)
  const tabs: [typeof tab, string, number | undefined][] = [
    ['sales', 'Sales', s.length], ['payouts', 'Payouts', data?.payouts.length], ['disputes', 'Disputes', data?.disputes.length],
    ['book', 'Order book', data?.orderbook.length], ['lots', 'Lots', data?.lots.length],
  ]
  return (
    <main className="page wide">
      <div className="page-head">
        <div><h1>{session.account!.shop!.name} · console</h1><p className="lede">Every sale with its PayPal authorization and capture, every payout and dispute, and the demand around the shop.</p></div>
        <input className="input" style={{ maxWidth: 280 }} placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the table" />
      </div>
      <div className="grid-3" style={{ gridTemplateColumns: 'repeat(5, minmax(0,1fr))', marginBottom: 18 }}>
        <Tile label="Captured" value={eur(captured)} hint={`${s.filter((m) => m.status === 'captured').length} sales`} />
        <Tile label="Holds open" value={eur(held.reduce((a, m) => a + m.total, 0))} hint={`${held.length} awaiting pickup`} />
        <Tile label="Paid out" value={eur(net)} hint={`after the ${((data?.fee_bps ?? 400) / 100).toFixed(0)}% fee`} />
        <Tile label="Refunded" value={eur(refunded)} />
        <Tile label="Disputes" value={String(data?.disputes.length ?? 0)} hint={`${(data?.disputes ?? []).filter((d) => d.status === 'RESOLVED').length} resolved`} />
      </div>
      <div className="tabs" role="tablist">
        {tabs.map(([k, label, n]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label}{n !== undefined ? ` · ${n}` : ''}</button>)}
      </div>
      {tab === 'sales' && <div className="ag-host"><AgGridReact theme={theme} rowData={s} columnDefs={salesCols} quickFilterText={q} getRowId={(p) => p.data.id} animateRows /></div>}
      {tab === 'payouts' && <div className="ag-host"><AgGridReact theme={theme} rowData={data?.payouts ?? []} columnDefs={payoutCols} quickFilterText={q} getRowId={(p) => p.data.payout_id} /></div>}
      {tab === 'disputes' && (
        <div className="stack">
          <div className="ag-host short"><AgGridReact theme={theme} rowData={data?.disputes ?? []} columnDefs={disputeCols} quickFilterText={q} getRowId={(p) => p.data.dispute_id} /></div>
          <p className="small muted">A dispute arrives by PayPal webhook. The evidence pack is built only from the signed ledger: the buyer's signed mandate, the clearing round, the pickup scan and the PayPal ids, with a verifiable hash chain.</p>
        </div>
      )}
      {tab === 'book' && (
        <div className="grid-2" style={{ alignItems: 'start' }}>
          <div className="ag-host"><AgGridReact theme={theme} rowData={data?.orderbook ?? []} columnDefs={bookCols} quickFilterText={q} /></div>
          <div className="card stack">
            <div className="row between"><h2>Demand curve</h2>
              <select className="input" style={{ width: 'auto' }} value={cat} onChange={(e) => setCat(e.target.value)}>{Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
            <DemandChart book={data?.orderbook ?? []} category={cat} />
            <p className="small muted" style={{ margin: 0 }}>Standing wishes whose pickup area covers the shop. Exact locations and identities never leave the server.</p>
          </div>
        </div>
      )}
      {tab === 'lots' && <div className="ag-host"><AgGridReact theme={theme} rowData={data?.lots ?? []} columnDefs={lotCols} quickFilterText={q} getRowId={(p) => p.data.id} /></div>}
    </main>
  )
}
