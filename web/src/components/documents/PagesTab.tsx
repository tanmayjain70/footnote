import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../lib/api'
import type { DocumentOut, DocumentPageOut } from '../../lib/types'
import { Button, Empty, Input, Notice, Spinner, cx } from '../ui'
import { findRange, highlightPattern, isLive } from './values'

const MARK_ID = 'page-highlight'

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value))
}

/**
 * A page's text as paragraphs, with one character range wrapped in a <mark>.
 * The range is measured on the whole page text because a quoted sentence can
 * run across a line break; the offsets are walked paragraph by paragraph so
 * the mark simply continues into the next one.
 */
function PageText({ text, range }: { text: string; range: [number, number] | null }) {
  const paragraphs = text.split('\n')
  let offset = 0
  let marked = false
  return (
    <>
      {paragraphs.map((paragraph, index) => {
        const start = offset
        const end = start + paragraph.length
        offset = end + 1
        if (!paragraph.trim()) return null
        if (!range || range[1] <= start || range[0] >= end) {
          return (
            <p key={index} className="text-sm leading-relaxed text-slate-800">
              {paragraph}
            </p>
          )
        }
        const from = Math.max(range[0], start) - start
        const to = Math.min(range[1], end) - start
        const id = marked ? undefined : MARK_ID
        marked = true
        return (
          <p key={index} className="text-sm leading-relaxed text-slate-800">
            {paragraph.slice(0, from)}
            <mark id={id}>{paragraph.slice(from, to)}</mark>
            {paragraph.slice(to)}
          </p>
        )
      })}
    </>
  )
}

/**
 * Every page of the document in reading order, with a navigator that jumps
 * between them. `?page=N&highlight=<text>` -- the link a citation or an
 * extracted value hands out -- scrolls to that page and marks the first
 * occurrence of the text, which is how someone checks that an answer really
 * says what it claims.
 */
export function PagesTab({ document: doc }: { document: DocumentOut }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const requested = searchParams.has('page')
  const highlight = searchParams.get('highlight')

  const pages = useQuery({
    queryKey: ['document-pages', doc.id],
    queryFn: () => api.get<DocumentPageOut[]>(`/documents/${doc.id}/pages`),
    enabled: doc.status === 'ready',
  })

  const total = pages.data?.length ?? 0
  const current = total ? clamp(Number(searchParams.get('page')) || 1, 1, total) : 1
  const [jump, setJump] = useState(String(current))

  useEffect(() => {
    setJump(String(current))
  }, [current])

  const pattern = useMemo(() => highlightPattern(highlight), [highlight])

  // Where the highlight actually is. Normally on the requested page; when it
  // is not, look elsewhere, because a value the model attributed to the wrong
  // page is exactly the kind of thing a reviewer needs to notice.
  const located = useMemo(() => {
    if (!pattern || !pages.data) return { range: null, elsewhere: null }
    const page = pages.data.find((p) => p.page_number === current)
    const range = page ? findRange(page.text, pattern) : null
    if (range) return { range, elsewhere: null }
    const other = pages.data.find((p) => p.page_number !== current && pattern.test(p.text))
    return { range: null, elsewhere: other?.page_number ?? null }
  }, [pattern, pages.data, current])

  useEffect(() => {
    if (!requested || !pages.data?.length) return
    const frame = requestAnimationFrame(() => {
      const mark = located.range ? window.document.getElementById(MARK_ID) : null
      const target = mark ?? window.document.getElementById(`page-${current}`)
      target?.scrollIntoView({ block: mark ? 'center' : 'start', behavior: 'smooth' })
    })
    return () => cancelAnimationFrame(frame)
  }, [requested, pages.data, current, located.range])

  function go(page: number, keepHighlight = false) {
    const next: Record<string, string> = { page: String(clamp(page, 1, total)) }
    if (keepHighlight && highlight) next.highlight = highlight
    setSearchParams(next, { replace: true })
  }

  function onJump(event: FormEvent) {
    event.preventDefault()
    const page = Number(jump)
    if (Number.isInteger(page) && page >= 1 && page <= total) go(page)
    else setJump(String(current))
  }

  if (doc.status !== 'ready') {
    if (doc.status === 'failed') {
      return <Empty title="No pages">This document could not be processed.</Empty>
    }
    return (
      <Notice tone="slate">
        Pages appear once the document has been processed.
        {isLive(doc.status) && ' This screen checks every few seconds.'}
      </Notice>
    )
  }
  if (pages.isLoading) return <Spinner label="Loading pages" />
  if (pages.error) return <Notice tone="bad">The pages could not be loaded.</Notice>
  if (!pages.data?.length) return <Empty title="No pages">The document has no readable text.</Empty>

  return (
    <div className="space-y-4">
      <div className="sticky top-0 z-10 -mx-1 flex flex-wrap items-center gap-3 border-b border-slate-200 bg-slate-50/95 px-1 py-2 backdrop-blur">
        <Button size="sm" disabled={current <= 1} onClick={() => go(current - 1)}>
          Previous
        </Button>
        <form onSubmit={onJump} className="flex items-center gap-1.5 text-sm text-slate-600">
          Page
          <Input
            type="number"
            min={1}
            max={total}
            value={jump}
            onChange={(event) => setJump(event.target.value)}
            onBlur={onJump}
            aria-label="Page number"
            className="tabular w-16 px-2 py-1 text-center"
          />
          <span className="tabular">of {total}</span>
        </form>
        <Button size="sm" disabled={current >= total} onClick={() => go(current + 1)}>
          Next
        </Button>
        {highlight && (
          <span className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span>
              Highlighting{' '}
              <span className="text-slate-700">
                “{highlight.length > 70 ? `${highlight.slice(0, 70)}…` : highlight}”
              </span>
            </span>
            {!located.range && located.elsewhere !== null && (
              <button
                type="button"
                className="font-medium text-brand-700 hover:underline"
                onClick={() => go(located.elsewhere ?? current, true)}
              >
                Not on this page — found on page {located.elsewhere}
              </button>
            )}
            {!located.range && located.elsewhere === null && (
              <span className="text-note-800">Not found in this document’s text</span>
            )}
            <button
              type="button"
              className="text-slate-500 hover:text-slate-800 hover:underline"
              onClick={() => go(current)}
            >
              Clear
            </button>
          </span>
        )}
      </div>

      <div className="space-y-4">
        {pages.data.map((page) => (
          <article
            key={page.page_number}
            id={`page-${page.page_number}`}
            className={cx(
              'scroll-mt-16 rounded-xl border bg-white px-6 py-5 shadow-xs',
              page.page_number === current ? 'border-brand-200' : 'border-slate-200',
            )}
          >
            <h3 className="mb-3 text-xs font-medium tracking-wide text-slate-500 uppercase">
              Page {page.page_number}
            </h3>
            <div className="max-w-3xl space-y-3">
              {page.text.trim() ? (
                <PageText
                  text={page.text}
                  range={page.page_number === current ? located.range : null}
                />
              ) : (
                <p className="text-sm text-slate-400">No text on this page.</p>
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  )
}
