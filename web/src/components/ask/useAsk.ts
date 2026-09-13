import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../../lib/api'
import type { AnswerOut, AskRequest, DoneEvent } from '../../lib/types'
import { IDLE, ended, failed, fromStored, reduceEvent, started } from './answerState'
import type { AnswerState } from './answerState'

export type { AnswerState, AnswerStatus, LiveCitation } from './answerState'
export { IDLE, fromStored, statusOf } from './answerState'

/*
 * The fetch and the abort. Everything the events do to the state lives in
 * `answerState.ts`; this hook only decides which stream is the current one
 * and drops events from any other.
 */
export function useAsk() {
  const [state, setState] = useState<AnswerState>(IDLE)
  const controller = useRef<AbortController | null>(null)

  const abort = useCallback(() => {
    controller.current?.abort()
    controller.current = null
  }, [])

  // Leaving the page abandons the stream rather than letting it write into an
  // unmounted component.
  useEffect(() => abort, [abort])

  /**
   * Stream one answer. Resolves with the finished question, or null when the
   * request failed, the stream was stopped, or a newer question superseded it.
   */
  const ask = useCallback(
    async (question: string, portfolioId: string | null): Promise<AnswerOut | null> => {
      abort()
      const mine = new AbortController()
      controller.current = mine
      setState(started(question, portfolioId))

      // Events from a stream that has been superseded are dropped: the state
      // belongs to the newest question only.
      const update = (fn: (current: AnswerState) => AnswerState) => {
        if (controller.current === mine) setState(fn)
      }
      const finished: { value: AnswerOut | null } = { value: null }

      try {
        const body: AskRequest = { question, portfolio_id: portfolioId }
        await api.streamPost('/ask', body, {
          signal: mine.signal,
          onEvent(name, data) {
            if (name === 'done') finished.value = data as DoneEvent
            update((s) => reduceEvent(s, name, data))
          },
        })
        // The body ended without `done` or `error`: the connection dropped.
        // (An abort resolves quietly too, but then `update` is a no-op.)
        update(ended)
      } catch (err) {
        const failure =
          err instanceof ApiError
            ? { code: err.status === 429 ? 'budget_exhausted' : err.code, message: err.message }
            : {
                code: 'network',
                message: err instanceof Error ? err.message : 'The request did not go through.',
              }
        update((s) => failed(s, failure))
      }

      if (controller.current !== mine) return null
      controller.current = null
      return finished.value
    },
    [abort],
  )

  const stop = useCallback(() => {
    if (!controller.current) return
    abort()
    setState(ended)
  }, [abort])

  /** Swap in a fresh copy of the finished question, e.g. after feedback was saved. */
  const replace = useCallback((result: AnswerOut) => setState(fromStored(result)), [])

  /** Clear a finished answer. A stream in flight is left alone. */
  const resetIfDone = useCallback(
    () => setState((s) => (s.phase === 'done' ? IDLE : s)),
    [],
  )

  return { state, ask, stop, replace, resetIfDone }
}
