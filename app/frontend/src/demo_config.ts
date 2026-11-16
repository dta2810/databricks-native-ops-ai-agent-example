// Demo content is served by the API from config/demo_cases.yaml (BEV_DEMO_CASES) + server/prompts/ask_templates.yaml.
// Nothing in the UI hardcodes markets, distributors, SKUs or promo ids.
import { useEffect, useState } from 'react'
import { getJson } from './api'

export type DemoCases = {
  company: string; product: string; currency: string
  stories: { key: string; title: string; prompts: string[] }[]
  ask: Record<string, string>
}

const FALLBACK: DemoCases = { company: '', product: 'Beverage Ops Agent', currency: 'USD', stories: [], ask: {} }
let pending: Promise<DemoCases> | null = null

export function useDemo(): DemoCases {
  const [d, setD] = useState<DemoCases>(FALLBACK)
  useEffect(() => {
    pending = pending || getJson<DemoCases>('/demo-cases').catch(() => { pending = null; return FALLBACK })
    pending.then(setD)
  }, [])
  return d
}

// fill a {{var}} template from the row the user clicked; unknown templates fall back to a generic question
export function ask(d: DemoCases, key: string, vars: Record<string, string> = {}) {
  const t = d.ask[key]
  if (!t) return Object.values(vars).filter(Boolean).join(' ') || 'What needs attention today?'
  return t.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, k) => vars[k] ?? '')
}
