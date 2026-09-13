import { useState } from 'react'
import { ApiError } from '../lib/api'
import { useAuth } from '../lib/auth'
import { Button, ErrorNote, Field, Input } from '../components/ui'

const DEMO_ACCOUNTS = [
  [
    'director@hallampryce.demo',
    'Director',
    'Every portfolio, plus the evals and the model budget',
  ],
  [
    'admin@hallampryce.demo',
    'Lease administrator',
    'Uploads, runs extraction and reviews values across all three portfolios',
  ],
  [
    'manager@hallampryce.demo',
    'Property manager',
    'Asks questions over City Centre and Northern Estates; cannot see Riverside',
  ],
  [
    'riverside@hallampryce.demo',
    'Property manager',
    'Riverside only — the confidential portfolio that is being sold',
  ],
  [
    'finance@hallampryce.demo',
    'Finance',
    'Reads the register and the documents, and cannot ask questions',
  ],
] as const

export function Login() {
  const { signIn } = useAuth()
  const [email, setEmail] = useState('director@hallampryce.demo')
  const [password, setPassword] = useState('demo-password')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [slow, setSlow] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const timer = setTimeout(() => setSlow(true), 4000)
    try {
      await signIn(email, password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not sign in.')
    } finally {
      clearTimeout(timer)
      setSlow(false)
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto grid min-h-screen max-w-5xl items-center gap-10 px-6 py-12 lg:grid-cols-2">
      <div>
        <div className="flex items-center gap-2.5">
          <span className="grid h-8 w-8 place-items-center rounded-md bg-brand-600 text-xs font-bold text-white">
            Fn
          </span>
          <span className="text-lg font-semibold">Footnote</span>
        </div>
        <h1 className="mt-6 text-2xl font-semibold text-slate-900">
          It points at the page. Or it says it can't.
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-slate-600">
          A Manchester property firm was set up with a chat-with-your-PDFs tool. It told a lease
          administrator that a break notice was three months. It was six. Notice went out late,
          and the tenant is now locked in for another five years. The brief that followed was
          short: do not be clever. Point at the page, and say "I don't know" when the documents
          do not say.
        </p>
        <p className="mt-4 text-xs text-slate-500">
          Every answer here cites the page it came from, and each citation is checked against the
          passage the model was actually shown. Key terms are pulled into a register with a quote
          and a page number, and a person confirms each value before it counts.
        </p>

        <div className="mt-8 rounded-xl border border-slate-200 bg-white p-4">
          <p className="text-xs font-medium text-slate-600">
            Five demo accounts — password <code className="text-slate-900">demo-password</code>
          </p>
          <ul className="mt-2.5 space-y-1.5">
            {DEMO_ACCOUNTS.map(([account, role, note]) => (
              <li key={account} className="flex flex-wrap items-baseline gap-x-2 text-xs">
                <button
                  type="button"
                  className="font-medium text-brand-700 hover:underline"
                  onClick={() => {
                    setEmail(account)
                    setPassword('demo-password')
                  }}
                >
                  {account}
                </button>
                <span className="text-slate-500">{role}</span>
                <span className="text-slate-400">— {note}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <form onSubmit={submit} className="rounded-xl border border-slate-200 bg-white p-6 shadow-xs">
        <h2 className="text-sm font-semibold text-slate-900">Sign in</h2>
        <div className="mt-4 space-y-4">
          <Field label="Email">
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              required
            />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>
          {error && <ErrorNote>{error}</ErrorNote>}
          <Button type="submit" variant="primary" className="w-full" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>
          {slow && (
            <p className="text-xs text-slate-500">
              The demo server sleeps when nobody is using it. The first sign-in can take up to a
              minute while it wakes.
            </p>
          )}
        </div>
      </form>
    </div>
  )
}
