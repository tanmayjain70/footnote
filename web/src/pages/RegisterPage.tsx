import { useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { count, isoDate } from '../lib/format'
import type { RegisterResponse } from '../lib/types'
import { Card, Empty, ErrorNote, Notice, Spinner, Stat } from '../components/ui'
import { DEFAULT_FILTERS, byTermEnd, registerQuery } from '../components/register/filters'
import type { RegisterFilters } from '../components/register/filters'
import { RegisterFilterBar } from '../components/register/RegisterFilters'
import { RegisterTable } from '../components/register/RegisterTable'
import { ReviewQueueCard } from '../components/register/ReviewQueueCard'

/*
 * "Which leases have a break in 2027?" is a table, not a chat. This is the
 * table: one row per lease with a finished extraction, and by default only
 * the values a person has confirmed or corrected. Everything else is blank
 * until someone signs it off, and the export follows the same rule.
 */
export function RegisterPage() {
  const { me } = useAuth()
  const [filters, setFilters] = useState<RegisterFilters>(DEFAULT_FILTERS)
  const [exportError, setExportError] = useState<string | null>(null)

  const register = useQuery({
    queryKey: ['register', filters],
    queryFn: () => api.get<RegisterResponse>(`/register${registerQuery(filters)}`),
    placeholderData: keepPreviousData,
  })

  // With unreviewed values on screen, the reviewed view of the same
  // portfolio (no value filters, so every document is present) says which
  // cells to mark. It is the default view, so it is usually already cached.
  const baselineFilters: RegisterFilters = {
    ...DEFAULT_FILTERS,
    portfolio: filters.portfolio,
  }
  const baseline = useQuery({
    queryKey: ['register', baselineFilters],
    queryFn: () => api.get<RegisterResponse>(`/register${registerQuery(baselineFilters)}`),
    enabled: filters.includeUnreviewed,
  })
  const reviewed = useMemo(
    () =>
      baseline.data
        ? new Map(baseline.data.rows.map((row) => [row.document_id, row]))
        : undefined,
    [baseline.data],
  )

  const exportCsv = useMutation({
    mutationFn: () =>
      api.download(
        `/register/export.csv${registerQuery(filters)}`,
        `register-${isoDate(new Date())}.csv`,
      ),
    onSuccess: () => setExportError(null),
    onError: (err) =>
      setExportError(err instanceof ApiError ? err.message : 'The export could not be downloaded.'),
  })

  const portfolios = me?.portfolios ?? []
  const portfolioName = portfolios.find((p) => p.id === filters.portfolio)?.name ?? null
  const rows = useMemo(() => [...(register.data?.rows ?? [])].sort(byTermEnd), [register.data])
  const counts = register.data?.counts
  const filtered = Boolean(filters.portfolio || filters.expiringBefore || filters.hasBreak)
  const canReview = Boolean(me?.permissions.can_review)

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Register</h1>
        <p className="mt-0.5 max-w-3xl text-sm text-slate-500">
          Key terms across every lease you can see, one row per document. A value appears here
          once a lease administrator has confirmed or corrected it; until then the cell is blank,
          because a figure nobody has checked is not a figure to serve notice on.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label="Leases in the register" value={counts ? count(counts.documents) : '—'} />
        <Stat
          label="Fully reviewed"
          value={counts ? count(counts.complete) : '—'}
          hint={counts && counts.documents ? `of ${count(counts.documents)}` : undefined}
          tone={counts && counts.documents > 0 && counts.complete === counts.documents ? 'good' : 'slate'}
        />
        <Stat
          label="Values pending review"
          value={counts ? count(counts.pending_values) : '—'}
          tone={counts && counts.pending_values > 0 ? 'note' : 'slate'}
          hint={canReview ? 'Confirm them from each document’s page' : undefined}
        />
      </div>

      <div className={canReview ? 'grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]' : undefined}>
        <div className="min-w-0 space-y-5">
          <RegisterFilterBar
            filters={filters}
            portfolios={portfolios}
            onChange={setFilters}
            onExport={() => exportCsv.mutate()}
            exporting={exportCsv.isPending}
            refreshing={register.isFetching && Boolean(register.data)}
          />

          {exportError && <ErrorNote>{exportError}</ErrorNote>}
          {register.error && (
            <ErrorNote>
              {register.error instanceof ApiError
                ? register.error.message
                : 'The register could not be loaded.'}
            </ErrorNote>
          )}
          {filters.includeUnreviewed && (
            <Notice tone="note" title="Unreviewed values are showing">
              Cells marked <span className="font-medium">unreviewed</span> are what the model
              extracted and nobody has confirmed. They are left out of the register by default and
              out of the export unless this box is ticked.
              {baseline.error && ' The reviewed view could not be loaded, so the marks are missing.'}
            </Notice>
          )}

          {register.isLoading ? (
            <Spinner label="Loading the register" />
          ) : !register.error && !rows.length ? (
            <Empty title={filtered ? 'No leases match these filters' : 'The register is empty'}>
              {filtered
                ? 'Try a wider date, another portfolio, or clear the break filter.'
                : 'A lease joins the register once its key terms have been extracted. Run an extraction from a document’s page.'}
            </Empty>
          ) : rows.length > 0 ? (
            <Card
              title={`${count(rows.length)} lease${rows.length === 1 ? '' : 's'}`}
              subtitle="Sorted by term end, soonest first. Scroll sideways for the later columns."
            >
              <RegisterTable
                rows={rows}
                reviewed={reviewed}
                showUnreviewed={filters.includeUnreviewed}
              />
            </Card>
          ) : null}
        </div>

        {canReview && <ReviewQueueCard portfolioName={portfolioName} />}
      </div>
    </div>
  )
}
