import { useEffect, useState } from 'react'
import { Me, chatApi, getJson, useRoute } from './api'
import Shell, { NAV } from './components/Shell'
import { ErrorState, Skeleton } from './components/ui'
import Assistant from './pages/Assistant'
import Markets from './pages/Markets'
import Overview from './pages/Overview'
import Requests from './pages/Requests'

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [page, go] = useRoute()
  // prompt handed from another page ("Ask the agent about this")
  const [seed, setSeed] = useState<string | null>(null)

  useEffect(() => {
    getJson<Me>('/me').then(setMe).catch((e) => setError(e.message))
  }, [])

  if (error) return <Centered><ErrorState message={error} onRetry={() => window.location.reload()} /></Centered>
  if (!me) return <Centered><div className="w-80"><Skeleton rows={3} /></div></Centered>
  if (!me.roles.length) return (
    <Centered>
      <div className="max-w-md text-center">
        <h1 className="text-xl font-medium">You don't have access yet</h1>
        <p className="text-ink-700 mt-2">Ask an administrator to add you to the supply planner or approver group.</p>
      </div>
    </Centered>
  )

  const current = NAV.some((n) => n.id === page) ? page : 'overview'
  const ask = (prompt: string) => { setSeed(prompt); go('assistant') }
  return (
    <Shell me={me} page={current}>
      {current === 'overview' ? <Overview onAsk={ask} />
        : current === 'markets' ? <Markets onAsk={ask} />
        : current === 'requests' ? <Requests onAsk={ask} me={me} />
          : <Assistant me={me} api={chatApi} seed={seed} onSeedUsed={() => setSeed(null)} />}
    </Shell>
  )
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen grid place-items-center bg-oat-light p-6">{children}</div>
}
