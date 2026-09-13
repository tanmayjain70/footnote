import { Fragment, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../../lib/api'
import { cost, count, dateTime } from '../../lib/format'
import type {
  DocumentOut,
  ExtractedValueOut,
  ExtractionOut,
  FieldSpec,
  ReviewRequest,
} from '../../lib/types'
import {
  Badge,
  Button,
  Empty,
  ErrorNote,
  Field,
  Input,
  Notice,
  Select,
  Spinner,
  StatusPill,
  Table,
  Td,
  Th,
} from '../ui'
import { ISSUES, editableValue, formatValue, isLive } from './values'

type EditMode = 'correct' | 'reject'

interface Editing {
  id: string
  mode: EditMode
}

/**
 * The value column. A corrected value shows what it was corrected from, and a
 * rejected one is struck through rather than hidden: the register must never
 * quietly forget what the model said.
 */
function ValueCell({ value }: { value: ExtractedValueOut }) {
  const effective = formatValue(value.type, value.effective_value)
  const original = formatValue(value.type, value.value_json)

  if (value.review_status === 'rejected') {
    return (
      <span className="text-slate-400 line-through" title="Rejected by a reviewer">
        {effective === '—' ? (value.value_text ?? '—') : effective}
      </span>
    )
  }
  if (value.review_status === 'corrected') {
    return (
      <>
        <span className="font-medium text-slate-900">{effective}</span>
        {original !== effective && (
          <span className="block text-xs text-slate-400">
            was <span className="line-through">{original === '—' ? value.value_text : original}</span>
          </span>
        )}
      </>
    )
  }
  if (effective === '—' && value.value_text) {
    // The model wrote something the parser could not read. Show it as text,
    // in amber, so the reviewer sees what to correct.
    return (
      <span className="text-note-800" title="As the model wrote it; could not be read as a typed value">
        {value.value_text}
      </span>
    )
  }
  return (
    <span className={effective === '—' ? 'text-slate-400' : 'font-medium text-slate-900'}>
      {effective}
    </span>
  )
}

function Evidence({
  value,
  onOpenPage,
}: {
  value: ExtractedValueOut
  onOpenPage: (page: number, highlight: string) => void
}) {
  const issue = value.issue ? ISSUES[value.issue] : null
  return (
    <div className="space-y-1">
      {value.quote && (
        <blockquote className="line-clamp-3 text-xs text-slate-600 italic">“{value.quote}”</blockquote>
      )}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {value.quote && value.page_number !== null && (
          <button
            type="button"
            className="font-medium text-brand-700 hover:underline"
            onClick={() => onOpenPage(value.page_number as number, value.quote as string)}
          >
            Open page {value.page_number} →
          </button>
        )}
        {!value.quote && value.page_number !== null && (
          <span className="text-slate-500">Page {value.page_number}</span>
        )}
        {issue && (
          <Badge tone="amber" title={issue.title}>
            {issue.label}
          </Badge>
        )}
      </div>
    </div>
  )
}

function ReviewCell({ value }: { value: ExtractedValueOut }) {
  return (
    <div className="space-y-0.5">
      <StatusPill status={value.review_status} />
      {value.reviewed_at && (
        <span className="block text-xs text-slate-400">
          {value.reviewed_by_name ?? 'Someone'} · {dateTime(value.reviewed_at)}
        </span>
      )}
      {value.review_note && (
        <span className="block max-w-xs text-xs text-slate-500 italic">{value.review_note}</span>
      )}
    </div>
  )
}

/**
 * The inline form under a row. A correction takes a value in the field's own
 * type (a date picker for dates, the allowed codes for an enum) so what goes
 * to the API is already in a shape `parse_value` accepts; the note is optional
 * either way, but it is what the next person reads, so it is asked for.
 */
function Editor({
  value,
  spec,
  mode,
  pending,
  onSave,
  onCancel,
}: {
  value: ExtractedValueOut
  spec: FieldSpec | undefined
  mode: EditMode
  pending: boolean
  onSave: (body: ReviewRequest) => void
  onCancel: () => void
}) {
  const [corrected, setCorrected] = useState(() =>
    value.type === 'date' || value.type === 'bool' || value.type === 'enum'
      ? editableValue(value.effective_value)
      : editableValue(value.effective_value) || (value.value_text ?? ''),
  )
  const [note, setNote] = useState('')

  function submit(event: FormEvent) {
    event.preventDefault()
    if (mode === 'reject') {
      onSave({ action: 'reject', note: note.trim() || null })
      return
    }
    if (!corrected.trim()) return
    onSave({ action: 'correct', corrected_value: corrected.trim(), note: note.trim() || null })
  }

  let control
  if (mode === 'correct') {
    if (value.type === 'date') {
      control = (
        <Input type="date" value={corrected} onChange={(e) => setCorrected(e.target.value)} required />
      )
    } else if (value.type === 'bool') {
      control = (
        <Select className="w-full" value={corrected} onChange={(e) => setCorrected(e.target.value)} required>
          <option value="">Choose…</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </Select>
      )
    } else if (value.type === 'enum' && spec?.enum_values.length) {
      control = (
        <Select className="w-full" value={corrected} onChange={(e) => setCorrected(e.target.value)} required>
          <option value="">Choose…</option>
          {spec.enum_values.map((code) => (
            <option key={code} value={code}>
              {formatValue('enum', code)}
            </option>
          ))}
        </Select>
      )
    } else {
      control = (
        <Input
          value={corrected}
          onChange={(e) => setCorrected(e.target.value)}
          placeholder={
            value.type === 'money'
              ? 'e.g. £42,500'
              : value.type === 'integer'
                ? 'e.g. 6'
                : 'As written in the lease'
          }
          inputMode={value.type === 'integer' ? 'numeric' : undefined}
          required
          autoFocus
        />
      )
    }
  }

  return (
    <form onSubmit={submit} className="grid items-end gap-3 sm:grid-cols-[1fr_1fr_auto]">
      {mode === 'correct' ? (
        <Field label={`Corrected ${value.label.toLowerCase()}`} hint={spec?.description}>
          {control}
        </Field>
      ) : (
        <p className="text-sm text-slate-700 sm:self-center">
          Rejecting leaves this term blank in the register. Say why, if it helps the next person.
        </p>
      )}
      <Field label="Note (optional)">
        <Input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. Rent stated inclusive of service charge"
          maxLength={500}
        />
      </Field>
      <div className="flex gap-2">
        <Button
          type="submit"
          size="sm"
          variant={mode === 'reject' ? 'danger' : 'primary'}
          disabled={pending || (mode === 'correct' && !corrected.trim())}
        >
          {pending ? 'Saving…' : mode === 'reject' ? 'Reject value' : 'Save correction'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

/**
 * The register's raw material for one document: every field in the registry
 * with what the model found, where it found it, and whether a person has
 * agreed. The table polls while an extraction is queued or running, and the
 * review actions update the row in place so a reviewer can work down the list.
 */
export function ExtractedTerms({
  document: doc,
  canReview,
  onOpenPage,
  onRun,
  runPending,
}: {
  document: DocumentOut
  canReview: boolean
  onOpenPage: (page: number, highlight: string) => void
  /** Present when the caller may start an extraction. */
  onRun?: () => void
  runPending?: boolean
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<Editing | null>(null)
  const [error, setError] = useState<string | null>(null)

  const extraction = useQuery({
    queryKey: ['extraction', doc.id],
    queryFn: async (): Promise<ExtractionOut | null> => {
      try {
        return await api.get<ExtractionOut>(`/documents/${doc.id}/extraction`)
      } catch (err) {
        // "Never extracted" is a state of the screen, not a failure of it.
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }
    },
    // Poll while this extraction is live, and also while the document says
    // one is on the way: a run queued from another screen leaves this query
    // holding the previous result until the new row lands.
    refetchInterval: (query) => {
      const data = query.state.data
      return (data && isLive(data.status)) || isLive(doc.extraction_status) ? 3000 : false
    },
  })

  const fields = useQuery({
    queryKey: ['fields'],
    queryFn: () => api.get<FieldSpec[]>('/fields'),
    staleTime: Infinity,
  })

  // When a run finishes, the counts elsewhere (pending review in the nav, the
  // register, this document's row) are stale.
  const status = extraction.data?.status
  useEffect(() => {
    if (status !== 'done' && status !== 'failed') return
    queryClient.invalidateQueries({ queryKey: ['document', doc.id] })
    queryClient.invalidateQueries({ queryKey: ['documents'] })
    queryClient.invalidateQueries({ queryKey: ['review-queue'] })
    queryClient.invalidateQueries({ queryKey: ['register'] })
  }, [status, doc.id, queryClient])

  const review = useMutation({
    mutationFn: ({ valueId, body }: { valueId: string; body: ReviewRequest }) =>
      api.post<ExtractedValueOut>(`/extracted-values/${valueId}/review`, body),
    onSuccess: (updated) => {
      queryClient.setQueryData<ExtractionOut | null>(['extraction', doc.id], (current) =>
        current
          ? { ...current, values: current.values.map((v) => (v.id === updated.id ? updated : v)) }
          : current,
      )
      queryClient.invalidateQueries({ queryKey: ['document', doc.id] })
      queryClient.invalidateQueries({ queryKey: ['documents'] })
      queryClient.invalidateQueries({ queryKey: ['review-queue'] })
      queryClient.invalidateQueries({ queryKey: ['register'] })
      setEditing(null)
      setError(null)
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'The review was not saved.'),
  })

  if (extraction.isLoading) return <Spinner label="Loading extracted terms" />
  if (extraction.error) return <Notice tone="bad">The extraction could not be loaded.</Notice>

  const data = extraction.data
  if (!data) {
    if (isLive(doc.extraction_status)) {
      return <Notice tone="slate">An extraction has been queued. Checking every few seconds…</Notice>
    }
    return (
      <Empty title="No extraction yet">
        {doc.status === 'ready'
          ? 'Run an extraction to pull the key terms out of this document, each with a quote and a page number.'
          : 'Terms can be extracted once the document has been processed.'}
        {onRun && doc.status === 'ready' && (
          <div className="mt-3">
            <Button size="sm" variant="primary" onClick={onRun} disabled={runPending}>
              {runPending ? 'Starting…' : 'Run extraction'}
            </Button>
          </div>
        )}
      </Empty>
    )
  }

  const order = new Map((fields.data ?? []).map((field, index) => [field.key, index]))
  const specs = new Map((fields.data ?? []).map((field) => [field.key, field]))
  const values = [...data.values].sort(
    (a, b) => (order.get(a.field_key) ?? 999) - (order.get(b.field_key) ?? 999),
  )
  const tally = { pending: 0, confirmed: 0, corrected: 0, rejected: 0 }
  for (const value of values) tally[value.review_status] += 1
  const columns = canReview ? 6 : 5
  const live = isLive(data.status) || isLive(doc.extraction_status)

  return (
    <div className="space-y-4">
      {live && (
        <Notice tone="slate" title={data.status === 'running' ? 'Extraction running' : 'Extraction queued'}>
          The model is reading the document. This table fills in when it finishes; checking every
          few seconds.
        </Notice>
      )}
      {data.status === 'failed' && (
        <Notice tone="bad" title="Extraction failed">
          {data.error ?? 'No error message was recorded.'}
          {onRun && ' Run it again from the button above.'}
        </Notice>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}

      {data.status === 'done' && (
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-xs text-slate-500">
          <p className="tabular">
            {count(values.length)} terms ·{' '}
            <span className={tally.pending > 0 ? 'font-medium text-note-800' : undefined}>
              {count(tally.pending)} pending
            </span>{' '}
            · {count(tally.confirmed)} confirmed · {count(tally.corrected)} corrected ·{' '}
            {count(tally.rejected)} rejected
          </p>
          <p>
            Extracted by <span className="text-slate-700">{data.model}</span>
            {data.finished_at && ` · ${dateTime(data.finished_at)}`} · {cost(data.cost_usd)}
            {!canReview && ' · values count in the register once a lease administrator confirms them'}
          </p>
        </div>
      )}

      {values.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white px-5 shadow-xs">
          <Table>
            <thead>
              <tr>
                <Th>Term</Th>
                <Th>Value</Th>
                <Th>Confidence</Th>
                <Th>Evidence</Th>
                <Th>Review</Th>
                {canReview && <Th right>Actions</Th>}
              </tr>
            </thead>
            <tbody>
              {values.map((value) => (
                <Fragment key={value.id}>
                  <tr className={editing?.id === value.id ? 'bg-brand-50/40' : 'hover:bg-slate-50'}>
                    <Td className="whitespace-nowrap font-medium text-slate-900">{value.label}</Td>
                    <Td className="tabular">
                      <ValueCell value={value} />
                    </Td>
                    <Td>
                      <StatusPill status={value.confidence} />
                    </Td>
                    <Td className="max-w-md">
                      <Evidence value={value} onOpenPage={onOpenPage} />
                    </Td>
                    <Td>
                      <ReviewCell value={value} />
                    </Td>
                    {canReview && (
                      <Td right>
                        <div className="flex justify-end gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={review.isPending || value.review_status === 'confirmed'}
                            onClick={() =>
                              review.mutate({ valueId: value.id, body: { action: 'confirm' } })
                            }
                          >
                            Confirm
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={review.isPending}
                            onClick={() =>
                              setEditing(
                                editing?.id === value.id && editing.mode === 'correct'
                                  ? null
                                  : { id: value.id, mode: 'correct' },
                              )
                            }
                          >
                            Correct
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-bad-700"
                            disabled={review.isPending || value.review_status === 'rejected'}
                            onClick={() =>
                              setEditing(
                                editing?.id === value.id && editing.mode === 'reject'
                                  ? null
                                  : { id: value.id, mode: 'reject' },
                              )
                            }
                          >
                            Reject
                          </Button>
                        </div>
                      </Td>
                    )}
                  </tr>
                  {editing?.id === value.id && (
                    <tr>
                      <td colSpan={columns} className="border-b border-slate-100 bg-slate-50 px-5 py-4">
                        <Editor
                          key={`${value.id}-${editing.mode}`}
                          value={value}
                          spec={specs.get(value.field_key)}
                          mode={editing.mode}
                          pending={review.isPending}
                          onSave={(body) => review.mutate({ valueId: value.id, body })}
                          onCancel={() => setEditing(null)}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </div>
  )
}
