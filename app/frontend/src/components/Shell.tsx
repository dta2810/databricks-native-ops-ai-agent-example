import React from 'react'
import { Me, useReference } from '../api'
import { useDemo } from '../demo_config'
import { Icon, Pill } from './ui'

export const NAV = [
  { id: 'overview', label: 'Overview' },
  { id: 'markets', label: 'Markets' },
  { id: 'requests', label: 'Requests' },
  { id: 'assistant', label: 'Assistant' },
]

export default function Shell({ me, page, children }: { me: Me; page: string; children: React.ReactNode }) {
  const ref = useReference()
  const demo = useDemo()
  return (
    <div className="min-h-screen flex bg-oat-light text-navy-900">
      <aside className="w-60 shrink-0 bg-navy-900 text-white/80 flex flex-col sticky top-0 h-screen">
        <div className="px-5 pt-6 pb-5">
          <div className="flex items-center gap-2.5">
            <span className="w-7 h-7 rounded-md bg-white/10 grid place-items-center" aria-hidden>
              <span className="w-2.5 h-2.5 rounded-full bg-lava-600" />
            </span>
            <div>
              <div className="text-white font-medium leading-tight">{demo.company}</div>
              <div className="text-xs text-white/60">{demo.product}</div>
            </div>
          </div>
        </div>
        <nav className="flex-1 px-3 space-y-0.5" aria-label="Main">
          {NAV.map((n) => {
            const active = page === n.id
            return (
              <a key={n.id} href={`#/${n.id}`} aria-current={active ? 'page' : undefined}
                className={`flex items-center gap-3 px-3 min-h-[44px] rounded-md text-sm transition-colors duration-150 ease-quart ${active ? 'bg-white/10 text-white font-medium' : 'hover:bg-white/5 hover:text-white'}`}>
                <Icon name={n.id} />{n.label}
              </a>
            )
          })}
        </nav>
        <div className="p-5 text-xs border-t border-white/10 space-y-2">
          <div className="text-white/50">Signed in as</div>
          <div className="text-white break-all">{me.email}</div>
          <div className="flex flex-wrap gap-1.5 pt-1">
            {me.roles.map((r) => <span key={r.role} className="px-2 py-0.5 rounded-full bg-white/10 text-white/80">{r.label}</span>)}
          </div>
        </div>
      </aside>

      <div className="flex-1 min-w-0 flex flex-col">
        <header className="h-14 bg-white border-b border-line/70 px-6 flex items-center gap-3 sticky top-0 z-10">
          <Pill tone="neutral">Demo · synthetic data</Pill>
          {me.agent_mode === 'stub' && <Pill tone="warn">Preview: scripted agent, real data</Pill>}
          {me.local_dev && <Pill tone="info">Local dev</Pill>}
          {me.self_approval && <Pill tone="warn">Single-presenter mode: self-approval on, four-eyes off</Pill>}
          {ref && <span className="ml-auto text-xs text-ink-700">{ref.markets.length} markets · {ref.distributors.length} distributors · {demo.currency}</span>}
        </header>
        <main className="flex-1 min-h-0">{children}</main>
      </div>
    </div>
  )
}
