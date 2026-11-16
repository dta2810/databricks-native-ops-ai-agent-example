// Executive visuals (patterns ported from FE BAR: KpiTile, trend chart, OperationalAlerts, AiInsights), on the Databricks light palette.
import { useState } from 'react'
import { CartesianGrid, Legend as RLegend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { fmt } from '../api'
import { Button, Icon, Pill, Skeleton, Tone } from './ui'

export const SERIES = { sellIn: '#618794', sellOut: '#0B2026', grid: '#EEEDE9', axis: '#4A5568' }
const TOOLTIP = { borderRadius: 8, border: '1px solid #C4CCD6', fontFamily: 'DM Sans', fontSize: 13 }

// ---------- KPI tile ----------

export type KpiTileData = {
  id: string; label: string; value: number | null; format: 'currency' | 'cases' | 'count' | 'percent'
  delta?: number | null; delta_label?: string; subtitle?: string; tone?: 'neutral' | 'good' | 'warn' | 'alert'
}

const TILE_TONE: Record<NonNullable<KpiTileData['tone']>, { ring: string; dot: string | null }> = {
  neutral: { ring: 'border-line/70', dot: null },
  good: { ring: 'border-ok/40', dot: 'bg-ok' },
  warn: { ring: 'border-warn/60', dot: 'bg-warn' },
  alert: { ring: 'border-lava-600/50', dot: 'bg-lava-600' },
}

function tileValue(t: KpiTileData) {
  if (t.value == null) return '–'
  if (t.format === 'currency') return fmt.compactUsd(t.value)
  if (t.format === 'percent') return fmt.pct(t.value)
  return fmt.num(t.value)
}

export function KpiTile({ t }: { t: KpiTileData }) {
  const tone = TILE_TONE[t.tone || 'neutral']
  const up = (t.delta ?? 0) > 0
  return (
    <div className={`bg-white border rounded-card p-5 flex flex-col gap-1.5 ${tone.ring}`}>
      <div className="flex items-center gap-2 text-sm text-ink-700">
        {tone.dot && <span className={`w-2 h-2 rounded-full ${tone.dot}`} aria-hidden />}
        {t.label}
      </div>
      <div className="text-3xl font-medium tabular-nums text-navy-900">
        {tileValue(t)}{t.format === 'cases' && t.value != null && <span className="text-base text-ink-700 font-normal"> cases</span>}
      </div>
      {t.delta != null && Math.abs(t.delta) > 0.0005 && (
        <div className={`text-sm tabular-nums ${up ? 'text-ok-800' : 'text-rose-900'}`}>
          {up ? '▲' : '▼'} {fmt.pct(t.delta)} <span className="text-ink-700">{t.delta_label}</span>
        </div>
      )}
      {t.subtitle && <div className="text-sm text-ink-700">{t.subtitle}</div>}
    </div>
  )
}

export function KpiGrid({ tiles, loading, error }: { tiles?: KpiTileData[]; loading: boolean; error?: string }) {
  if (loading) return <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="bg-white border border-line/70 rounded-card p-5"><Skeleton rows={2} /></div>)}</div>
  if (error) return <div role="alert" className="text-sm text-rose-900">Couldn't load the KPIs: {error}</div>
  return <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{tiles?.map((t) => <KpiTile key={t.id} t={t} />)}</div>
}

// ---------- sell-in vs sell-out trend ----------

export type TrendRow = { week_start: string; sell_in_cases: number; sell_out_cases: number }

export function SellInOutChart({ rows, height = 260 }: { rows: TrendRow[]; height?: number }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={rows} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke={SERIES.grid} />
        <XAxis dataKey="week_start" tickFormatter={fmt.week} tick={{ fontSize: 12, fill: SERIES.axis }} minTickGap={24} />
        <YAxis domain={['auto', 'auto']} tickFormatter={(v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v)))} tick={{ fontSize: 12, fill: SERIES.axis }} width={48} />
        <Tooltip contentStyle={TOOLTIP} labelFormatter={(l) => `Week of ${fmt.week(String(l))}`} formatter={(v: number, n) => [`${fmt.num(v)} cases`, n]} />
        <RLegend iconType="plainline" wrapperStyle={{ fontSize: 13, paddingTop: 8 }} />
        <Line type="monotone" dataKey="sell_in_cases" name="Sell-in (shipments)" stroke={SERIES.sellIn} strokeWidth={2} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="sell_out_cases" name="Sell-out (depletions)" stroke={SERIES.sellOut} strokeWidth={2.5} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}

// ---------- operational alerts ----------

export type Alert = { id: string; category: 'oos' | 'promo' | 'request'; severity: 'alert' | 'warn' | 'info' | 'good'; title: string; body: string; market: string | null; ask: string | null }

const SEV: Record<Alert['severity'], { tone: Tone; label: string }> = {
  alert: { tone: 'critical', label: 'Act now' }, warn: { tone: 'warn', label: 'Watch' }, info: { tone: 'info', label: 'FYI' }, good: { tone: 'ok', label: 'Good' },
}
const CAT: Record<Alert['category'], { icon: string; label: string }> = {
  oos: { icon: 'alert', label: 'Out of stock' }, promo: { icon: 'genie', label: 'Promotion' }, request: { icon: 'requests', label: 'Requests' },
}

export function OperationalAlerts({ alerts, onAsk }: { alerts: Alert[]; onAsk: (p: string) => void }) {
  const [all, setAll] = useState(false)
  const shown = all ? alerts : alerts.slice(0, 4)
  if (!alerts.length) return <p className="text-sm text-ink-700">Nothing needs attention right now.</p>
  return (
    <div>
      <ul className="grid gap-3 md:grid-cols-2">
        {shown.map((a) => (
          <li key={a.id} className="bg-white border border-line/70 rounded-card p-4 flex flex-col gap-2">
            <div className="flex items-center gap-2 flex-wrap text-xs">
              <Pill tone={SEV[a.severity].tone}>{SEV[a.severity].label}</Pill>
              <span className="inline-flex items-center gap-1 text-ink-700"><Icon name={CAT[a.category].icon} className="w-3.5 h-3.5" />{CAT[a.category].label}</span>
              {a.market && <span className="text-ink-700">· {a.market}</span>}
            </div>
            <div className="font-medium text-navy-900 leading-snug">{a.title}</div>
            <p className="text-sm text-ink-700">{a.body}</p>
            {a.ask && <div className="pt-1"><Button variant="ghost" className="!px-2 -ml-2" onClick={() => onAsk(a.ask!)}><Icon name="assistant" />Ask the agent</Button></div>}
            {!a.ask && a.category === 'request' && <a href="#/requests" className="text-sm font-medium underline underline-offset-2">Open requests</a>}
          </li>
        ))}
      </ul>
      {alerts.length > 4 && <Button variant="ghost" className="mt-3" onClick={() => setAll(!all)}>{all ? 'Show fewer' : `Show all ${alerts.length}`}</Button>}
    </div>
  )
}

// ---------- AI insights (llm_fast) ----------

export type Insights = { bullets: string[]; model: string; generated_at: string; cached: boolean; latency_ms: number; as_of_week: string }

export function AiInsights({ data, loading, error, onRegenerate }: { data?: Insights; loading: boolean; error?: string; onRegenerate: () => void }) {
  return (
    <section className="bg-white border border-line/70 rounded-card p-6" aria-busy={loading}>
      <header className="flex items-center justify-between gap-3 mb-4">
        <h2 className="flex items-center gap-2 text-lg font-medium"><Icon name="genie" className="w-4 h-4 text-lava-600" />What matters this week</h2>
        <Button variant="ghost" onClick={onRegenerate} disabled={loading} aria-label="Regenerate insights"><Icon name="refresh" />Regenerate</Button>
      </header>
      {loading ? <Skeleton rows={3} />
        : error ? <p role="alert" className="text-sm text-rose-900">{error}</p>
          : (
            <>
              <ol className="space-y-3">
                {data?.bullets.map((b, i) => (
                  <li key={i} className="flex gap-3">
                    <span className="shrink-0 w-6 h-6 rounded-full bg-oat-medium grid place-items-center text-xs font-medium">{i + 1}</span>
                    <span className="leading-relaxed">{b}</span>
                  </li>
                ))}
              </ol>
              <p className="mt-4 text-xs text-ink-700">
                Generated by <span className="font-mono">{data?.model.split('.').pop()}</span> from the KPIs and alerts on this page · {data && fmt.ago(data.generated_at)}{data?.cached ? ' · cached' : ''}
              </p>
            </>
          )}
    </section>
  )
}

// ---------- cover status chip ----------

export function CoverChip({ status, days }: { status: string; days: number }) {
  const tone: Tone = status === 'AT RISK' ? 'critical' : status === 'BELOW TARGET' ? 'warn' : 'ok'
  return <Pill tone={tone}>{fmt.days(days)}<span className="font-normal">· {status === 'AT RISK' ? 'at risk' : status === 'BELOW TARGET' ? 'below target' : 'ok'}</span></Pill>
}
