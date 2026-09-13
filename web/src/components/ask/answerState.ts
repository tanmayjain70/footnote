import type {
  AnswerBlock,
  AnswerOut,
  Citation,
  CitationEvent,
  DoneEvent,
  ErrorEvent,
  RetrievalEvent,
  SourceOut,
  TextEvent,
} from '../../lib/types'

/*
 * One shape for an answer whether it is arriving over the stream right now
 * or was stored weeks ago. The page renders both through the same components,
 * so a stored answer looks exactly like it did when it was live and nothing
 * that only works mid-stream can creep into the layout.
 *
 * Pure functions only: the hook in `useAsk.ts` owns the fetch and the abort,
 * and feeds each event through `reduceEvent`. Keeping the two apart means the
 * event handling can be run without a browser.
 */

/**
 * What a citation carries while it is still arriving: everything but the
 * block range, which only the stored row has. Both the streamed event and the
 * stored `Citation` satisfy it.
 */
export type LiveCitation = Pick<
  Citation,
  | 'ordinal'
  | 'chunk_id'
  | 'document_id'
  | 'document_title'
  | 'page_number'
  | 'cited_text'
  | 'source_index'
>

export type Phase = 'idle' | 'streaming' | 'done'

export interface Failure {
  code: string
  message: string
}

export interface AnswerState {
  phase: Phase
  /** Known from the `retrieval` event onwards; null until then. */
  questionId: string | null
  question: string
  portfolioId: string | null
  blocks: AnswerBlock[]
  citations: LiveCitation[]
  sources: SourceOut[]
  /** The full question row once the server has finished, or the stored one. */
  result: AnswerOut | null
  /** The request was refused before streaming (a 429) or the stream sent `error`. */
  failure: Failure | null
  /** The stream ended before `done`: stopped here, or the connection dropped. */
  stopped: boolean
}

export const IDLE: AnswerState = {
  phase: 'idle',
  questionId: null,
  question: '',
  portfolioId: null,
  blocks: [],
  citations: [],
  sources: [],
  result: null,
  failure: null,
  stopped: false,
}

export type AnswerStatus =
  | 'idle'
  | 'streaming'
  | 'answered'
  | 'unanswered'
  | 'failed'
  | 'budget_exhausted'
  | 'stopped'

export function hasText(state: AnswerState): boolean {
  return state.blocks.some((block) => block.text.trim().length > 0)
}

/**
 * The one status the page branches on. The server's own status wins once it
 * has spoken, with one exception: a finished answer with no text in it is a
 * refusal however the row is labelled, because there is nothing to cite.
 */
export function statusOf(state: AnswerState): AnswerStatus {
  if (state.phase === 'idle') return 'idle'
  if (state.phase === 'streaming') return 'streaming'
  if (state.failure) return state.failure.code === 'budget_exhausted' ? 'budget_exhausted' : 'failed'
  if (state.stopped) return 'stopped'
  const result = state.result
  if (!result) return 'failed'
  if (result.status === 'answered' && !hasText(state)) return 'unanswered'
  return result.status
}

/** The state a fresh question starts in, before the first event. */
export function started(question: string, portfolioId: string | null): AnswerState {
  return { ...IDLE, phase: 'streaming', question, portfolioId }
}

/** A stored question, rendered as if it had just finished. */
export function fromStored(question: AnswerOut): AnswerState {
  const blocks = question.answer_blocks?.length
    ? question.answer_blocks
    : question.answer_text
      ? [{ block: 0, text: question.answer_text, citations: [] }]
      : []
  return {
    phase: 'done',
    questionId: question.id,
    question: question.text,
    portfolioId: question.portfolio_id,
    blocks,
    citations: question.citations,
    sources: question.sources,
    result: question,
    failure: null,
    stopped: false,
  }
}

function onRetrieval(state: AnswerState, data: RetrievalEvent): AnswerState {
  return {
    ...state,
    questionId: data.question_id,
    sources: data.sources.map((source) => ({ ...source, cited: false })),
  }
}

function onText(state: AnswerState, data: TextEvent): AnswerState {
  const blocks = state.blocks.slice()
  const at = blocks.findIndex((block) => block.block === data.block)
  if (at === -1) blocks.push({ block: data.block, text: data.text, citations: [] })
  else blocks[at] = { ...blocks[at], text: blocks[at].text + data.text }
  return { ...state, blocks }
}

function onCitation(state: AnswerState, data: CitationEvent): AnswerState {
  const blocks = state.blocks.slice()
  const at = blocks.findIndex((block) => block.block === data.block)
  if (at === -1) blocks.push({ block: data.block, text: '', citations: [data.ordinal] })
  else if (!blocks[at].citations.includes(data.ordinal)) {
    blocks[at] = { ...blocks[at], citations: [...blocks[at].citations, data.ordinal] }
  }
  // The same passage cited twice keeps its first ordinal, so the list is by
  // ordinal and a repeat only adds a marker to the new block.
  const citations = state.citations.some((c) => c.ordinal === data.ordinal)
    ? state.citations
    : [...state.citations, data]
  const sources = state.sources.map((source) =>
    source.index === data.source_index ? { ...source, cited: true } : source,
  )
  return { ...state, blocks, citations, sources }
}

function onDone(state: AnswerState, result: DoneEvent): AnswerState {
  // The server's blocks are the ones it stored; what streamed should match,
  // and is kept only when the server sent none (a refusal with no text).
  const blocks = result.answer_blocks?.length ? result.answer_blocks : state.blocks
  return {
    ...state,
    phase: 'done',
    questionId: result.id,
    blocks,
    citations: result.citations,
    sources: result.sources,
    result,
  }
}

/** Apply one server-sent event. Unknown event names are ignored. */
export function reduceEvent(state: AnswerState, name: string, data: unknown): AnswerState {
  switch (name) {
    case 'retrieval':
      return onRetrieval(state, data as RetrievalEvent)
    case 'text':
      return onText(state, data as TextEvent)
    case 'citation':
      return onCitation(state, data as CitationEvent)
    case 'done':
      return onDone(state, data as DoneEvent)
    case 'error':
      return { ...state, phase: 'done', failure: data as ErrorEvent }
    default:
      return state
  }
}

/** The request itself failed: a 429 before the stream, or a network error. */
export function failed(state: AnswerState, failure: Failure): AnswerState {
  return { ...state, phase: 'done', failure }
}

/** The stream ended without `done`. A finished answer is left as it is. */
export function ended(state: AnswerState): AnswerState {
  return state.phase === 'streaming' ? { ...state, phase: 'done', stopped: true } : state
}
