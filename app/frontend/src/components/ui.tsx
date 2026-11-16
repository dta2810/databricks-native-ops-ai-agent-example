import React from 'react'

// status → classes; full borders + tints only (no side-stripe borders)
export type Tone = 'ok' | 'warn' | 'critical' | 'info' | 'neutral'
const TONE: Record<Tone, string> = {
  ok: 'bg-ok-50 text-ok-800 border-ok/30',
  warn: 'bg-warn-50 text-warn-800 border-warn/40',
  critical: 'bg-rose-50 text-rose-900 border-rose-900/20',
  info: 'bg-info-50 text-info-800 border-info/30',
  neutral: 'bg-oat-medium text-ink-700 border-line',
}

export function Pill({ tone, children, className = '' }: { tone: Tone; children: React.ReactNode; className?: string }) {
  return <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs font-medium whitespace-nowrap ${TONE[tone]} ${className}`}>{children}</span>
}

export function Section({ title, subtitle, actions, children, className = '' }: {
  title: string; subtitle?: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode; className?: string
}) {
  return (
    <section className={`bg-white rounded-card border border-line/70 p-6 ${className}`}>
      <header className="flex items-start justify-between gap-4 mb-5 flex-wrap">
        <div>
          <h2 className="text-lg font-medium text-navy-900">{title}</h2>
          {subtitle && <p className="text-sm text-ink-700 mt-1">{subtitle}</p>}
        </div>
        {actions}
      </header>
      {children}
    </section>
  )
}

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-3 animate-pulse" aria-label="Loading" role="status">
      {Array.from({ length: rows }).map((_, i) => <div key={i} className="h-5 bg-oat-medium rounded" style={{ width: `${90 - i * 12}%` }} />)}
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-rose-900/20 bg-rose-50 px-4 py-3 text-sm text-rose-900 flex items-center justify-between gap-4">
      <span>Couldn't load this: {message}</span>
      {onRetry && <button onClick={onRetry} className="font-medium underline underline-offset-2 min-h-[44px] px-2">Try again</button>}
    </div>
  )
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line px-6 py-10 text-center">
      <p className="text-base font-medium text-navy-900">{title}</p>
      {children && <div className="text-sm text-ink-700 mt-1">{children}</div>}
    </div>
  )
}

export function Button({ variant = 'secondary', className = '', ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' }) {
  const v = {
    primary: 'bg-navy-900 text-white hover:bg-navy-800 disabled:bg-ink-500',
    secondary: 'bg-white text-navy-900 border border-line hover:bg-oat-light disabled:text-ink-500',
    ghost: 'text-navy-900 hover:bg-oat-medium disabled:text-ink-500',
  }[variant]
  return <button {...p} className={`inline-flex items-center justify-center gap-2 rounded-md px-4 min-h-[40px] text-sm font-medium transition-colors duration-150 ease-quart disabled:cursor-not-allowed ${v} ${className}`} />
}

export function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[]
}) {
  return (
    <label className="inline-flex items-center gap-2 text-sm text-ink-700">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="border border-line rounded-md px-2 min-h-[40px] bg-white text-navy-900">
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  )
}

const ICONS: Record<string, string> = {
  assistant: 'M4 5h16v11H8l-4 4V5z',
  overview: 'M4 13h7V4H4v9zm0 7h7v-5H4v5zm9 0h7v-9h-7v9zm0-16v5h7V4h-7z',
  requests: 'M9 5h10M9 12h10M9 19h10M4 5h.01M4 12h.01M4 19h.01',
  genie: 'M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z',
  uc_function: 'M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16',
  documents: 'M7 3h7l5 5v13H7V3zm7 0v5h5',
  memory: 'M12 8v4l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z',
  request: 'M5 12l4 4L19 6',
  send: 'M4 12l16-8-6 16-2-7-8-1z',
  chevron: 'M9 6l6 6-6 6',
  check: 'M5 12l4 4L19 6',
  x: 'M6 6l12 12M18 6L6 18',
  alert: 'M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
  refresh: 'M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5',
  plus: 'M12 5v14M5 12h14',
  edit: 'M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4',
  trash: 'M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  markets: 'M3 21h18M5 21V9l7-5 7 5v12M9 21v-6h6v6',
  thumbUp: 'M7 11v9H4v-9h3zm0 0l4-7a2 2 0 012 2v4h5a2 2 0 012 2.3l-1.2 6A2 2 0 0116.8 20H7',
  thumbDown: 'M17 13V4h3v9h-3zm0 0l-4 7a2 2 0 01-2-2v-4H6a2 2 0 01-2-2.3l1.2-6A2 2 0 017.2 4H17',
}

export function Icon({ name, className = 'w-4 h-4' }: { name: string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      <path d={ICONS[name] || ICONS.chevron} />
    </svg>
  )
}

export function Spinner({ className = 'w-3.5 h-3.5' }: { className?: string }) {
  return <span className={`inline-block rounded-full border-2 border-current border-t-transparent animate-spin ${className}`} aria-hidden />
}

export function coverTone(cover: number | null | undefined, threshold = 7): Tone {
  if (cover == null) return 'neutral'
  return cover < threshold / 2 ? 'critical' : cover < threshold ? 'warn' : 'ok'
}
