import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { count, dateTime, humanise } from '../lib/format'
import type { DocumentOut, ExtractionOut, Page, QuestionOut } from '../lib/types'
import {
  Badge,
  Button,
  Empty,
  ErrorNote,
  Notice,
  Spinner,
  StatusPill,
  TabPanel,
  Tabs,
} from '../components/ui'
import { ExtractedTerms } from '../components/documents/ExtractedTerms'
import { PagesTab } from '../components/documents/PagesTab'
import { QuestionsTab } from '../components/documents/QuestionsTab'
import { fileSize, isLive } from '../components/documents/values'

type TabId = 'terms' | 'pages' | 'questions'

/** The questions endpoint is a list; accept a page too so a change of mind on the API side does not blank the tab. */
function asList<T>(data: T[] | Page<T>): T[] {
  return Array.isArray(data) ? data : data.items
}

export function DocumentPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { me, reload } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const pageParam = searchParams.get('page')
  const highlightParam = searchParams.get('highlight')
  const [tab, setTab] = useState<TabId>(pageParam ? 'pages' : 'terms')
  const [error, setError] = useState<string | null>(null)
  const permissions = me?.permissions

  // A link into a page (from an answer's citation, or a value's quote) lands
  // on the Pages tab whichever tab was open.
  useEffect(() => {
    if (pageParam) setTab('pages')
  }, [pageParam, highlightParam])

  const documentQuery = useQuery({
    queryKey: ['document', id],
    queryFn: () => api.get<DocumentOut>(`/documents/${id}`),
    // Ingestion and extraction both run in the worker; follow either.
    refetchInterval: (current) => {
      const doc = current.state.data
      return doc && (isLive(doc.status) || isLive(doc.extraction_status)) ? 3000 : false
    },
    retry: (failureCount, err) =>
      !(err instanceof ApiError && err.status === 404) && failureCount < 1,
  })

  const questions = useQuery({
    queryKey: ['document-questions', id],
    queryFn: async () =>
      asList(await api.get<QuestionOut[] | Page<QuestionOut>>(`/documents/${id}/questions`)),
    enabled: Boolean(documentQuery.data),
  })

  function invalidateLists() {
    queryClient.invalidateQueries({ queryKey: ['documents'] })
    queryClient.invalidateQueries({ queryKey: ['health'] })
    queryClient.invalidateQueries({ queryKey: ['review-queue'] })
    queryClient.invalidateQueries({ queryKey: ['register'] })
  }

  const extract = useMutation({
    mutationFn: () => api.post<ExtractionOut>(`/documents/${id}/extract`),
    onSuccess: (extraction) => {
      setError(null)
      queryClient.setQueryData(['extraction', id], extraction)
      queryClient.invalidateQueries({ queryKey: ['document', id] })
      invalidateLists()
      setTab('terms')
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'The extraction could not be started.'),
  })

  const reingest = useMutation({
    mutationFn: () => api.post<DocumentOut>(`/documents/${id}/reingest`),
    onSuccess: (updated) => {
      setError(null)
      queryClient.setQueryData(['document', id], updated)
      queryClient.invalidateQueries({ queryKey: ['extraction', id] })
      queryClient.invalidateQueries({ queryKey: ['document-pages', id] })
      invalidateLists()
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'The document could not be re-ingested.'),
  })

  const remove = useMutation({
    mutationFn: () => api.del(`/documents/${id}`),
    onSuccess: () => {
      // Leave first: once the row is gone, this page's own queries would
      // refetch into a 404.
      navigate('/documents', { replace: true })
      queryClient.removeQueries({ queryKey: ['document', id] })
      invalidateLists()
      reload().catch(() => undefined)
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'The document could not be deleted.'),
  })

  function openPage(page: number, highlight: string) {
    setSearchParams({ page: String(page), highlight })
    setTab('pages')
  }

  if (documentQuery.isLoading) return <Spinner label="Loading document" />
  if (documentQuery.error) {
    const missing = documentQuery.error instanceof ApiError && documentQuery.error.status === 404
    return (
      <Empty title={missing ? 'Document not found' : 'The document could not be loaded'}>
        {missing && 'It may have been deleted, or it is in a portfolio you cannot see. '}
        <Link to="/documents" className="font-medium text-brand-700 hover:underline">
          Back to documents
        </Link>
      </Empty>
    )
  }
  if (!documentQuery.data) return null

  const doc = documentQuery.data
  const busy = isLive(doc.status)
  const extracting = isLive(doc.extraction_status)
  const metadata = Object.entries(doc.metadata).filter(
    ([, value]) => value !== null && value !== '' && typeof value !== 'object',
  )

  function confirmReingest() {
    if (
      window.confirm(
        `Re-ingest “${doc.title}”?\n\nIts pages and passages are rebuilt from the stored PDF. Any extracted terms, and the review of them, are removed.`,
      )
    ) {
      reingest.mutate()
    }
  }

  function confirmDelete() {
    if (
      window.confirm(
        `Delete “${doc.title}”?\n\nThe PDF, its pages, its extracted terms and every citation into it are removed. This cannot be undone.`,
      )
    ) {
      remove.mutate()
    }
  }

  // No count on the terms tab: "17" would read as the number of terms and
  // "3" as how many are pending, and neither is obvious from a bare figure.
  const tabs = [
    { id: 'terms', label: 'Extracted terms' },
    { id: 'pages', label: 'Pages', count: doc.page_count || null },
    { id: 'questions', label: 'Questions', count: questions.data?.length ?? null },
  ]

  return (
    <div className="space-y-5">
      <div>
        <Link to="/documents" className="text-xs font-medium text-slate-500 hover:text-brand-700">
          ← Documents
        </Link>
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold text-slate-900">{doc.title}</h1>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-slate-500">
              <span className="font-medium text-slate-700">{doc.portfolio_name}</span>
              <Badge>{humanise(doc.doc_type)}</Badge>
              <StatusPill status={doc.status} />
              <span className="tabular">
                {count(doc.page_count)} page{doc.page_count === 1 ? '' : 's'} ·{' '}
                {count(doc.chunk_count)} passage{doc.chunk_count === 1 ? '' : 's'} ·{' '}
                {fileSize(doc.byte_size)}
              </span>
              <span>
                Uploaded {doc.uploaded_by_name ? `by ${doc.uploaded_by_name} ` : ''}
                {dateTime(doc.created_at)}
              </span>
            </div>
            {metadata.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {metadata.map(([key, value]) => (
                  <Badge key={key} tone="slate" title={humanise(key)}>
                    {humanise(key)}: {String(value)}
                  </Badge>
                ))}
              </div>
            )}
            <p className="mt-1 text-xs text-slate-400">{doc.filename}</p>
          </div>

          <div className="flex flex-wrap gap-2">
            {permissions?.can_review && (
              <Button
                size="sm"
                variant="primary"
                disabled={doc.status !== 'ready' || extracting || extract.isPending}
                title={
                  doc.status !== 'ready'
                    ? 'The document must be processed first'
                    : extracting
                      ? 'An extraction is already in progress'
                      : undefined
                }
                onClick={() => extract.mutate()}
              >
                {extract.isPending
                  ? 'Starting…'
                  : extracting
                    ? 'Extracting…'
                    : doc.extraction_status === 'none'
                      ? 'Run extraction'
                      : 'Re-run extraction'}
              </Button>
            )}
            {permissions?.can_upload && (
              <>
                <Button
                  size="sm"
                  disabled={busy || reingest.isPending}
                  onClick={confirmReingest}
                >
                  {reingest.isPending ? 'Queueing…' : 'Re-ingest'}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={remove.isPending}
                  onClick={confirmDelete}
                >
                  {remove.isPending ? 'Deleting…' : 'Delete'}
                </Button>
              </>
            )}
          </div>
        </div>
      </div>

      {doc.status === 'failed' && (
        <Notice tone="bad" title="Processing failed">
          {doc.error ?? 'No error message was recorded.'}
          {permissions?.can_upload && ' Re-ingest to try again.'}
        </Notice>
      )}
      {busy && (
        <Notice tone="slate">
          This document is {doc.status === 'queued' ? 'queued for processing' : 'being processed'}.
          Pages, passages and terms appear when it is ready; this screen checks every few seconds.
        </Notice>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}

      <Tabs
        name="document"
        tabs={tabs}
        value={tab}
        onChange={(next) => setTab(next as TabId)}
        label="Document sections"
      />
      <TabPanel name="document" id="terms" active={tab === 'terms'}>
        <ExtractedTerms
          document={doc}
          canReview={Boolean(permissions?.can_review)}
          onOpenPage={openPage}
          onRun={permissions?.can_review ? () => extract.mutate() : undefined}
          runPending={extract.isPending}
        />
      </TabPanel>
      <TabPanel name="document" id="pages" active={tab === 'pages'}>
        <PagesTab document={doc} />
      </TabPanel>
      <TabPanel name="document" id="questions" active={tab === 'questions'}>
        <QuestionsTab documentId={id} questions={questions.data} loading={questions.isLoading} />
      </TabPanel>
    </div>
  )
}
