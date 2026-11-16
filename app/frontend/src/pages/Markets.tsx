// Market / distributor 360 (list panel + detail panel, FE BAR CustomerPage pattern). Everything loads from the data.
import { useEffect, useMemo, useState } from 'react'
import { Distributor, fmt, useFetch, useReference } from '../api'
import { Button, EmptyState, ErrorState, Icon, Pill, Section, Select, Skeleton } from '../components/ui'
import { CoverChip, SellInOutChart, TrendRow } from '../components/viz'
import { ask, useDemo } from '../demo_config'

type Cover = { sku: string; product_name: string; brand: string; on_hand_cases: number; in_transit_cases: number; avg_weekly_depletion_8w: number; cover_days: number; target_cover_days: number; weekly_revenue_at_risk_usd: number; status: string }
type PromoRow = { promo_id: string; sku: string; mechanic: string; discount_pct: number; uplift_forecast_pct: number; uplift_actual_pct: number; gap_vs_forecast_pct: number }
type Summary = { distributor: Distributor & { market_name: string; region: string }; cover: Cover[]; promos: PromoRow[]; counts: { at_risk: number; below_target: number; skus: number } }
type Activity = {
  requests: { request_id: string; created_at: string; sku: string; qty_cases: number; status: string; reason: string; decided_by: string | null; decision_reason: string | null }[]
  decisions: { decision_id: string; created_at: string; action: string; sku: string; summary: string }[]
}

const readSel = () => new URLSearchParams(window.location.hash.split('?')[1] || '').get('d') || ''

export default function Markets({ onAsk }: { onAsk: (p: string) => void }) {
  const ref = useReference()
  const [market, setMarket] = useState('')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState(readSel)
  const select = (id: string) => { setSelected(id); window.history.replaceState(null, '', `#/markets?d=${encodeURIComponent(id)}`) }

  // default market = the selected distributor's, else the first market
  useEffect(() => {
    if (!ref || market) return
    const d = ref.distributors.find((x) => x.distributor_id === selected)
    setMarket(d?.market_id || ref.markets[0]?.market_id || '')
  }, [ref, market, selected])

  const list = useMemo(() => (ref?.distributors || []).filter((d) =>
    (!market || d.market_id === market) && (!search || `${d.distributor_id} ${d.distributor_name}`.toLowerCase().includes(search.toLowerCase()))), [ref, market, search])

  return (
    <div className="px-6 py-8 max-w-7xl">
      <div className="mb-6">
        <h1 className="text-2xl font-medium">Markets and distributors</h1>
        <p className="text-ink-700 mt-1">Stock cover, sell-in vs sell-out, active promotions and the decisions taken, per distributor.</p>
      </div>
      <div className="grid gap-6 lg:grid-cols-[300px_minmax(0,1fr)]">
        <aside className="bg-white border border-line/70 rounded-card p-4 h-fit lg:sticky lg:top-20 space-y-3">
          {!ref ? <Skeleton rows={6} /> : (
            <>
              <Select label="Market" value={market} onChange={(v) => { setMarket(v) }}
                options={ref.markets.map((m) => ({ value: m.market_id, label: m.market_name }))} />
              <label className="block">
                <span className="sr-only">Search distributors</span>
                <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search distributors"
                  className="w-full border border-line rounded-md px-3 min-h-[40px] bg-oat-light focus:bg-white" />
              </label>
              <ul className="space-y-1" aria-label="Distributors">
                {list.map((d) => (
                  <li key={d.distributor_id}>
                    <button onClick={() => select(d.distributor_id)} aria-current={selected === d.distributor_id}
                      className={`w-full text-left rounded-md px-3 py-2.5 min-h-[44px] transition-colors duration-150 ease-quart ${selected === d.distributor_id ? 'bg-navy-900 text-white' : 'hover:bg-oat-medium'}`}>
                      <div className="text-sm font-medium">{d.distributor_id} · {d.distributor_name}</div>
                      <div className={`text-xs ${selected === d.distributor_id ? 'text-white/70' : 'text-ink-700'}`}>{d.channel}</div>
                    </button>
                  </li>
                ))}
                {!list.length && <li className="text-sm text-ink-700 px-1">No distributors match.</li>}
              </ul>
            </>
          )}
        </aside>
        <div className="min-w-0">
          {selected ? <Detail key={selected} id={selected} onAsk={onAsk} />
            : <EmptyState title="Pick a distributor">Choose a market and a distributor on the left to see its stock cover, trend and decisions.</EmptyState>}
        </div>
      </div>
    </div>
  )
}

function Detail({ id, onAsk }: { id: string; onAsk: (p: string) => void }) {
  const demo = useDemo()
  const s = useFetch<Summary>(`/distributors/${encodeURIComponent(id)}/summary`, {})
  const [sku, setSku] = useState('')
  const t = useFetch<{ rows: TrendRow[] }>(`/distributors/${encodeURIComponent(id)}/trend`, { sku: sku || undefined })
  const a = useFetch<Activity>(`/distributors/${encodeURIComponent(id)}/activity`, {})
  if (s.loading) return <div className="space-y-6"><Section title="Loading distributor…"><Skeleton rows={6} /></Section></div>
  if (s.error) return <ErrorState message={s.error} onRetry={s.reload} />
  const { distributor: d, cover, promos, counts } = s.data!
  const ok = counts.skus - counts.at_risk - counts.below_target
  return (
    <div className="space-y-6">
      <section className="bg-white border border-line/70 rounded-card p-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <div className="text-sm text-ink-700">{d.market_name} · {d.region}</div>
            <h2 className="text-2xl font-medium mt-1">{d.distributor_name}</h2>
            <div className="text-sm text-ink-700 mt-1"><span className="font-mono">{d.distributor_id}</span> · {d.channel}</div>
          </div>
          <Button variant="primary" onClick={() => onAsk(ask(demo, 'distributor', { distributor_id: d.distributor_id, distributor_name: d.distributor_name }))}><Icon name="assistant" />Ask the agent</Button>
        </div>
        <div className="flex flex-wrap gap-2 mt-5">
          <Pill tone={counts.at_risk ? 'critical' : 'ok'}>{counts.at_risk} SKU{counts.at_risk === 1 ? '' : 's'} at risk</Pill>
          <Pill tone={counts.below_target ? 'warn' : 'ok'}>{counts.below_target} below target</Pill>
          <Pill tone="ok">{ok} on target</Pill>
          <Pill tone={promos.length ? 'info' : 'neutral'}>{promos.length} active promotion{promos.length === 1 ? '' : 's'}</Pill>
        </div>
      </section>

      <Section title="Stock cover by SKU" subtitle="Days of cover = (on hand + in transit) / average daily depletions of the last 8 weeks, against the channel target.">
        {!cover.length ? <EmptyState title="No stock positions" /> : (
          <div className="overflow-x-auto -mx-2">
            <table className="w-full text-sm">
              <thead><tr className="text-left text-xs text-ink-700">
                <th className="font-medium px-2 py-2">Product</th><th className="font-medium px-2 py-2">Cover vs target</th>
                <th className="font-medium px-2 py-2 text-right">On hand + in transit</th><th className="font-medium px-2 py-2 text-right">Avg weekly depletion</th>
                <th className="font-medium px-2 py-2 text-right">Weekly revenue</th><th className="px-2" />
              </tr></thead>
              <tbody>
                {cover.map((c) => (
                  <tr key={c.sku} className="border-t border-line/60">
                    <td className="px-2 py-2.5">{c.product_name}<span className="block text-xs text-ink-700 font-mono">{c.sku}</span></td>
                    <td className="px-2 py-2.5 min-w-[220px]">
                      <div className="flex items-center gap-3">
                        <CoverChip status={c.status} days={c.cover_days} />
                        <CoverBar days={c.cover_days} target={c.target_cover_days} status={c.status} />
                      </div>
                    </td>
                    <td className="px-2 py-2.5 text-right tabular-nums">{fmt.num(c.on_hand_cases + c.in_transit_cases)}</td>
                    <td className="px-2 py-2.5 text-right tabular-nums">{fmt.num(c.avg_weekly_depletion_8w)}</td>
                    <td className="px-2 py-2.5 text-right tabular-nums">{fmt.usd(c.weekly_revenue_at_risk_usd)}</td>
                    <td className="px-2 py-2.5 text-right whitespace-nowrap">
                      <Button variant="ghost" className="!min-h-[36px] !px-2" onClick={() => setSku(c.sku)} aria-label={`Show trend for ${c.sku}`}>Trend</Button>
                      {c.status !== 'OK' && <Button variant="ghost" className="!min-h-[36px] !px-2" onClick={() => onAsk(ask(demo, 'replenish', { distributor_id: d.distributor_id, product_name: c.product_name, sku: c.sku }))}>Ask</Button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <div className="grid gap-6 xl:grid-cols-2">
        <Section title="Sell-in vs sell-out" subtitle={sku ? `${cover.find((c) => c.sku === sku)?.product_name || sku}, last 26 weeks` : 'All SKUs, last 26 weeks'}
          actions={<Select label="SKU" value={sku} onChange={setSku} options={[{ value: '', label: 'All SKUs' }, ...cover.map((c) => ({ value: c.sku, label: c.product_name }))]} />}>
          {t.loading ? <Skeleton rows={6} /> : t.error ? <ErrorState message={t.error} onRetry={t.reload} /> : <SellInOutChart rows={t.data!.rows} height={240} />}
        </Section>
        <Section title="Active promotions" subtitle="Measured uplift at this distributor vs the promo forecast">
          {!promos.length ? <EmptyState title="No active promotions here" /> : (
            <ul className="divide-y divide-line/60">
              {promos.map((p) => (
                <li key={p.promo_id} className="py-3 flex items-start justify-between gap-4">
                  <div>
                    <div className="font-medium"><span className="font-mono">{p.promo_id}</span> · {cover.find((c) => c.sku === p.sku)?.product_name || p.sku}</div>
                    <div className="text-sm text-ink-700">{p.mechanic}, {Math.round(p.discount_pct * 100)}% off · forecast {fmt.pct(p.uplift_forecast_pct)}</div>
                    <Button variant="ghost" className="!px-2 -ml-2 mt-1" onClick={() => onAsk(ask(demo, 'whyPromo', { promo_id: p.promo_id }))}><Icon name="assistant" />Ask why</Button>
                  </div>
                  <div className="text-right">
                    <div className="text-xl font-medium tabular-nums">{fmt.pct(p.uplift_actual_pct)}</div>
                    <Pill tone={p.gap_vs_forecast_pct < -0.2 ? 'critical' : p.gap_vs_forecast_pct < 0 ? 'warn' : 'ok'}>{fmt.pts(p.gap_vs_forecast_pct)} vs forecast</Pill>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>

      <Section title="Requests and decisions" subtitle="Replenishment requests for this distributor, and the decisions you took (the agent recalls them).">
        {a.loading ? <Skeleton rows={3} /> : a.error ? <ErrorState message={a.error} onRetry={a.reload} />
          : !a.data!.requests.length && !a.data!.decisions.length ? <EmptyState title="No requests or decisions yet" /> : (
            <div className="grid gap-6 md:grid-cols-2">
              <div>
                <h3 className="text-sm font-medium mb-2">Requests</h3>
                <ul className="space-y-2">
                  {a.data!.requests.map((r) => (
                    <li key={r.request_id} className="rounded-md border border-line/70 p-3 text-sm">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">{fmt.cases(r.qty_cases)} · <span className="font-mono">{r.sku}</span></span>
                        <Pill tone={r.status === 'approved' ? 'ok' : r.status === 'proposed' ? 'warn' : 'neutral'}>{r.status}</Pill>
                      </div>
                      <div className="text-ink-700 mt-1">{r.decision_reason || r.reason}</div>
                      <div className="text-xs text-ink-700 mt-1">{fmt.dateTime(r.created_at)}{r.decided_by ? ` · ${r.decided_by}` : ''}</div>
                    </li>
                  ))}
                  {!a.data!.requests.length && <li className="text-sm text-ink-700">None.</li>}
                </ul>
              </div>
              <div>
                <h3 className="text-sm font-medium mb-2">Your decisions</h3>
                <ul className="space-y-2">
                  {a.data!.decisions.map((x) => (
                    <li key={x.decision_id} className="rounded-md bg-oat-light p-3 text-sm">
                      <div className="text-xs text-ink-700 mb-1">{fmt.date(x.created_at)} · {x.action}</div>
                      {x.summary}
                    </li>
                  ))}
                  {!a.data!.decisions.length && <li className="text-sm text-ink-700">None.</li>}
                </ul>
              </div>
            </div>
          )}
      </Section>
    </div>
  )
}

function CoverBar({ days, target, status }: { days: number; target: number; status: string }) {
  const max = Math.max(target * 1.25, days)
  const fill = status === 'AT RISK' ? 'bg-lava-600' : status === 'BELOW TARGET' ? 'bg-warn' : 'bg-navy-700'
  return (
    <div className="relative flex-1 h-2 rounded-full bg-oat-medium min-w-[80px]" title={`${days} of ${target} days`} aria-hidden>
      <div className={`absolute inset-y-0 left-0 rounded-full ${fill}`} style={{ width: `${Math.min(100, (days / max) * 100)}%` }} />
      <div className="absolute -top-1 -bottom-1 w-0.5 bg-navy-900" style={{ left: `${(target / max) * 100}%` }} />
    </div>
  )
}
