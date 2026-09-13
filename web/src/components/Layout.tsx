import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { count } from '../lib/format'
import type { Health, ReviewQueueItem } from '../lib/types'
import { Badge, Button, cx } from './ui'

interface NavItem {
  to: string
  label: string
  end?: boolean
  /** A second path prefix that should light this item up (stored answers live under /questions). */
  also?: string
}

export function Layout() {
  const { me, signOut } = useAuth()
  const { pathname } = useLocation()
  const permissions = me?.permissions

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.get<Health>('/health'),
    // Documents are ingested in the background, so the job counts in the
    // header move on their own.
    refetchInterval: 30_000,
  })

  const { data: queue } = useQuery({
    queryKey: ['review-queue'],
    queryFn: () => api.get<ReviewQueueItem[]>('/review-queue'),
    enabled: Boolean(permissions?.can_review),
    refetchInterval: 30_000,
  })
  const pending = queue?.reduce((sum, item) => sum + item.pending, 0) ?? 0

  const nav: NavItem[] = [
    { to: '/', label: 'Ask', end: true, also: '/questions' },
    { to: '/documents', label: 'Documents' },
    { to: '/register', label: 'Register' },
  ]
  if (permissions?.can_run_evals) nav.push({ to: '/evals', label: 'Evals' })
  if (permissions?.can_see_usage) nav.push({ to: '/usage', label: 'Usage' })

  const busy = health ? health.jobs.queued + health.jobs.running : 0
  const failed = health?.jobs.failed ?? 0

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <div className="flex items-center gap-2.5">
            <span className="grid h-7 w-7 place-items-center rounded-md bg-brand-600 text-xs font-bold text-white">
              Fn
            </span>
            <span className="text-sm font-semibold text-slate-900">Footnote</span>
            <span className="hidden text-xs text-slate-400 sm:inline">Hallam &amp; Pryce</span>
          </div>

          <nav className="flex items-center gap-1">
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cx(
                    'rounded-lg px-3 py-1.5 text-sm font-medium transition',
                    isActive || (item.also && pathname.startsWith(item.also))
                      ? 'bg-brand-50 text-brand-700'
                      : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900',
                  )
                }
              >
                {item.label}
                {item.to === '/register' && pending > 0 && (
                  <span
                    className="tabular ml-1.5 rounded-md bg-note-100 px-1.5 py-0.5 text-xs text-note-800"
                    title={`${count(pending)} extracted values waiting for review`}
                  >
                    {count(pending)}
                  </span>
                )}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-3">
            {health && (busy > 0 || failed > 0) && (
              <span className="hidden items-center gap-1.5 text-xs text-slate-500 lg:flex">
                {busy > 0 && (
                  <span className="tabular">
                    <span className="font-medium text-slate-700">{count(busy)}</span> processing
                  </span>
                )}
                {failed > 0 && (
                  <span className="tabular text-bad-700">
                    <span className="font-medium">{count(failed)}</span> failed
                  </span>
                )}
              </span>
            )}
            {me && (
              <span className="hidden items-center gap-2 text-xs text-slate-500 sm:flex">
                {me.user.full_name}
                <Badge tone="blue">{me.user.role}</Badge>
              </span>
            )}
            <Button size="sm" variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-6">
        <Outlet />
      </main>

      <footer className="mx-auto max-w-7xl px-6 pb-10 text-xs text-slate-400">
        <p>
          Answers are generated from your documents by{' '}
          <span className="text-slate-500">{health?.llm.model ?? 'the configured model'}</span>;
          every citation is checked against the passage the model was shown.
        </p>
        <p className="mt-1">A simulated engagement. No real client, no real company.</p>
      </footer>
    </div>
  )
}
