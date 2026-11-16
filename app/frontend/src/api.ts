// API client + types. Components receive these callbacks as props (dependency injection, configurable-react-template pattern).
import { useEffect, useState } from 'react'

export type Role = { role: string; label: string; can_propose: boolean; can_approve: boolean }
export type Me = {
  email: string; roles: Role[]; can_approve: boolean; can_propose?: boolean; is_manager?: boolean; self_approval?: boolean; self_approval_ignored?: boolean
  manager_threshold_cases?: number; agent_mode: 'stub' | 'graph' | string; local_dev: boolean
}

export type Source =
  | { kind: 'doc'; doc_id: string; section?: string; snippet?: string }
  | { kind: 'sql'; title: string; sql: string }
  | { kind: 'memory'; decision_id: string; created_at?: string; summary: string }

export type ToolKind = 'genie' | 'uc_function' | 'documents' | 'memory' | 'request'

export type ProposedRequest = {
  request_id?: string; market_id: string; distributor_id: string; distributor_name?: string; sku: string; product_name?: string
  qty_cases: number; reason: string; cover_days?: number; target_cover_days?: number; needs_manager_approval?: boolean; requested_by?: string
}

export type AgentEvent =
  | { type: 'token'; text: string }
  | { type: 'tool_start'; id: string; tool: string; kind: ToolKind; label: string; input?: Record<string, unknown> }
  | { type: 'tool_end'; id: string; ok: boolean; summary?: string; sources?: Source[]; rows?: Record<string, unknown>[] }
  | { type: 'approval_required'; request: ProposedRequest }
  | { type: 'requests_submitted'; requests: ProposedRequest[] }
  | { type: 'decision'; status: 'approved' | 'rejected'; request: ProposedRequest; decided_by: string }
  | { type: 'final'; trace_id?: string }
  | { type: 'error'; message: string }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

async function detail(r: Response) {
  const body = await r.json().catch(() => ({}))
  return typeof body.detail === 'string' ? body.detail : `HTTP ${r.status}`
}

export async function getJson<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const q = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => { if (v !== undefined && v !== '') q.set(k, String(v)) })
  const r = await fetch(`/api${path}${q.toString() ? `?${q}` : ''}`, { credentials: 'same-origin' })
  if (!r.ok) throw new ApiError(r.status, await detail(r))
  return r.json()
}

// POST + Server-Sent Events: EventSource can't POST, so parse the stream by hand
// body === undefined -> GET (re-attach to a running turn; 204 = nothing live)
export async function stream(path: string, body: unknown, onEvent: (e: AgentEvent) => void, signal?: AbortSignal) {
  const r = await fetch(`/api${path}`, body === undefined
    ? { method: 'GET', headers: { Accept: 'text/event-stream' }, signal, credentials: 'same-origin' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' }, body: JSON.stringify(body), signal, credentials: 'same-origin' })
  if (r.status === 204) return
  if (!r.ok || !r.body) throw new ApiError(r.status, await detail(r))
  const reader = r.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i)
      buf = buf.slice(i + 2)
      const data = chunk.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trim()).join('')
      if (data) onEvent(JSON.parse(data))
    }
  }
}

export type Thread = { thread_id: string; title: string; created_at: string; updated_at: string; pending_approval?: boolean }
export type StoredMessage = {
  id: string; role: 'user' | 'assistant'; content: string; status: 'done' | 'error'; error?: string | null
  tool_events: unknown[]; sources: Source[]; approval: unknown | null; created_at: string
  trace_id?: string | null; feedback?: Feedback | null
}

export type Feedback = { rating: 'up' | 'down'; comment?: string | null }

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined, credentials: 'same-origin',
  })
  if (!r.ok) throw new ApiError(r.status, await detail(r))
  return r.json()
}

export const threadsApi = {
  list: (q?: string) => getJson<{ threads: Thread[] }>('/threads', { q }),
  create: () => send<Thread>('POST', '/threads', {}),
  messages: (id: string) => getJson<{ thread: Thread; messages: StoredMessage[]; running?: boolean }>(`/threads/${id}/messages`),
  rename: (id: string, title: string) => send<{ ok: boolean }>('PATCH', `/threads/${id}`, { title }),
  remove: (id: string) => send<{ ok: boolean }>('DELETE', `/threads/${id}`),
}

export type Market = { market_id: string; market_name: string; region: string }
export type Distributor = { distributor_id: string; distributor_name: string; market_id: string; channel: string }
type Reference = { markets: Market[]; distributors: Distributor[] }

// markets + distributors from dim_market / dim_distributor, fetched once per page load
let refPromise: Promise<Reference> | null = null
export function useReference() {
  const [ref, setRef] = useState<Reference | null>(null)
  useEffect(() => {
    refPromise = refPromise || getJson<Reference>('/markets')
    refPromise.then(setRef).catch(() => { refPromise = null })
  }, [])
  return ref
}

export type ChatApi = {
  send: (threadId: string, message: string, onEvent: (e: AgentEvent) => void, signal?: AbortSignal) => Promise<void>
  resume: (threadId: string, decision: 'approve' | 'reject', reason: string | undefined, onEvent: (e: AgentEvent) => void) => Promise<void>
  attach: (threadId: string, onEvent: (e: AgentEvent) => void, signal?: AbortSignal) => Promise<void>
  feedback: (threadId: string, traceId: string, rating: 'up' | 'down', comment?: string) => Promise<unknown>
}

export const decideRequest = (requestId: string, decision: 'approve' | 'reject', reason?: string) =>
  send<{ ok: boolean; status: string }>('POST', `/requests/${encodeURIComponent(requestId)}/decision`, { decision, reason })

export const chatApi: ChatApi = {
  send: (thread_id, message, onEvent, signal) => stream('/chat', { thread_id, message }, onEvent, signal),
  resume: (thread_id, decision, reason, onEvent) => stream('/chat/resume', { thread_id, decision, reason }, onEvent),
  attach: (thread_id, onEvent, signal) => stream(`/chat/stream/${thread_id}`, undefined, onEvent, signal),
  feedback: (thread_id, trace_id, rating, comment) => send('POST', '/chat/feedback', { thread_id, trace_id, rating, comment }),
}

export function useFetch<T>(path: string, params: Record<string, string | number | undefined>) {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true })
  const [tick, setTick] = useState(0)
  const key = JSON.stringify(params)
  useEffect(() => {
    let live = true
    setState((s) => ({ ...s, loading: true, error: undefined }))
    getJson<T>(path, params)
      .then((data) => live && setState({ data, loading: false }))
      .catch((e) => live && setState({ error: e.message, loading: false }))
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, key, tick])
  return { ...state, reload: () => setTick((t) => t + 1) }
}

export function useRoute(): [string, (p: string) => void] {
  const read = () => window.location.hash.replace(/^#\/?/, '').split('?')[0] || 'overview'
  const [page, setPage] = useState(read)
  useEffect(() => {
    const on = () => setPage(read())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return [page, (p: string) => { window.location.hash = `#/${p}` }]
}

// API values may arrive as numeric strings (Postgres numeric -> str): coerce, '–' when not a number
type Num = number | string | null | undefined
const n = (v: Num): number | null => (v == null || v === '' || Number.isNaN(Number(v)) ? null : Number(v))

export const fmt = {
  days: (raw: Num) => ((v) => (v == null ? '–' : `${v.toFixed(1)} d`))(n(raw)),
  pct: (raw: Num, signed = true) => ((v) => (v == null ? '–' : `${signed && v > 0 ? '+' : ''}${(v * 100).toFixed(1).replace('.0', '')}%`))(n(raw)),
  pts: (raw: Num) => ((v) => (v == null ? '–' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(0)} pts`))(n(raw)),
  usd: (raw: Num) => ((v) => (v == null ? '–' : `$${Math.round(v).toLocaleString('en-US')}`))(n(raw)),
  cases: (raw: Num) => ((v) => (v == null ? '–' : `${Math.round(v).toLocaleString('en-US')} cases`))(n(raw)),
  num: (raw: Num) => ((v) => (v == null ? '–' : Math.round(v).toLocaleString('en-US')))(n(raw)),
  compactUsd: (raw: Num) => ((v) => (v == null ? '–' : v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : v >= 1e4 ? `$${(v / 1e3).toFixed(1)}K` : `$${Math.round(v).toLocaleString('en-US')}`))(n(raw)),
  week: (iso: string) => new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }),
  ago: (iso: string) => {
    const s = (Date.now() - new Date(iso).getTime()) / 1000
    return s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`
  },
  date: (iso: string | null | undefined) => (iso ? new Date(iso).toISOString().slice(0, 10) : '–'),
  dateTime: (iso: string | null | undefined) => (iso ? new Date(iso).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : '–'),
}
