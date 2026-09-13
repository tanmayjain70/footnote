import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../../lib/api'
import { cost, count, dateTime, humanise, latency } from '../../lib/format'
import type { EvalResult, EvalRunDetail } from '../../lib/types'
import { Badge, Empty, Notice, Spinner, Table, Td, Th } from '../ui'

/*
 * One run, question by question. The totals above say how the system did;
 * this table says on which questions, which is the only way to find out
 * why. A miss on the wrong-document row is a retrieval problem; an
 * "answered anyway" on an unanswerable one is a refusal problem, and the
 * link opens the very answer so the person can read what it said.
 */

function HitBadge({ hit, label }: { hit: boolean | null; label: string }) {
  if (hit === null) return null
  return (
    <Badge tone={hit ? 'green' : 'red'} title={`${label}: ${hit ? 'hit' : 'miss'}`}>
      {label} {hit ? 'hit' : 'miss'}
    </Badge>
  )
}

function Retrieved({ result }: { result: EvalResult }) {
  if (!result.answerable) {
    return (
      <span className="text-xs text-slate-400" title="Retrieval is not scored for a question with no right document">
        not scored
      </span>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      <HitBadge hit={result.retrieved_doc_hit} label="doc" />
      <HitBadge hit={result.retrieved_page_hit} label="page" />
    </div>
  )
}

function Answer({ result }: { result: EvalResult }) {
  if (result.answered === null && result.refused_correctly === null) {
    return <span className="text-slate-400">—</span>
  }
  if (!result.answerable) {
    return result.refused_correctly ? (
      <Badge tone="green">refused</Badge>
    ) : (
      <Badge tone="red" title="The system answered a question the documents cannot answer">
        answered anyway
      </Badge>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {result.answered ? (
        <Badge tone="green">answered</Badge>
      ) : (
        <Badge tone="amber" title="No answer with a verified citation">
          no answer
        </Badge>
      )}
      {result.answered && <HitBadge hit={result.cited_doc_hit} label="cited doc" />}
      {result.answered && <HitBadge hit={result.cited_page_hit} label="cited page" />}
    </div>
  )
}

export function RunDetail({ runId }: { runId: string }) {
  const run = useQuery({
    queryKey: ['evals', 'run', runId],
    queryFn: () => api.get<EvalRunDetail>(`/evals/runs/${runId}`),
    // An end-to-end run answers questions in the worker; follow it in.
    refetchInterval: (current) => (current.state.data?.status === 'running' ? 3000 : false),
  })

  if (run.isLoading) return <Spinner label="Loading results" />
  if (run.error) {
    return (
      <Notice tone="bad">
        {run.error instanceof ApiError ? run.error.message : 'The run could not be loaded.'}
      </Notice>
    )
  }
  if (!run.data) return null

  const data = run.data
  const endToEnd = data.mode === 'end_to_end'
  const config = Object.entries(data.config).filter(
    ([, value]) => value !== null && value !== '' && typeof value !== 'object',
  )

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span>
          Started {dateTime(data.started_at)}
          {data.started_by_name ? ` by ${data.started_by_name}` : ''}
          {data.finished_at ? ` · finished ${dateTime(data.finished_at)}` : ''}
        </span>
        {typeof data.totals.latency_p50_ms === 'number' && (
          <span className="tabular">median latency {latency(data.totals.latency_p50_ms)}</span>
        )}
        {config.map(([key, value]) => (
          <Badge key={key} tone="slate" title={humanise(key)}>
            {humanise(key)}: {String(value)}
          </Badge>
        ))}
      </div>

      {data.status === 'running' && (
        <Notice tone="slate">
          This run is still going. Results appear as each question finishes; checking every few
          seconds.
        </Notice>
      )}
      {data.status === 'failed' && (
        <Notice tone="bad" title="The run failed">
          {data.error ?? 'No error message was recorded.'}
        </Notice>
      )}

      {!data.results.length ? (
        <Empty title="No results yet">
          {data.status === 'running'
            ? 'The first questions are being scored.'
            : 'No active questions were in the set when this run started.'}
        </Empty>
      ) : (
        <div className="rounded-lg border border-slate-200 bg-white px-5">
          <Table>
            <thead>
              <tr>
                <Th>Question</Th>
                <Th>Expected</Th>
                <Th>Retrieved</Th>
                <Th right>Rank</Th>
                {endToEnd && <Th>Answer</Th>}
                {endToEnd && <Th right>Latency</Th>}
                {endToEnd && <Th right>Cost</Th>}
              </tr>
            </thead>
            <tbody>
              {data.results.map((result) => (
                <tr key={result.eval_question_id} className="align-top">
                  <Td className="max-w-md">
                    <span className="block text-slate-900">{result.question}</span>
                    {result.question_id && (
                      <Link
                        to={`/questions/${result.question_id}`}
                        className="mt-0.5 inline-block text-xs font-medium text-brand-700 hover:underline"
                      >
                        Open answer →
                      </Link>
                    )}
                  </Td>
                  <Td className="min-w-44">
                    {result.answerable ? (
                      <>
                        <span className="block text-slate-800">
                          {result.expected_document_title ?? (
                            <span className="text-slate-400">Document no longer exists</span>
                          )}
                        </span>
                        {result.expected_pages.length > 0 && (
                          <span className="tabular block text-xs text-slate-500">
                            p. {result.expected_pages.join(', ')}
                          </span>
                        )}
                      </>
                    ) : (
                      <Badge tone="slate">should refuse</Badge>
                    )}
                  </Td>
                  <Td>
                    <Retrieved result={result} />
                  </Td>
                  <Td right className="text-slate-600">
                    {result.hit_rank === null ? '—' : count(result.hit_rank)}
                  </Td>
                  {endToEnd && (
                    <Td>
                      <Answer result={result} />
                    </Td>
                  )}
                  {endToEnd && (
                    <Td right className="whitespace-nowrap text-slate-600">
                      {latency(result.latency_ms)}
                    </Td>
                  )}
                  {endToEnd && (
                    <Td right className="whitespace-nowrap text-xs text-slate-500">
                      {cost(result.cost_usd)}
                    </Td>
                  )}
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </div>
  )
}
