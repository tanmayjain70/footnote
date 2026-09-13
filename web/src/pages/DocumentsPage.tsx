import { useEffect, useState } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { api, query } from '../lib/api'
import { useAuth } from '../lib/auth'
import { count, dateTime, humanise } from '../lib/format'
import type { DocumentOut, Page } from '../lib/types'
import {
  Badge,
  Card,
  Empty,
  Input,
  Notice,
  Pager,
  Select,
  Spinner,
  StatusPill,
  Table,
  Td,
  Th,
} from '../components/ui'
import { UploadZone } from '../components/documents/UploadZone'
import { isLive } from '../components/documents/values'

const LIMIT = 50

const STATUSES = [
  { value: '', label: 'Any status' },
  { value: 'queued', label: 'Queued' },
  { value: 'processing', label: 'Processing' },
  { value: 'ready', label: 'Ready' },
  { value: 'failed', label: 'Failed' },
]

export function DocumentsPage() {
  const { me, reload } = useAuth()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [portfolio, setPortfolio] = useState('')
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [offset, setOffset] = useState(0)

  // The search box filters as you type, but not on every keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(search.trim())
      setOffset(0)
    }, 300)
    return () => clearTimeout(timer)
  }, [search])

  const documents = useQuery({
    queryKey: ['documents', { portfolio, status, q, offset }],
    queryFn: () =>
      api.get<Page<DocumentOut>>(
        `/documents${query({ portfolio_id: portfolio, status, q, limit: LIMIT, offset })}`,
      ),
    // Ingestion runs in a background worker. While anything on screen is
    // still queued or processing, the list follows it.
    refetchInterval: (current) =>
      current.state.data?.items.some((doc) => isLive(doc.status)) ? 3000 : false,
    placeholderData: keepPreviousData,
  })

  const portfolios = me?.portfolios ?? []
  const items = documents.data?.items ?? []
  const filtered = Boolean(portfolio || status || q)

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Documents</h1>
        <p className="mt-0.5 max-w-3xl text-sm text-slate-500">
          Every lease, deed and side letter in the portfolios you can see. Uploading reads the
          pages and indexes them for questions; extracting the key terms is a separate step, run
          per document from its page.
        </p>
      </div>

      {me?.permissions.can_upload && portfolios.length > 0 && (
        <UploadZone
          portfolios={portfolios}
          defaultPortfolio={portfolio}
          onUploaded={(result) => {
            queryClient.invalidateQueries({ queryKey: ['documents'] })
            queryClient.invalidateQueries({ queryKey: ['health'] })
            // The portfolio counts in the auth context are what the nav and
            // the scope selects show; a new document changes them.
            if (result.created) reload().catch(() => undefined)
          }}
        />
      )}

      <Card>
        <div className="flex flex-wrap items-center gap-3">
          <Select
            value={portfolio}
            onChange={(e) => {
              setPortfolio(e.target.value)
              setOffset(0)
            }}
          >
            <option value="">All portfolios</option>
            {portfolios.map((option) => (
              <option key={option.id} value={option.id}>
                {option.name}
                {option.confidential ? ' (confidential)' : ''}
              </option>
            ))}
          </Select>
          <Select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value)
              setOffset(0)
            }}
          >
            {STATUSES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
          <Input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search titles and file names"
            aria-label="Search documents"
            className="w-72"
          />
          {documents.isFetching && documents.data && (
            <span className="text-xs text-slate-400">Refreshing…</span>
          )}
        </div>
      </Card>

      {documents.isLoading ? (
        <Spinner label="Loading documents" />
      ) : documents.error ? (
        <Notice tone="bad">The documents could not be loaded.</Notice>
      ) : !items.length ? (
        <Empty title={filtered ? 'No documents match' : 'No documents yet'}>
          {filtered
            ? 'Try a different portfolio, status or search.'
            : me?.permissions.can_upload
              ? 'Upload a PDF above to get started.'
              : 'Nothing has been uploaded to the portfolios you can see.'}
        </Empty>
      ) : (
        <Card title={`${count(documents.data?.total ?? 0)} document${documents.data?.total === 1 ? '' : 's'}`}>
          <Table>
            <thead>
              <tr>
                <Th>Title</Th>
                <Th>Portfolio</Th>
                <Th>Type</Th>
                <Th right>Pages</Th>
                <Th>Status</Th>
                <Th>Extraction</Th>
                <Th right>To review</Th>
                <Th>Uploaded by</Th>
                <Th>Uploaded</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((doc) => (
                <tr
                  key={doc.id}
                  onClick={() => navigate(`/documents/${doc.id}`)}
                  className="cursor-pointer hover:bg-slate-50"
                >
                  <Td className="max-w-xs">
                    <Link
                      to={`/documents/${doc.id}`}
                      onClick={(event) => event.stopPropagation()}
                      className="font-medium text-slate-900 hover:text-brand-700"
                    >
                      {doc.title}
                    </Link>
                    {doc.filename !== doc.title && (
                      <span className="block truncate text-xs text-slate-400">{doc.filename}</span>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap">{doc.portfolio_name}</Td>
                  <Td className="whitespace-nowrap">{humanise(doc.doc_type)}</Td>
                  <Td right>{doc.page_count ? count(doc.page_count) : '—'}</Td>
                  <Td>
                    <StatusPill status={doc.status} title={doc.error ?? undefined} />
                  </Td>
                  <Td>
                    {doc.extraction_status === 'none' ? (
                      <span className="text-xs text-slate-400">Not run</span>
                    ) : (
                      <StatusPill status={doc.extraction_status} />
                    )}
                  </Td>
                  <Td right>
                    {doc.review_pending > 0 ? (
                      <Badge tone="amber" title="Extracted values waiting for a person to confirm them">
                        {count(doc.review_pending)}
                      </Badge>
                    ) : doc.extraction_status === 'done' ? (
                      <span className="text-xs text-slate-400">none</span>
                    ) : (
                      '—'
                    )}
                  </Td>
                  <Td className="whitespace-nowrap">{doc.uploaded_by_name ?? '—'}</Td>
                  <Td className="whitespace-nowrap text-slate-500">{dateTime(doc.created_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pager
            total={documents.data?.total ?? 0}
            limit={LIMIT}
            offset={offset}
            onChange={setOffset}
          />
        </Card>
      )}
    </div>
  )
}
