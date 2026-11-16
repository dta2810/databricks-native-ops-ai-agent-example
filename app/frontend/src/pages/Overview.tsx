import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { fmt, getJson, useFetch, useReference } from '../api'
import { Button, EmptyState, ErrorState, Icon, Pill, Section, Select, Skeleton, coverTone } from '../components/ui'
import { AiInsights, Alert, Insights, KpiGrid, KpiTileData, OperationalAlerts, SellInOutChart, TrendRow } from '../components/viz'
import { ask, useDemo } from '../demo_config'

// market options from dim_market (no hardcoded codes)
export function useMarketOptions(allLabel = 'All markets') {
  const ref = useReference()
  return [{ value: '', label: allLabel }, ...(ref?.markets || []).map((m) => ({ value: m.market_id, label: m.market_name }))]
}

type OosRow = { market_id: string; distributor_id: string; distributor_name: string; channel: string; sku: string; product_name: string; cover_days: number; avg_weekly_depletion_8w: number; weekly_revenue_at_risk_usd: number }
type Promo = { promo_id: string; market_id: string; sku: string; product_name: string; start_date: string; end_date: string; discount_pct: number; mechanic: string; uplift_forecast_pct: number; uplift_actual_pct: number; gap_vs_forecast_pct: number; status: string }
type PromoDist = { distributor_id: string; distributor_name: string; channel: string; pre_promo_avg_cases: number; in_promo_avg_cases: number; uplift_pct: number }

export default function Overview({ onAsk }: { onAsk: (p: string) => void }) {
  const kpis = useFetch<{ as_of_week: string; tiles: KpiTileData[] }>('/exec/kpis', {})
  const alerts = useFetch<{ alerts: Alert[] }>('/exec/alerts', {})
  return (
    <div className="px-6 py-8 max-w-7xl space-y-6">
      <div>
        <h1 className="text-2xl font-medium">Executive overview</h1>
        <p className="text-ink-700 mt-1">
          {kpis.data ? <>Week of {fmt.week(kpis.data.as_of_week)} · </> : null}Live from Unity Catalog metric views and functions, with your own data permissions.
        </p>
      </div>
      <KpiGrid tiles={kpis.data?.tiles} loading={kpis.loading} error={kpis.error} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        {/* insights load only after the KPIs: they summarise the same snapshot */}
        {kpis.data ? <LazyInsights /> : <AiInsights loading onRegenerate={() => {}} />}
        <Section title="Operational alerts" subtitle={alerts.data ? `${alerts.data.alerts.length} items, most urgent first` : undefined}>
          {alerts.loading ? <Skeleton rows={4} /> : alerts.error ? <ErrorState message={alerts.error} onRetry={alerts.reload} />
            : <OperationalAlerts alerts={alerts.data!.alerts} onAsk={onAsk} />}
        </Section>
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <TrendPanel />
        <PromoPanel onAsk={onAsk} />
      </div>
      <OosPanel onAsk={onAsk} />
    </div>
  )
}

function LazyInsights() {
  const [state, setState] = useState<{ data?: Insights; error?: string; loading: boolean }>({ loading: true })
  const load = (refresh = false) => {
    setState((s) => ({ ...s, loading: true, error: undefined }))
    getJson<Insights>('/exec/insights', refresh ? { refresh: 'true' } : {})
      .then((data) => setState({ data, loading: false }))
      .catch((e) => setState({ error: e.message, loading: false }))
  }
  useEffect(() => { load() }, [])
  return <AiInsights {...state} onRegenerate={() => load(true)} />
}

function TrendPanel() {
  const [market, setMarket] = useState('')
  const opts = useMarketOptions()
  const q = useFetch<{ rows: TrendRow[] }>('/exec/trend', { market })
  return (
    <Section title="Sell-in vs sell-out" subtitle="Weekly cases, last 26 weeks. A gap that stays open means distributor stock is building or drawing down."
      actions={<Select label="Market" value={market} onChange={setMarket} options={opts} />}>
      {q.loading ? <Skeleton rows={6} /> : q.error ? <ErrorState message={q.error} onRetry={q.reload} />
        : !q.data!.rows.length ? <EmptyState title="No weeks to show" /> : <SellInOutChart rows={q.data!.rows} />}
    </Section>
  )
}

function OosPanel({ onAsk }: { onAsk: (p: string) => void }) {
  const demo = useDemo()
  const [market, setMarket] = useState('')
  const [all, setAll] = useState(false)
  const opts = useMarketOptions()
  const q = useFetch<{ threshold_days: number; rows: OosRow[] }>('/overview/oos', { market })
  const rows = q.data?.rows || []
  const risk = rows.reduce((s, r) => s + (r.weekly_revenue_at_risk_usd || 0), 0)
  return (
    <Section title="Out-of-stock risk"
      subtitle={q.data ? <>{rows.length} distributor positions below {q.data.threshold_days} days of cover · {fmt.usd(risk)} weekly revenue at risk</> : 'Positions below the 7-day cover threshold'}
      actions={<Select label="Market" value={market} onChange={setMarket} options={opts} />}>
      {q.loading ? <Skeleton rows={5} /> : q.error ? <ErrorState message={q.error} onRetry={q.reload} />
        : !rows.length ? <EmptyState title="No positions at risk">Every distributor in this market is above the cover threshold.</EmptyState> : (
          <div className="overflow-x-auto -mx-2">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-ink-700">
                  <th className="font-medium px-2 py-2">Market</th><th className="font-medium px-2 py-2">Distributor</th>
                  <th className="font-medium px-2 py-2">Product</th><th className="font-medium px-2 py-2 text-right">Cover</th>
                  <th className="font-medium px-2 py-2 text-right">Weekly revenue at risk</th><th className="px-2 py-2" />
                </tr>
              </thead>
              <tbody>
                {(all ? rows : rows.slice(0, 10)).map((r) => (
                  <tr key={`${r.distributor_id}-${r.sku}`} className="border-t border-line/60">
                    <td className="px-2 py-2.5">{r.market_id}</td>
                    <td className="px-2 py-2.5">{r.distributor_id}<span className="block text-xs text-ink-700">{r.distributor_name} · {r.channel}</span></td>
                    <td className="px-2 py-2.5">{r.product_name}<span className="block text-xs text-ink-700 font-mono">{r.sku}</span></td>
                    <td className="px-2 py-2.5 text-right"><Pill tone={coverTone(r.cover_days, q.data!.threshold_days)}>{fmt.days(r.cover_days)}</Pill></td>
                    <td className="px-2 py-2.5 text-right tabular-nums">{fmt.usd(r.weekly_revenue_at_risk_usd)}</td>
                    <td className="px-2 py-2.5 text-right">
                      <Button variant="ghost" className="!min-h-[36px] !px-2" onClick={() => onAsk(ask(demo, 'replenish', { distributor_id: r.distributor_id, product_name: r.product_name, sku: r.sku }))}>Ask</Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length > 10 && (
              <div className="px-2 pt-3">
                <Button variant="ghost" onClick={() => setAll(!all)}>{all ? 'Show the 10 lowest' : `Show all ${rows.length}`}</Button>
              </div>
            )}
          </div>
        )}
    </Section>
  )
}

function PromoPanel({ onAsk }: { onAsk: (p: string) => void }) {
  const demo = useDemo()
  const [promoId, setPromoId] = useState('')
  const q = useFetch<{ promo_id: string | null; selected: Promo | null; active: Promo[]; distributors: PromoDist[] }>('/overview/promo', { promo_id: promoId || undefined })
  const p = q.data?.selected
  const dist = q.data?.distributors || []
  const forecast = p?.uplift_forecast_pct
  return (
    <Section title="In-flight promotion"
      subtitle={p ? <>{p.promo_id} · {p.product_name} · {p.mechanic}, {Math.round(p.discount_pct * 100)}% off · {fmt.date(p.start_date)} to {fmt.date(p.end_date)}</> : 'Measured uplift against forecast, by distributor'}
      actions={q.data && q.data.active.length > 1 ? (
        <Select label="Promo" value={promoId || q.data.promo_id || ''} onChange={setPromoId} options={q.data.active.map((a) => ({ value: a.promo_id, label: `${a.promo_id} · ${a.market_id}` }))} />
      ) : undefined}>
      {q.loading ? <Skeleton rows={5} /> : q.error ? <ErrorState message={q.error} onRetry={q.reload} />
        : !dist.length ? <EmptyState title="No measurement yet">This promotion has no in-promo weeks to compare.</EmptyState> : (
          <div className="space-y-6">
            <dl className="flex flex-wrap items-end gap-x-8 gap-y-4">
              <div><dt className="text-xs text-ink-700">Measured uplift</dt><dd className="text-3xl font-medium mt-1 tabular-nums">{fmt.pct(p?.uplift_actual_pct)}</dd></div>
              <div><dt className="text-xs text-ink-700">Forecast</dt><dd className="text-xl mt-1 tabular-nums">{fmt.pct(forecast)}</dd></div>
              <div><dt className="text-xs text-ink-700">Gap</dt><dd className="mt-1"><Pill tone={(p?.gap_vs_forecast_pct ?? 0) < -0.1 ? 'critical' : 'warn'}>{fmt.pts(p?.gap_vs_forecast_pct)} vs forecast</Pill></dd></div>
              <Button className="ml-auto" onClick={() => onAsk(ask(demo, 'whyPromo', { promo_id: q.data!.promo_id! }))}><Icon name="assistant" />Ask why</Button>
            </dl>
            <div>
              <div className="flex items-center gap-4 text-xs text-ink-700 mb-2">
                <span className="inline-flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-navy-700" />Uplift by distributor</span>
                <span className="inline-flex items-center gap-1.5"><span className="w-3 h-0.5 bg-ink-700" />Forecast</span>
                <span className="inline-flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-lava-600" />Below baseline</span>
              </div>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={dist.map((d) => ({ ...d, label: `${d.distributor_id} · ${d.channel}`, value: d.uplift_pct * 100 }))} layout="vertical" margin={{ left: 8, right: 32 }}>
                  <CartesianGrid horizontal={false} stroke="#EEEDE9" />
                  <XAxis type="number" unit="%" domain={[Math.min(-10, ...dist.map((d) => d.uplift_pct * 100)), Math.max(50, (forecast ?? 0) * 100 + 5)]} tick={{ fontSize: 12, fill: '#4A5568' }} />
                  <YAxis type="category" dataKey="label" width={180} tick={{ fontSize: 12, fill: '#0B2026' }} />
                  <Tooltip formatter={(v: number) => [`${v.toFixed(1)}%`, 'Uplift']} labelFormatter={(l) => String(l)}
                    contentStyle={{ borderRadius: 8, border: '1px solid #C4CCD6', fontFamily: 'DM Sans' }} />
                  {forecast != null && <ReferenceLine x={forecast * 100} stroke="#4A5568" strokeDasharray="4 3" />}
                  <ReferenceLine x={0} stroke="#C4CCD6" />
                  <Bar dataKey="value" barSize={22} radius={[0, 4, 4, 0]} isAnimationActive={false}>
                    {dist.map((d) => <Cell key={d.distributor_id} fill={d.uplift_pct < 0 ? '#FF3621' : '#1B5162'} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
    </Section>
  )
}
