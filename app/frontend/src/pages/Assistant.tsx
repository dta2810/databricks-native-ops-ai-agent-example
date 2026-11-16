import { useCallback, useEffect, useRef, useState } from 'react'
import { AgentEvent, ApiError, ChatApi, Me, StoredMessage, Thread, fmt, threadsApi } from '../api'
import { Approval, Message, ToolCall, Turn } from '../components/chat'
import { Button, Icon, Skeleton, Spinner } from '../components/ui'
import { useDemo } from '../demo_config'

const ACTIVE = 'bev-ops.thread.v2'
const uid = () => crypto.randomUUID()

// fold one stream event into the assistant turn it belongs to
function apply(t: Turn, e: AgentEvent): Turn {
  switch (e.type) {
    case 'token': return { ...t, text: t.text + e.text }
    // preamble text before a tool call is dropped: the answer comes after the tools
    case 'tool_start': return { ...t, text: '', tools: [...t.tools, { id: e.id, tool: e.tool, kind: e.kind, label: e.label, input: e.input, status: 'running' }] }
    case 'tool_end': return { ...t, tools: t.tools.map((x) => (x.id === e.id ? { ...x, status: e.ok ? 'ok' : 'error', summary: e.summary, sources: e.sources, rows: e.rows } : x)) }
    case 'approval_required': return { ...t, approval: { request: e.request, state: 'pending' } }
    case 'requests_submitted': return e.requests.length ? { ...t, approval: { request: e.requests[0], requests: e.requests, state: 'submitted' } } : t
    // an error earlier in the stream wins: final never turns a failed turn into a done one
    case 'final': return { ...t, status: t.status === 'error' ? 'error' : 'done', traceId: e.trace_id || t.traceId }
    case 'error': return { ...t, status: 'error', error: e.message }
    default: return t
  }
}

// persisted message → render model (tool chips, sources, approval card keep their final state)
const fromStored = (m: StoredMessage): Turn => ({
  id: m.id, role: m.role, text: m.content, tools: (m.tool_events || []) as ToolCall[],
  approval: (m.approval as Approval | null) || undefined, status: m.status, error: m.error || undefined,
  traceId: m.trace_id || undefined, feedback: m.feedback || undefined,
})

export default function Assistant({ me, api, seed, onSeedUsed }: { me: Me; api: ChatApi; seed: string | null; onSeedUsed: () => void }) {
  const [threads, setThreads] = useState<Thread[] | null>(null)
  const [threadsError, setThreadsError] = useState<string | null>(null)
  const [active, setActive] = useState<string | null>(() => localStorage.getItem(ACTIVE))
  const [turns, setTurns] = useState<Turn[]>([])
  const [loadingThread, setLoadingThread] = useState(false)
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState('')
  const busy = turns.some((t) => t.status === 'streaming') || turns.some((t) => t.approval?.state === 'submitting')
  const end = useRef<HTMLDivElement>(null)
  const abort = useRef<AbortController | null>(null)
  // a thread created in this tab starts empty: don't reload it over the turn that is streaming into it
  const fresh = useRef<string | null>(null)

  const refreshThreads = useCallback((q?: string) => {
    threadsApi.list(q).then((d) => { setThreads(d.threads); setThreadsError(null) }).catch((e) => setThreadsError(e.message))
  }, [])

  useEffect(() => { refreshThreads() }, [refreshThreads])
  useEffect(() => { const t = setTimeout(() => refreshThreads(search || undefined), 250); return () => clearTimeout(t) }, [search, refreshThreads])
  useEffect(() => { if (active) localStorage.setItem(ACTIVE, active); else localStorage.removeItem(ACTIVE) }, [active])
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [turns])

  // load the selected thread from Lakebase
  useEffect(() => {
    // a cancelled load (switch or new chat) must not leave the spinner on
    if (!active) { setTurns([]); setLoadingThread(false); return }
    if (fresh.current === active) { setLoadingThread(false); return }
    let live = true
    setLoadingThread(true)
    threadsApi.messages(active)
      .then((d) => {
        if (!live) return
        setTurns(d.messages.map(fromStored))
        if (d.running) follow(active)  // a turn is still answering here: re-attach to it
      })
      .catch(() => { if (live) { setActive(null); setTurns([]) } })  // gone or not yours
      .finally(() => live && setLoadingThread(false))
    return () => { live = false }
  }, [active])

  useEffect(() => {
    if (seed && !busy) { newChat().then((id) => send(seed, id)); onSeedUsed() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed])

  const patch = (id: string, f: (t: Turn) => Turn) => setTurns((ts) => ts.map((t) => (t.id === id ? f(t) : t)))

  // replay + follow the server-side turn, then show what was stored (authoritative: tools, card, trace id)
  function follow(tid: string) {
    const a: Turn = { id: uid(), role: 'assistant', text: '', tools: [], status: 'streaming' }
    setTurns((ts) => [...ts, a])
    const ctl = new AbortController()
    abort.current = ctl
    api.attach(tid, (e) => patch(a.id, (t) => apply(t, e)), ctl.signal)
      .then(() => threadsApi.messages(tid))
      .then((d) => { if (!ctl.signal.aborted) setTurns(d.messages.map(fromStored)) })
      .catch(() => { if (!ctl.signal.aborted) patch(a.id, (t) => (t.status === 'streaming' ? { ...t, status: 'done' } : t)) })
  }

  // leaving a conversation only drops this tab's stream; the server keeps answering and stores the turn
  function open(tid: string) {
    abort.current?.abort()
    fresh.current = null
    setActive(tid)
  }

  async function newChat() {
    abort.current?.abort()
    const t = await threadsApi.create()
    fresh.current = t.thread_id
    setTurns([])
    setActive(t.thread_id)
    setThreads((ts) => [t, ...(ts || [])])
    return t.thread_id
  }

  async function send(text: string, threadId?: string) {
    const msg = text.trim()
    if (!msg || busy) return
    const tid = threadId || active || (await newChat())
    const a: Turn = { id: uid(), role: 'assistant', text: '', tools: [], status: 'streaming' }
    const q: Turn = { id: uid(), role: 'user', text: msg, tools: [], status: 'done' }
    // a card left pending stays in the Requests queue (the server marks it the same way)
    setTurns((ts) => [...ts.map((t) => (t.approval?.state === 'pending' ? { ...t, approval: { ...t.approval, state: 'queued' as const } } : t)), q, a])
    setDraft('')
    abort.current = new AbortController()
    try {
      await api.send(tid, msg, (e) => patch(a.id, (t) => apply(t, e)), abort.current.signal)
      patch(a.id, (t) => (t.status === 'streaming' ? { ...t, status: 'done' } : t))
    } catch (err) {
      const aborted = (err as Error).name === 'AbortError'
      if (err instanceof ApiError && err.status === 409) {
        // the previous turn is still answering: this message was not stored, give the text back
        setTurns((ts) => ts.filter((t) => t.id !== q.id).map((t) => (t.id === a.id ? { ...t, status: 'error', error: err.message } : t)))
        setDraft(msg)
      } else {
        patch(a.id, (t) => ({ ...t, status: aborted ? 'done' : 'error', error: aborted ? undefined : `The assistant is unavailable: ${(err as Error).message}` }))
      }
    }
    refreshThreads(search || undefined)
  }

  async function decide(turnId: string, decision: 'approve' | 'reject', reason?: string) {
    if (!active) return
    patch(turnId, (t) => ({ ...t, approval: t.approval && { ...t.approval, state: 'submitting', error: undefined } }))
    const a: Turn = { id: uid(), role: 'assistant', text: '', tools: [], status: 'streaming' }
    setTurns((ts) => [...ts, a])
    try {
      await api.resume(active, decision, reason, (e) => {
        if (e.type === 'decision') patch(turnId, (t) => ({ ...t, approval: t.approval && { ...t.approval, state: e.status, decidedBy: e.decided_by } }))
        else patch(a.id, (t) => apply(t, e))
      })
      patch(a.id, (t) => (t.status === 'streaming' ? { ...t, status: 'done' } : t))
      patch(turnId, (t) => (t.approval?.state === 'submitting' ? { ...t, approval: { ...t.approval, state: 'pending' } } : t))
    } catch (err) {
      setTurns((ts) => ts.filter((t) => t.id !== a.id))
      patch(turnId, (t) => ({ ...t, approval: t.approval && { ...t.approval, state: 'error', error: (err as Error).message } }))
    }
    refreshThreads(search || undefined)
  }

  // one feedback request at a time per turn: a re-rate must see the assessment id the previous one stored
  const fbQueue = useRef<Record<string, Promise<void>>>({})
  function feedback(turnId: string, rating: 'up' | 'down', comment?: string) {
    const t = turns.find((x) => x.id === turnId)
    if (!active || !t?.traceId) return
    const thread = active, traceId = t.traceId
    patch(turnId, (x) => ({ ...x, feedback: { rating, comment }, feedbackError: undefined, feedbackSaving: true }))
    const run = async () => {
      try {
        await api.feedback(thread, traceId, rating, comment)
        patch(turnId, (x) => ({ ...x, feedbackSaving: false }))
      } catch (err) {
        patch(turnId, (x) => ({ ...x, feedbackSaving: false, feedbackError: (err as Error).message }))
      }
    }
    fbQueue.current[turnId] = (fbQueue.current[turnId] || Promise.resolve()).then(run)
  }

  async function rename(t: Thread) {
    const title = window.prompt('Rename conversation', t.title)?.trim()
    if (!title || title === t.title) return
    await threadsApi.rename(t.thread_id, title).catch(() => null)
    refreshThreads(search || undefined)
  }

  async function remove(t: Thread) {
    if (!window.confirm(`Delete "${t.title}"?`)) return
    await threadsApi.remove(t.thread_id).catch(() => null)
    if (active === t.thread_id) setActive(null)
    refreshThreads(search || undefined)
  }

  return (
    <div className="h-[calc(100vh-3.5rem)] flex">
      <aside className="w-72 shrink-0 border-r border-line/70 bg-white flex flex-col" aria-label="Conversations">
        <div className="p-4 space-y-3 border-b border-line/60">
          <Button variant="primary" className="w-full" onClick={() => newChat()}><Icon name="plus" />New conversation</Button>
          <label className="block">
            <span className="sr-only">Search conversations</span>
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search conversations"
              className="w-full border border-line rounded-md px-3 min-h-[40px] bg-oat-light focus:bg-white text-sm" />
          </label>
        </div>
        <nav className="flex-1 overflow-y-auto p-2">
          {threadsError ? <p role="alert" className="text-sm text-rose-900 p-2">Couldn't load conversations: {threadsError}</p>
            : !threads ? <div className="p-2"><Skeleton rows={5} /></div>
              : !threads.length ? <p className="text-sm text-ink-700 p-2">{search ? 'No conversations match.' : 'No conversations yet.'}</p>
                : (
                  <ul className="space-y-0.5">
                    {threads.map((t) => {
                      const on = t.thread_id === active
                      return (
                        <li key={t.thread_id} className="group relative">
                          <button onClick={() => open(t.thread_id)} aria-current={on ? 'true' : undefined}
                            className={`w-full text-left rounded-md px-3 py-2.5 pr-16 min-h-[44px] transition-colors duration-150 ease-quart ${on ? 'bg-oat-medium' : 'hover:bg-oat-light'}`}>
                            <div className="text-sm text-navy-900 truncate">{t.title}</div>
                            <div className="text-xs text-ink-700 flex items-center gap-1.5">
                              {fmt.ago(t.updated_at)}
                              {t.pending_approval && <span className="inline-flex items-center gap-1 text-warn-800"><span className="w-1.5 h-1.5 rounded-full bg-warn" />awaiting approval</span>}
                            </div>
                          </button>
                          <div className="absolute right-1.5 top-1.5 hidden group-hover:flex group-focus-within:flex gap-0.5">
                            <button onClick={() => rename(t)} className="p-2 rounded hover:bg-white text-ink-700" aria-label={`Rename ${t.title}`}><Icon name="edit" className="w-3.5 h-3.5" /></button>
                            <button onClick={() => remove(t)} className="p-2 rounded hover:bg-white text-ink-700" aria-label={`Delete ${t.title}`}><Icon name="trash" className="w-3.5 h-3.5" /></button>
                          </div>
                        </li>
                      )
                    })}
                  </ul>
                )}
        </nav>
      </aside>

      <div className="flex-1 min-w-0 flex flex-col">
        <div className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-6 py-8 space-y-8">
            {loadingThread ? <div className="flex items-center gap-2 text-ink-700 text-sm"><Spinner />Loading conversation…</div>
              : !turns.length ? <Welcome onPick={(p) => send(p)} />
                : turns.map((t) => <Message key={t.id} t={t} canApprove={me.can_approve} onDecide={(d, r) => decide(t.id, d, r)} onFeedback={(r, c) => feedback(t.id, r, c)}
                  viewer={{ email: me.email, selfApproval: !!me.self_approval }} />)}
            <div ref={end} />
          </div>
        </div>
        <div className="border-t border-line/70 bg-white">
          <form className="max-w-3xl mx-auto px-6 py-4 flex items-end gap-3" onSubmit={(e) => { e.preventDefault(); send(draft) }}>
            <label className="sr-only" htmlFor="composer">Message</label>
            <textarea id="composer" value={draft} onChange={(e) => setDraft(e.target.value)} rows={1} maxLength={4000}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(draft) } }}
              placeholder="Ask about stock cover, replenishment or promotions…"
              className="flex-1 resize-none border border-line rounded-card px-4 py-2.5 min-h-[44px] max-h-40 bg-oat-light focus:bg-white" />
            <Button type="submit" variant="primary" disabled={busy || !draft.trim()} aria-label="Send"><Icon name="send" />Send</Button>
          </form>
        </div>
      </div>
    </div>
  )
}

function Welcome({ onPick }: { onPick: (p: string) => void }) {
  const demo = useDemo()
  return (
    <div className="pt-6">
      <h1 className="text-2xl font-medium">What do you need to decide today?</h1>
      <p className="text-ink-700 mt-2 max-w-xl">
        I check stock cover, size replenishment against the policy, read promo briefs and field reports, and remember the decisions your team makes. Nothing ships until an approver decides.
      </p>
      <div className="mt-8 grid gap-3 sm:grid-cols-2">
        {demo.stories.flatMap((st) => st.prompts.map((prompt) => ({ story: st.title, prompt }))).map((s) => (
          <button key={s.prompt} onClick={() => onPick(s.prompt)}
            className="text-left flex flex-col justify-start rounded-card border border-line bg-white p-4 min-h-[44px] hover:bg-oat-medium/50 transition-colors duration-150 ease-quart">
            <div className="text-xs text-ink-700">{s.story}</div>
            <div className="mt-1 text-navy-900">{s.prompt}</div>
          </button>
        ))}
      </div>
    </div>
  )
}
