import { useState } from 'react'
import { Me, decideRequest, fmt, useFetch } from '../api'
import { Button, EmptyState, ErrorState, Icon, Pill, Section, Select, Skeleton, Tone } from '../components/ui'
import { useMarketOptions } from './Overview'
import { ask, useDemo } from '../demo_config'

type Req = {
  request_id: string; created_at: string; requested_by: string; market_id: string; distributor_id: string; sku: string
  qty_cases: number; reason: string; status: 'proposed' | 'approved' | 'rejected' | 'withdrawn'; decided_by: string | null; decided_at: string | null; decision_reason: string | null
}

const STATUS: Record<Req['status'], { tone: Tone; label: string }> = {
  proposed: { tone: 'warn', label: 'Waiting for approval' }, approved: { tone: 'ok', label: 'Approved' }, rejected: { tone: 'neutral', label: 'Rejected' },
  withdrawn: { tone: 'neutral', label: 'Withdrawn' },
}

function Decide({ r, me, onDone }: { r: Req; me: Me; onDone: () => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const own = !me.self_approval && r.requested_by.toLowerCase() === me.email.toLowerCase()
  const overThreshold = me.manager_threshold_cases != null && Number(r.qty_cases) > me.manager_threshold_cases
  if (!me.can_approve) return null
  if (own) return <p className="text-xs text-ink-700 mt-2">You proposed this request: another approver decides it.</p>
  if (overThreshold && !me.is_manager) return <p className="text-xs text-ink-700 mt-2">Above the policy threshold: a manager decides it.</p>
  const go = async (d: 'approve' | 'reject') => {
    setBusy(true); setErr(null)
    try { await decideRequest(r.request_id, d, reason.trim() || undefined); onDone() } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <label className="sr-only" htmlFor={`note-${r.request_id}`}>Note for the decision</label>
      <input id={`note-${r.request_id}`} value={reason} maxLength={500} disabled={busy} onChange={(e) => setReason(e.target.value)}
        placeholder="Note for the decision (optional)" className="border border-line rounded-md px-3 min-h-[36px] text-sm bg-white w-72" />
      <Button variant="primary" disabled={busy} onClick={() => go('approve')}><Icon name="check" />Approve</Button>
      <Button disabled={busy} onClick={() => go('reject')}><Icon name="x" />Reject</Button>
      {err && <span role="alert" className="text-sm text-rose-900">{err}</span>}
    </div>
  )
}

export default function Requests({ onAsk, me }: { onAsk: (p: string) => void; me: Me }) {
  const [status, setStatus] = useState('')
  const [market, setMarket] = useState('')
  const markets = useMarketOptions()
  const demo = useDemo()
  const q = useFetch<{ rows: Req[] }>('/requests', { status, market })
  const rows = q.data?.rows || []
  return (
    <div className="px-6 py-8 max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-medium">Replenishment requests</h1>
        <p className="text-ink-700 mt-1">Every request the assistant proposed, and who decided on it.</p>
      </div>
      <Section title={`${rows.length} request${rows.length === 1 ? '' : 's'}`}
        actions={
          <div className="flex flex-wrap gap-3 items-center">
            <Select label="Status" value={status} onChange={setStatus} options={[{ value: '', label: 'All' }, { value: 'proposed', label: 'Waiting' }, { value: 'approved', label: 'Approved' }, { value: 'rejected', label: 'Rejected' }, { value: 'withdrawn', label: 'Withdrawn' }]} />
            <Select label="Market" value={market} onChange={setMarket} options={markets} />
            <Button variant="ghost" onClick={q.reload} aria-label="Refresh"><Icon name="refresh" /></Button>
          </div>
        }>
        {q.loading ? <Skeleton rows={4} /> : q.error ? <ErrorState message={q.error} onRetry={q.reload} />
          : !rows.length ? (
            <EmptyState title="No requests yet">
              <button className="underline underline-offset-2" onClick={() => onAsk(ask(demo, 'anyRisk'))}>Ask the assistant</button> to check stock cover and propose one.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-line/60">
              {rows.map((r) => (
                <li key={r.request_id} className="py-4 grid gap-3 md:grid-cols-[1fr_auto] items-start">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{fmt.cases(r.qty_cases)}</span>
                      <span className="text-ink-700">of</span><span className="font-mono text-sm">{r.sku}</span>
                      <span className="text-ink-700">for</span><span className="font-medium">{r.distributor_id}</span>
                      <Pill tone="neutral">{r.market_id}</Pill>
                    </div>
                    <p className="text-sm text-ink-700 mt-1">{r.reason}</p>
                    {r.decision_reason && <p className="text-sm mt-1"><span className="text-ink-700">Decision note: </span>{r.decision_reason}</p>}
                    <p className="text-xs text-ink-700 mt-2">Requested by {r.requested_by} · {fmt.dateTime(r.created_at)}
                      {r.decided_by && <> · decided by {r.decided_by} · {fmt.dateTime(r.decided_at)}</>}</p>
                    {r.status === 'proposed' && <Decide r={r} me={me} onDone={q.reload} />}
                  </div>
                  <Pill tone={STATUS[r.status].tone}>{STATUS[r.status].label}</Pill>
                </li>
              ))}
            </ul>
          )}
      </Section>
    </div>
  )
}
