// Chat building blocks: tool-call chips with expandable sources, source list, approval card, message view.
import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Feedback, ProposedRequest, Source, ToolKind, fmt } from '../api'
import { Button, Icon, Pill, Spinner } from './ui'

export type ToolCall = {
  id: string; tool: string; kind: ToolKind; label: string; input?: Record<string, unknown>
  status: 'running' | 'ok' | 'error'; summary?: string; sources?: Source[]; rows?: Record<string, unknown>[]
}

export type Approval = {
  request: ProposedRequest
  // submitted = sent to the approvers' queue (four-eyes); queued = left pending when the user moved on
  state: 'pending' | 'submitting' | 'approved' | 'rejected' | 'withdrawn' | 'submitted' | 'queued' | 'error'
  requests?: ProposedRequest[]; decidedBy?: string; error?: string
}

export type Turn = {
  id: string; role: 'user' | 'assistant'; text: string; tools: ToolCall[]
  approval?: Approval; status: 'streaming' | 'done' | 'error'; error?: string
  traceId?: string; feedback?: Feedback; feedbackError?: string; feedbackSaving?: boolean
}

const KIND_LABEL: Record<ToolKind, string> = {
  genie: 'Genie', uc_function: 'UC function', documents: 'Documents', memory: 'Memory', request: 'Request',
}

export function ToolChips({ tools }: { tools: ToolCall[] }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!tools.length) return null
  const sel = tools.find((t) => t.id === open)
  return (
    <div className="mb-3">
      <div className="flex flex-wrap gap-2">
        {tools.map((t) => (
          <button key={t.id} onClick={() => setOpen(open === t.id ? null : t.id)} aria-expanded={open === t.id}
            className={`inline-flex items-center gap-2 rounded-full border px-3 min-h-[32px] text-xs transition-colors duration-150 ease-quart ${open === t.id ? 'bg-navy-900 text-white border-navy-900' : 'bg-white text-navy-900 border-line hover:bg-oat-light'}`}>
            {t.status === 'running' ? <Spinner /> : t.status === 'error' ? <Icon name="alert" className="w-3.5 h-3.5 text-rose-900" /> : <Icon name={t.kind} className="w-3.5 h-3.5" />}
            <span className="font-medium">{KIND_LABEL[t.kind] || t.kind}</span>
            <span className={open === t.id ? 'text-white/80' : 'text-ink-700'}>{t.status === 'running' ? t.label : t.summary || t.label}</span>
          </button>
        ))}
      </div>
      {sel && <ToolDetail t={sel} />}
    </div>
  )
}

function ToolDetail({ t }: { t: ToolCall }) {
  return (
    <div className="mt-2 rounded-lg border border-line bg-oat-light p-4 text-sm space-y-3">
      <div className="flex items-start gap-2 text-xs text-ink-700">
        <span className="font-mono shrink-0">{t.tool}</span>
        {/* a Genie question is shown in full; other inputs stay on one line */}
        {t.input && (t.kind === 'genie' && typeof t.input.question === 'string'
          ? <span className="whitespace-pre-wrap break-words">“{t.input.question}”</span>
          : <span className="font-mono truncate">{JSON.stringify(t.input)}</span>)}
      </div>
      {t.sources?.map((s, i) => <SourceItem key={i} s={s} />)}
      {t.rows && t.rows.length > 0 && <RowsTable rows={t.rows} />}
      {!t.sources?.length && !t.rows?.length && <p className="text-ink-700">{t.status === 'running' ? 'Waiting for the result…' : 'No details returned.'}</p>}
    </div>
  )
}

function SourceItem({ s }: { s: Source }) {
  if (s.kind === 'sql') return (
    <div>
      <div className="text-xs font-medium text-navy-900 mb-1">{s.title}</div>
      <pre className="font-mono text-xs bg-white border border-line rounded-md p-3 overflow-x-auto whitespace-pre-wrap">{s.sql}</pre>
    </div>
  )
  if (s.kind === 'memory') return (
    <div className="bg-white border border-line rounded-md p-3">
      <div className="text-xs text-ink-700 mb-1">Past decision · {s.created_at}</div>
      <div>{s.summary}</div>
    </div>
  )
  return (
    <div className="bg-white border border-line rounded-md p-3">
      <div className="text-xs mb-1"><span className="font-mono font-medium">{s.doc_id}</span>{s.section && <span className="text-ink-700"> · {s.section}</span>}</div>
      {s.snippet && <div className="text-navy-900">“{s.snippet}”</div>}
    </div>
  )
}

function RowsTable({ rows }: { rows: Record<string, unknown>[] }) {
  const cols = Object.keys(rows[0])
  return (
    <div className="overflow-x-auto">
      <table className="text-xs border-collapse">
        <thead><tr>{cols.map((c) => <th key={c} className="text-left font-medium text-ink-700 px-2 py-1 border-b border-line">{c}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c} className="px-2 py-1 font-mono border-b border-line/50">{String(r[c] ?? '–')}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}

export function DocSources({ tools }: { tools: ToolCall[] }) {
  const docs = new Map<string, string | undefined>()
  tools.forEach((t) => t.sources?.forEach((s) => { if (s.kind === 'doc' && !docs.has(s.doc_id)) docs.set(s.doc_id, s.section) }))
  if (!docs.size) return null
  return (
    <div className="mt-3 pt-3 border-t border-line/60 flex flex-wrap items-center gap-2 text-xs">
      <span className="text-ink-700">Sources</span>
      {[...docs].map(([id, section]) => (
        <span key={id} className="inline-flex items-center gap-1 rounded-md bg-oat-medium px-2 py-1">
          <Icon name="documents" className="w-3 h-3" /><span className="font-mono">{id}</span>{section && <span className="text-ink-700">· {section}</span>}
        </span>
      ))}
    </div>
  )
}

export type Viewer = { email: string; selfApproval: boolean }

export function ApprovalCard({ a, canApprove, onDecide, viewer }: {
  a: Approval; canApprove: boolean; onDecide: (decision: 'approve' | 'reject', reason?: string) => void; viewer?: Viewer
}) {
  const [reason, setReason] = useState('')
  const r = a.request
  // four-eyes: the requester never decides their own request (unless the demo-only exception is on)
  // tool output masks people: the caller's own requests read "you"
  const ownRequest = !!viewer && !viewer.selfApproval && !!r.requested_by && (r.requested_by === 'you' || r.requested_by.toLowerCase() === viewer.email.toLowerCase())
  const done = a.state === 'approved' || a.state === 'rejected' || a.state === 'withdrawn'
  const inQueue = a.state === 'submitted' || a.state === 'queued'
  const busy = a.state === 'submitting'
  return (
    <div className="mt-4 rounded-card border border-line bg-white overflow-hidden" role="group" aria-label="Replenishment request approval">
      <div className="px-5 py-3 bg-oat-medium flex items-center justify-between gap-3">
        <div className="text-sm font-medium">Replenishment request</div>
        {done ? <Pill tone={a.state === 'approved' ? 'ok' : 'neutral'}>{a.state === 'approved' ? 'Approved' : a.state === 'withdrawn' ? 'Withdrawn' : 'Rejected'}{a.decidedBy ? ` by ${a.decidedBy}` : ''}</Pill>
          : inQueue ? <Pill tone="warn">{a.state === 'submitted' ? 'Sent to approvers' : 'In the Requests queue'}</Pill>
          : <Pill tone="warn">Waiting for approval</Pill>}
      </div>
      <dl className="px-5 py-4 grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-3 text-sm">
        <Field k="Distributor" v={<>{r.distributor_id}<span className="block text-xs text-ink-700">{r.distributor_name}</span></>} />
        <Field k="Product" v={<>{r.product_name || r.sku}<span className="block text-xs text-ink-700 font-mono">{r.sku}</span></>} />
        <Field k="Quantity" v={<span className="text-lg font-medium">{fmt.cases(r.qty_cases)}</span>} />
        <Field k="Stock cover" v={<>{fmt.days(r.cover_days)}{r.target_cover_days != null && <span className="block text-xs text-ink-700">target {r.target_cover_days} d</span>}</>} />
        <div className="col-span-2 md:col-span-4"><Field k="Reason (drafted by the assistant)" v={r.reason} /></div>
        {(r.needs_manager_approval === true || String(r.needs_manager_approval).toLowerCase() === 'true') && <div className="col-span-2 md:col-span-4"><Pill tone="warn">Above the policy threshold: needs a manager's approval</Pill></div>}
      </dl>
      {!done && inQueue && (
        <p className="px-5 pb-5 text-sm text-ink-700">{a.state === 'submitted'
          ? 'An approver decides this request on the Requests page.'
          : 'Still waiting for a decision on the Requests page. Ask me to change or withdraw it.'}</p>
      )}
      {!done && !inQueue && (
        <div className="px-5 pb-5 space-y-3">
          {canApprove && ownRequest ? (
            <p className="text-sm text-ink-700">Waiting for another approver. You proposed this request, so someone else decides it on the Requests page.</p>
          ) : canApprove ? (
            <>
              <label className="block text-sm">
                <span className="text-ink-700">Note for the decision (optional)</span>
                <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} disabled={busy}
                  placeholder="For example: ship from the next allocation slot"
                  className="mt-1 w-full border border-line rounded-md px-3 min-h-[40px] bg-white" />
              </label>
              <div className="flex gap-2">
                <Button variant="primary" disabled={busy} onClick={() => onDecide('approve', reason || undefined)}>
                  {busy ? <Spinner /> : <Icon name="check" />}Approve request
                </Button>
                <Button disabled={busy} onClick={() => onDecide('reject', reason || undefined)}><Icon name="x" />Reject</Button>
              </div>
            </>
          ) : <p className="text-sm text-ink-700">An approver has to decide on this request. You can see it on the Requests page.</p>}
          {a.state === 'error' && <p role="alert" className="text-sm text-rose-900">{a.error}</p>}
        </div>
      )}
    </div>
  )
}

function Field({ k, v }: { k: string; v: React.ReactNode }) {
  return <div><dt className="text-xs text-ink-700">{k}</dt><dd className="mt-0.5">{v}</dd></div>
}

function FeedbackBar({ t, onFeedback }: { t: Turn; onFeedback: (r: 'up' | 'down', c?: string) => void }) {
  const [asking, setAsking] = useState(false)
  const [comment, setComment] = useState('')
  const r = t.feedback?.rating
  // 👎 is recorded on click; the optional comment is sent as an update of the same assessment
  const click = (k: 'up' | 'down') => { onFeedback(k); setAsking(k === 'down') }
  const btn = (k: 'up' | 'down', label: string) => (
    <button type="button" aria-label={label} aria-pressed={r === k} onClick={() => click(k)}
      className={`p-1.5 rounded hover:bg-oat-light ${r === k ? 'text-navy-900' : 'text-ink-500'}`}>
      <Icon name={k === 'up' ? 'thumbUp' : 'thumbDown'} className="w-4 h-4" />
    </button>
  )
  return (
    <div className="mt-2">
      <div className="flex items-center gap-1 text-xs text-ink-700">
        {btn('up', 'Helpful')}{btn('down', 'Not helpful')}
        {t.feedbackSaving ? <span>Saving…</span> : r && !t.feedbackError && <span>Thanks for the feedback{t.feedback?.comment ? ' and the comment' : ''}</span>}
        {t.feedbackError && <span role="alert" className="text-rose-900">{t.feedbackError}</span>}
      </div>
      {asking && (
        <form className="mt-2 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (comment.trim()) onFeedback('down', comment.trim()); setAsking(false) }}>
          <label className="sr-only" htmlFor={`fb-${t.id}`}>What was wrong?</label>
          <input id={`fb-${t.id}`} value={comment} maxLength={500} onChange={(e) => setComment(e.target.value)} placeholder="What was wrong? (optional)"
            className="flex-1 rounded border border-line px-2 py-1 text-sm" />
          <Button type="submit">Send</Button>
        </form>
      )}
    </div>
  )
}

// model output is untrusted: no remote images (silent exfiltration), links open isolated
const MD_COMPONENTS = {
  img: () => null,
  a: ({ node: _n, ...p }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { node?: unknown }) => <a {...p} target="_blank" rel="noopener noreferrer" />,
}

export function Message({ t, canApprove, onDecide, onFeedback, viewer }: { t: Turn; canApprove: boolean; onDecide: (d: 'approve' | 'reject', r?: string) => void; onFeedback?: (r: 'up' | 'down', c?: string) => void; viewer?: Viewer }) {
  if (t.role === 'user') return (
    <div className="flex justify-end">
      <div className="max-w-[80%] rounded-card bg-navy-900 text-white px-4 py-3 whitespace-pre-wrap">{t.text}</div>
    </div>
  )
  const thinking = t.status === 'streaming' && !t.text && !t.tools.length
  return (
    <div className="flex gap-3">
      <span className="w-8 h-8 shrink-0 rounded-full bg-navy-900 grid place-items-center" aria-hidden><span className="w-2.5 h-2.5 rounded-full bg-lava-600" /></span>
      <div className="flex-1 min-w-0 pt-1">
        <ToolChips tools={t.tools} />
        {thinking && <p className="text-ink-700 text-sm flex items-center gap-2"><Spinner />Working on it…</p>}
        {t.text && (
          <div className={`prose-agent text-[15px] ${t.status === 'streaming' && !t.approval ? 'caret' : ''}`}>
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>{t.text}</ReactMarkdown>
          </div>
        )}
        {t.status === 'error' && <p role="alert" className="mt-2 text-sm text-rose-900">{t.error}</p>}
        <DocSources tools={t.tools} />
        {t.approval && (t.approval.requests && t.approval.requests.length > 1
          ? t.approval.requests.map((r, i) => <ApprovalCard key={r.request_id || i} a={{ ...t.approval!, request: r }} canApprove={canApprove} onDecide={onDecide} viewer={viewer} />)
          : <ApprovalCard a={t.approval} canApprove={canApprove} onDecide={onDecide} viewer={viewer} />)}
        {t.status !== 'streaming' && t.traceId && onFeedback && <FeedbackBar t={t} onFeedback={onFeedback} />}
      </div>
    </div>
  )
}
