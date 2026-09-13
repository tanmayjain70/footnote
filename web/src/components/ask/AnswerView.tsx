import { cost, count, latency } from '../../lib/format'
import { CitationMarker, Notice, cx } from '../ui'
import type { AnswerState, AnswerStatus } from './answerState'

/*
 * The answer itself: the text with its [n] markers, and the card that says
 * what kind of answer it is. The markers sit at the end of each block because
 * a block is exactly the span a citation covers -- the provider splits text
 * at citation boundaries, and the stub emits one block per cited sentence --
 * so "end of block" is "after the sentence it supports".
 */

const REFUSAL_FALLBACK =
  'The documents in scope do not contain an answer to this. The closest passages are shown alongside.'

function AnswerText({
  state,
  status,
  active,
  onCite,
}: {
  state: AnswerState
  status: AnswerStatus
  active: number | null
  onCite: (ordinal: number) => void
}) {
  const blocks = [...state.blocks].sort((a, b) => a.block - b.block)
  const streaming = status === 'streaming'
  const titles = new Map(
    state.citations.map((c) => [c.ordinal, `${c.document_title} · page ${c.page_number}`]),
  )

  return (
    <p className="text-[15px] leading-7 whitespace-pre-wrap text-slate-800">
      {blocks.map((block, index) => (
        <span
          key={block.block}
          className={cx(streaming && index === blocks.length - 1 && 'caret')}
        >
          {block.text}
          {block.citations.map((ordinal) => (
            <CitationMarker
              key={ordinal}
              ordinal={ordinal}
              active={active === ordinal}
              title={titles.get(ordinal)}
              onClick={() => onCite(ordinal)}
            />
          ))}
        </span>
      ))}
    </p>
  )
}

function Progress({ state }: { state: AnswerState }) {
  const label =
    state.sources.length === 0
      ? 'Searching the documents'
      : `Reading ${count(state.sources.length)} passages`
  return (
    <p className="text-sm text-slate-500">
      <span className="caret">{label}</span>
    </p>
  )
}

/** Cost, time and model in small grey text, so the spend is never a surprise. */
function Meta({ state }: { state: AnswerState }) {
  const result = state.result
  if (!result) return null
  const tokens = result.input_tokens + result.output_tokens
  return (
    <div className="mt-3 space-y-0.5 text-xs text-slate-400">
      <p className="tabular">
        {cost(result.cost_usd)} · {latency(result.latency_ms)} · {result.model}
        {tokens > 0 && (
          <>
            {' '}
            · {count(result.input_tokens)} in / {count(result.output_tokens)} out
            {result.cache_read_tokens > 0 && ` (${count(result.cache_read_tokens)} cached)`}
          </>
        )}
      </p>
      {result.dropped_citations > 0 && (
        <p className="text-note-700">
          {count(result.dropped_citations)} citation{result.dropped_citations === 1 ? '' : 's'}{' '}
          dropped: the model pointed at something it was not shown, so the marker was not rendered.
        </p>
      )}
    </div>
  )
}

export function AnswerView({
  state,
  status,
  active,
  onCite,
}: {
  state: AnswerState
  status: AnswerStatus
  /** The selected citation ordinal, shared with the passages panel. */
  active: number | null
  onCite: (ordinal: number) => void
}) {
  const textual = state.blocks.some((block) => block.text.trim().length > 0)
  const text = textual ? (
    <AnswerText state={state} status={status} active={active} onCite={onCite} />
  ) : null

  switch (status) {
    case 'idle':
      return null

    case 'streaming':
      return text ?? <Progress state={state} />

    case 'answered':
      return (
        <div>
          {text}
          <Meta state={state} />
        </div>
      )

    case 'unanswered':
      return (
        <div>
          <Notice tone="note" title="Not answerable from the documents you can see">
            <div className="text-sm">
              {text ?? <p>{state.result?.answer_text ?? REFUSAL_FALLBACK}</p>}
            </div>
          </Notice>
          <Meta state={state} />
        </div>
      )

    case 'budget_exhausted':
      return (
        <Notice tone="bad" title="Daily model budget exhausted">
          {state.failure?.message ??
            state.result?.error ??
            state.result?.answer_text ??
            'Questions spend the model budget, and today’s is used up. It resets at midnight UTC.'}
        </Notice>
      )

    case 'failed':
      return (
        <div className="space-y-3">
          {text}
          <Notice tone="bad" title="The answer failed">
            {state.failure?.message ??
              state.result?.error ??
              'Something went wrong while answering. Nothing has been charged for an answer that did not arrive.'}
          </Notice>
          <Meta state={state} />
        </div>
      )

    case 'stopped':
      return (
        <div className="space-y-3">
          {text}
          <Notice tone="slate" title="Stopped">
            The answer was stopped before it finished, so it has not been checked and cannot be
            trusted as it stands. Ask again to get a complete one.
          </Notice>
        </div>
      )
  }
}
