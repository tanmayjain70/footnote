import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { query } from '../../lib/api'
import { count } from '../../lib/format'
import type { SourceOut } from '../../lib/types'
import { Badge, Empty, cx } from '../ui'
import type { AnswerStatus, LiveCitation } from './answerState'

/*
 * Every passage the model was shown, in the order it saw them, whether or not
 * it used them. Showing the uncited ones too is the point: when the answer is
 * a refusal, these are the closest the documents came, and a person can read
 * them and decide whether the refusal was right.
 */

function ranks(source: SourceOut): string {
  const parts: string[] = []
  if (source.vector_rank !== null) parts.push(`meaning #${source.vector_rank}`)
  if (source.lexical_rank !== null) parts.push(`keyword #${source.lexical_rank}`)
  return parts.join(' · ')
}

function SourceCard({
  source,
  citation,
  active,
  expanded,
  onClick,
}: {
  source: SourceOut
  citation: LiveCitation | undefined
  active: boolean
  expanded: boolean
  onClick: () => void
}) {
  const ref = useRef<HTMLLIElement>(null)

  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [active])

  // The document page highlights the cited text; for an uncited passage there
  // is no cited text, so the link opens the page and leaves it at that.
  const href = `/documents/${source.document_id}${query({
    page: source.page_number,
    highlight: citation?.cited_text,
  })}`

  return (
    <li
      ref={ref}
      className={cx(
        'rounded-lg border p-3 text-xs transition',
        active
          ? 'border-brand-500 bg-brand-50/50 ring-2 ring-brand-100'
          : source.cited
            ? 'border-slate-200 bg-white hover:border-slate-300'
            : 'border-slate-200 bg-slate-50/70 hover:border-slate-300',
      )}
    >
      {/* A button rather than a clickable card, so the keyboard reaches it. */}
      <button
        type="button"
        onClick={onClick}
        aria-pressed={active}
        aria-expanded={expanded}
        className="block w-full rounded text-left"
      >
        <span className="flex items-baseline justify-between gap-2">
          <span className="truncate font-medium text-slate-800" title={source.document_title}>
            {source.document_title}
          </span>
          <span className="tabular shrink-0 text-slate-500">p. {source.page_number}</span>
        </span>
        <span
          className={cx(
            'mt-1 block leading-relaxed text-slate-600',
            !expanded && 'line-clamp-3',
          )}
        >
          {source.snippet}
        </span>
      </button>
      {active && citation && (
        <blockquote className="mt-2 border-l-2 border-note-600 bg-note-50 px-2 py-1.5 leading-relaxed text-slate-800">
          “{citation.cited_text}”
        </blockquote>
      )}
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          {citation ? (
            <Badge tone="green">Cited [{citation.ordinal}]</Badge>
          ) : (
            <Badge>Not cited</Badge>
          )}
          <span className="text-[11px] text-slate-400" title="Rank in each retrieval leg">
            {ranks(source)}
          </span>
        </span>
        <Link to={href} className="shrink-0 font-medium text-brand-700 hover:underline">
          Open page →
        </Link>
      </div>
    </li>
  )
}

export function SourcesPanel({
  sources,
  citations,
  status,
  active,
  onSelect,
}: {
  sources: SourceOut[]
  citations: LiveCitation[]
  status: AnswerStatus
  /** The selected citation ordinal, shared with the answer's markers. */
  active: number | null
  onSelect: (ordinal: number | null) => void
}) {
  const [expanded, setExpanded] = useState<number | null>(null)

  // A new answer means a new set of passages; an old expansion index would
  // land on an unrelated card.
  useEffect(() => setExpanded(null), [sources])

  const byIndex = new Map(citations.map((c) => [c.source_index, c]))
  const activeIndex = active === null ? null : (citations.find((c) => c.ordinal === active)?.source_index ?? null)
  const cited = sources.filter((source) => source.cited).length

  const heading = status === 'unanswered' ? 'Closest passages' : 'Passages the model was shown'
  const hint =
    status === 'unanswered'
      ? 'None of these contained the answer. Read them to check that the refusal was right.'
      : status === 'streaming' && sources.length === 0
        ? 'Searching by meaning and by keyword, then fusing the two lists.'
        : `${count(sources.length)} retrieved, ${count(cited)} cited. Click a marker in the answer, or a passage here, to see what was quoted.`

  return (
    <div>
      <div className="px-1">
        <h2 className="text-xs font-semibold tracking-wide text-slate-500 uppercase">{heading}</h2>
        {sources.length > 0 || status === 'streaming' ? (
          <p className="mt-0.5 text-[11px] text-slate-400">{hint}</p>
        ) : null}
      </div>

      {sources.length === 0 ? (
        <div className="mt-2">
          {status === 'idle' ? (
            <Empty title="No passages yet">
              Ask a question to see which passages the model reads before it answers.
            </Empty>
          ) : status === 'streaming' ? (
            <p className="px-1 text-xs text-slate-500">
              <span className="caret">Searching</span>
            </p>
          ) : status === 'budget_exhausted' ? (
            <Empty title="Nothing was retrieved">The budget check comes before the search.</Empty>
          ) : (
            <Empty title="Nothing was retrieved">
              There are no readable documents in the scope of this question.
            </Empty>
          )}
        </div>
      ) : (
        <ul className="mt-2 space-y-2">
          {sources.map((source) => {
            const citation = byIndex.get(source.index)
            return (
              <SourceCard
                key={source.index}
                source={source}
                citation={citation}
                active={activeIndex === source.index}
                expanded={expanded === source.index}
                onClick={() => {
                  setExpanded(expanded === source.index ? null : source.index)
                  if (citation) onSelect(active === citation.ordinal ? null : citation.ordinal)
                  else onSelect(null)
                }}
              />
            )
          })}
        </ul>
      )}
    </div>
  )
}
