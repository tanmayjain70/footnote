import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, api } from '../../lib/api'
import type {
  DocumentOut,
  EvalQuestion,
  EvalQuestionCreate,
  MePortfolio,
  Page,
} from '../../lib/types'
import { Button, Card, ErrorNote, Field, Input, Select, Textarea } from '../ui'
import { parsePages } from './metrics'

/*
 * A golden question is a question plus the truth: which document answers it
 * and on which pages -- or that nothing does. The form insists on the
 * document for an answerable question because without it the run has
 * nothing to score against, and a question that cannot be scored is noise
 * in every rate on the page.
 */
export function AddQuestionForm({
  portfolios,
  onCreated,
}: {
  portfolios: MePortfolio[]
  onCreated: (question: EvalQuestion) => void
}) {
  const [question, setQuestion] = useState('')
  const [answerable, setAnswerable] = useState<'true' | 'false'>('true')
  const [documentId, setDocumentId] = useState('')
  const [pages, setPages] = useState('')
  const [portfolio, setPortfolio] = useState('')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)
  const isAnswerable = answerable === 'true'

  const documents = useQuery({
    queryKey: ['documents', 'options'],
    queryFn: () => api.get<Page<DocumentOut>>('/documents?limit=200'),
    staleTime: 60_000,
  })

  const create = useMutation({
    mutationFn: (body: EvalQuestionCreate) => api.post<EvalQuestion>('/evals/questions', body),
    onSuccess: (created) => {
      setQuestion('')
      setDocumentId('')
      setPages('')
      setNotes('')
      setError(null)
      onCreated(created)
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'The question could not be saved.'),
  })

  // A chosen portfolio narrows the document list to what could answer the
  // question in that scope; the full list is 48 titles that look alike.
  const options = (documents.data?.items ?? [])
    .filter((doc) => !portfolio || doc.portfolio_id === portfolio)
    .sort((a, b) => a.title.localeCompare(b.title))

  function submit(event: FormEvent) {
    event.preventDefault()
    const text = question.trim()
    if (text.length < 3) {
      setError('Write the question first.')
      return
    }
    const parsed = parsePages(pages)
    if (parsed === null) {
      setError('Expected pages should be page numbers separated by commas, like “3, 4”.')
      return
    }
    if (isAnswerable && !documentId) {
      setError('An answerable question needs the document that answers it, or retrieval cannot be scored.')
      return
    }
    setError(null)
    create.mutate({
      question: text,
      answerable: isAnswerable,
      expected_document_id: isAnswerable ? documentId : null,
      expected_pages: isAnswerable ? parsed : [],
      portfolio_id: portfolio || null,
      notes: notes.trim() || null,
    })
  }

  return (
    <Card
      title="Add a question"
      subtitle="Phrase it the way a property manager would ask it. The document and pages are what a run checks the answer against."
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Question">
          <Textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            rows={2}
            placeholder="e.g. What notice must the tenant give to exercise the break at Whitworth Court?"
            maxLength={2000}
            required
          />
        </Field>

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Field label="Answerable" hint={isAnswerable ? undefined : 'The right answer is a refusal.'}>
            <Select
              className="w-full"
              value={answerable}
              onChange={(e) => setAnswerable(e.target.value as 'true' | 'false')}
            >
              <option value="true">Answerable from the documents</option>
              <option value="false">Not answerable: should refuse</option>
            </Select>
          </Field>
          <Field label="Portfolio scope" hint="Optional. Limits retrieval, as the scope select on Ask does.">
            <Select
              className="w-full"
              value={portfolio}
              onChange={(e) => {
                setPortfolio(e.target.value)
                setDocumentId('')
              }}
            >
              <option value="">Any portfolio</option>
              {portfolios.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Expected document">
            <Select
              className="w-full"
              value={documentId}
              onChange={(e) => setDocumentId(e.target.value)}
              disabled={!isAnswerable || documents.isLoading}
              required={isAnswerable}
            >
              <option value="">
                {documents.isLoading ? 'Loading documents…' : isAnswerable ? 'Choose a document' : 'None'}
              </option>
              {options.map((doc) => (
                <option key={doc.id} value={doc.id}>
                  {doc.title}
                  {portfolio ? '' : ` · ${doc.portfolio_name}`}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Expected pages" hint="Where the answer sentence sits, e.g. 3, 4">
            <Input
              value={pages}
              onChange={(e) => setPages(e.target.value)}
              placeholder="3, 4"
              disabled={!isAnswerable}
              inputMode="numeric"
            />
          </Field>
        </div>

        <div className="grid items-end gap-4 md:grid-cols-[1fr_auto]">
          <Field label="Notes (optional)">
            <Input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Why this question is in the set"
              maxLength={500}
            />
          </Field>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? 'Saving…' : 'Add question'}
          </Button>
        </div>

        {documents.error && (
          <ErrorNote>The document list could not be loaded, so an expected document cannot be chosen.</ErrorNote>
        )}
        {error && <ErrorNote>{error}</ErrorNote>}
      </form>
    </Card>
  )
}
