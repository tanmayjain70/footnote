import { useRef } from 'react'
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  KeyboardEvent,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'
import { count, humanise } from '../lib/format'

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={cx('rounded-xl border border-slate-200 bg-white shadow-xs', className)}
    >
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-3.5">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-slate-900">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  )
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
}

export function Button({ variant = 'secondary', size = 'md', className, ...props }: ButtonProps) {
  const base =
    'inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition ' +
    'disabled:cursor-not-allowed disabled:opacity-50'
  const sizes = { sm: 'px-2.5 py-1.5 text-xs', md: 'px-3.5 py-2 text-sm' }
  const variants = {
    primary: 'bg-brand-600 text-white hover:bg-brand-700',
    secondary: 'border border-slate-300 bg-white text-slate-700 hover:bg-slate-50',
    ghost: 'text-slate-600 hover:bg-slate-100',
    danger: 'border border-bad-600 text-bad-700 hover:bg-bad-50',
  }
  return <button className={cx(base, sizes[size], variants[variant], className)} {...props} />
}

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cx(
        'w-full rounded-lg border border-slate-300 px-3 py-2 text-sm placeholder:text-slate-400',
        'focus:border-brand-500 focus:ring-2 focus:ring-brand-100 focus:outline-none',
        className,
      )}
      {...props}
    />
  )
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cx(
        'w-full rounded-lg border border-slate-300 px-3 py-2 text-sm leading-relaxed placeholder:text-slate-400',
        'focus:border-brand-500 focus:ring-2 focus:ring-brand-100 focus:outline-none',
        className,
      )}
      {...props}
    />
  )
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cx(
        'rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-sm text-slate-700',
        'focus:border-brand-500 focus:ring-2 focus:ring-brand-100 focus:outline-none',
        className,
      )}
      {...props}
    />
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  )
}

const TONES = {
  slate: 'bg-slate-100 text-slate-700',
  green: 'bg-good-100 text-good-800',
  amber: 'bg-note-100 text-note-800',
  red: 'bg-bad-100 text-bad-700',
  blue: 'bg-brand-100 text-brand-900',
}

export type BadgeTone = keyof typeof TONES

export function Badge({
  tone = 'slate',
  title,
  children,
}: {
  tone?: BadgeTone
  title?: string
  children: ReactNode
}) {
  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center rounded-md px-1.5 py-0.5 text-xs font-medium',
        TONES[tone],
      )}
    >
      {children}
    </span>
  )
}

/*
 * One pill for every status the API reports -- documents, jobs, extractions,
 * questions, review states and confidence -- so the same word is always the
 * same colour wherever it appears. Anything unknown falls back to grey rather
 * than breaking, because a new status on the API side should not take the
 * page down.
 */
const STATUS_TONES: Record<string, BadgeTone> = {
  // documents and jobs
  queued: 'slate',
  processing: 'amber',
  running: 'amber',
  ready: 'green',
  done: 'green',
  failed: 'red',
  // extraction on a document that has never been extracted
  none: 'slate',
  // questions
  answered: 'green',
  unanswered: 'amber',
  budget_exhausted: 'red',
  // review
  pending: 'amber',
  confirmed: 'green',
  corrected: 'blue',
  rejected: 'slate',
  // confidence
  high: 'green',
  medium: 'amber',
  low: 'red',
}

const STATUS_LIVE = new Set(['queued', 'processing', 'running'])

export function StatusPill({
  status,
  label,
  title,
}: {
  status: string | null | undefined
  /** Overrides the humanised status code. */
  label?: ReactNode
  title?: string
}) {
  if (!status) return <Badge tone="slate">—</Badge>
  return (
    <Badge tone={STATUS_TONES[status] ?? 'slate'} title={title}>
      {STATUS_LIVE.has(status) && (
        <span
          className={cx(
            'mr-1.5 inline-block h-1.5 w-1.5 rounded-full',
            status === 'queued' ? 'bg-slate-400' : 'animate-pulse bg-note-600',
          )}
          aria-hidden
        />
      )}
      {label ?? humanise(status)}
    </Badge>
  )
}

export function Stat({
  label,
  value,
  hint,
  tone = 'slate',
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'slate' | 'bad' | 'note' | 'good'
}) {
  const colours = {
    slate: 'text-slate-900',
    bad: 'text-bad-700',
    note: 'text-note-700',
    good: 'text-good-700',
  }
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3.5 shadow-xs">
      <p className="text-xs font-medium text-slate-500">{label}</p>
      <p className={cx('tabular mt-1 text-2xl font-semibold', colours[tone])}>{value}</p>
      {hint && <p className="mt-0.5 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-700">{title}</p>
      {children && <div className="mt-1 text-xs text-slate-500">{children}</div>}
    </div>
  )
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-sm text-slate-500">
      <span
        className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-brand-600"
        aria-hidden
      />
      {label}…
    </div>
  )
}

const NOTICE_TONES = {
  note: 'border-note-200 bg-note-50 text-note-800',
  bad: 'border-bad-100 bg-bad-50 text-bad-800',
  good: 'border-good-100 bg-good-50 text-good-800',
  brand: 'border-brand-100 bg-brand-50 text-brand-900',
  slate: 'border-slate-200 bg-slate-100 text-slate-700',
}

/**
 * A coloured card with a message: amber for "the documents could not answer
 * this" or "needs review", red for a failure or an exhausted budget.
 */
export function Notice({
  tone = 'note',
  title,
  children,
  className,
}: {
  tone?: keyof typeof NOTICE_TONES
  title?: ReactNode
  children?: ReactNode
  className?: string
}) {
  return (
    <div className={cx('rounded-lg border px-4 py-3 text-sm', NOTICE_TONES[tone], className)}>
      {title && <p className="font-medium">{title}</p>}
      {children && <div className={title ? 'mt-1 text-xs' : undefined}>{children}</div>}
    </div>
  )
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-bad-100 bg-bad-50 px-3.5 py-2.5 text-sm text-bad-700">
      {children}
    </div>
  )
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="-mx-5 overflow-x-auto">
      <table className="w-full min-w-full text-sm">{children}</table>
    </div>
  )
}

export function Th({
  children,
  right,
  className,
}: {
  children?: ReactNode
  right?: boolean
  className?: string
}) {
  return (
    <th
      className={cx(
        'border-b border-slate-200 px-5 py-2 text-xs font-medium tracking-wide text-slate-500 uppercase',
        right ? 'text-right' : 'text-left',
        className,
      )}
    >
      {children}
    </th>
  )
}

export function Td({
  children,
  right,
  className,
  colSpan,
}: {
  children?: ReactNode
  right?: boolean
  className?: string
  colSpan?: number
}) {
  return (
    <td
      colSpan={colSpan}
      className={cx(
        'border-b border-slate-100 px-5 py-2.5 text-slate-700',
        right && 'tabular text-right',
        className,
      )}
    >
      {children}
    </td>
  )
}

/** Offset pagination for a `Page<T>`. Renders nothing when everything fits on one page. */
export function Pager({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number
  limit: number
  offset: number
  onChange: (offset: number) => void
}) {
  if (total <= limit) return null
  const from = offset + 1
  const to = Math.min(offset + limit, total)
  return (
    <div className="flex items-center justify-between gap-3 pt-3 text-xs text-slate-500">
      <span className="tabular">
        {count(from)}–{count(to)} of {count(total)}
      </span>
      <div className="flex gap-2">
        <Button size="sm" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          Previous
        </Button>
        <Button size="sm" disabled={to >= total} onClick={() => onChange(offset + limit)}>
          Next
        </Button>
      </div>
    </div>
  )
}

export interface TabItem {
  id: string
  label: ReactNode
  /** Shown as a small count next to the label when present. */
  count?: number | null
}

/**
 * A controlled tab strip following the WAI-ARIA tabs pattern: one tab stop,
 * arrow keys move between tabs, Home and End jump to the ends. `name` ties
 * the strip to its `TabPanel`s so assistive technology can pair them, and
 * keeps two strips on one page from sharing ids.
 */
export function Tabs({
  name,
  tabs,
  value,
  onChange,
  label,
}: {
  name: string
  tabs: TabItem[]
  value: string
  onChange: (id: string) => void
  label?: string
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = Math.max(
      0,
      tabs.findIndex((tab) => tab.id === value),
    )
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault()
    onChange(tabs[next].id)
    refs.current[next]?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className="flex gap-1 border-b border-slate-200"
    >
      {tabs.map((tab, index) => {
        const active = tab.id === value
        return (
          <button
            key={tab.id}
            ref={(el) => {
              refs.current[index] = el
            }}
            type="button"
            role="tab"
            id={`${name}-tab-${tab.id}`}
            aria-selected={active}
            aria-controls={`${name}-panel-${tab.id}`}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className={cx(
              '-mb-px inline-flex items-center border-b-2 px-3 py-2 text-sm font-medium transition',
              active
                ? 'border-brand-600 text-brand-700'
                : 'border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800',
            )}
          >
            {tab.label}
            {tab.count !== undefined && tab.count !== null && (
              <span
                className={cx(
                  'tabular ml-1.5 rounded-md px-1.5 py-0.5 text-xs',
                  active ? 'bg-brand-50 text-brand-700' : 'bg-slate-100 text-slate-600',
                )}
              >
                {count(tab.count)}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

/** The content for one tab. Unmounted when inactive, so its queries do not run. */
export function TabPanel({
  name,
  id,
  active,
  children,
  className,
}: {
  name: string
  id: string
  active: boolean
  children: ReactNode
  className?: string
}) {
  if (!active) return null
  return (
    <div
      role="tabpanel"
      id={`${name}-panel-${id}`}
      aria-labelledby={`${name}-tab-${id}`}
      tabIndex={0}
      className={className}
    >
      {children}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded border border-slate-300 bg-slate-50 px-1.5 py-0.5 font-mono text-[11px] text-slate-600 shadow-[inset_0_-1px_0_theme(colors.slate.300)]">
      {children}
    </kbd>
  )
}

/**
 * The superscript [n] after a cited sentence. A button, not a link: it selects
 * the passage on the same screen rather than navigating away from the answer.
 */
export function CitationMarker({
  ordinal,
  active = false,
  title,
  onClick,
  className,
}: {
  ordinal: number
  active?: boolean
  /** Usually the document title and page, for the tooltip and screen readers. */
  title?: string
  onClick?: () => void
  className?: string
}) {
  return (
    <sup className={cx('ml-px inline-block leading-none', className)}>
      <button
        type="button"
        onClick={onClick}
        title={title}
        aria-label={title ? `Citation ${ordinal}: ${title}` : `Citation ${ordinal}`}
        aria-pressed={active}
        className={cx(
          'tabular rounded px-0.5 text-[0.72em] font-semibold transition',
          active ? 'bg-brand-600 text-white' : 'text-brand-700 hover:bg-brand-100',
        )}
      >
        [{ordinal}]
      </button>
    </sup>
  )
}
