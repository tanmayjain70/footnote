import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../../lib/api'
import { count } from '../../lib/format'
import type { ReviewQueueItem } from '../../lib/types'
import { Badge, Button, Card, Empty, Notice, Spinner } from '../ui'

const SHOW_FIRST = 10

/*
 * The documents with values still waiting for a person, worst first: the API
 * puts those whose evidence did not check out at the top, because a value
 * with an issue is the one most likely to be wrong. Same query key as the
 * badge in the nav, so the two never disagree.
 */
export function ReviewQueueCard({ portfolioName }: { portfolioName: string | null }) {
  const [showAll, setShowAll] = useState(false)

  const queue = useQuery({
    queryKey: ['review-queue'],
    queryFn: () => api.get<ReviewQueueItem[]>('/review-queue'),
    refetchInterval: 30_000,
  })

  const items = (queue.data ?? []).filter(
    (item) => !portfolioName || item.portfolio_name === portfolioName,
  )
  const pending = items.reduce((sum, item) => sum + item.pending, 0)
  const shown = showAll ? items : items.slice(0, SHOW_FIRST)

  return (
    <Card
      title="Needs review"
      subtitle={
        items.length
          ? `${count(pending)} value${pending === 1 ? '' : 's'} across ${count(items.length)} document${items.length === 1 ? '' : 's'}`
          : 'Values the model extracted that nobody has confirmed'
      }
    >
      {queue.isLoading ? (
        <Spinner label="Loading the review queue" />
      ) : queue.error ? (
        <Notice tone="bad">The review queue could not be loaded.</Notice>
      ) : !items.length ? (
        <Empty title="Nothing waiting for review">
          {portfolioName
            ? `Every extracted value in ${portfolioName} has been looked at.`
            : 'Every extracted value has been looked at. New extractions appear here.'}
        </Empty>
      ) : (
        <ul className="divide-y divide-slate-100">
          {shown.map((item) => (
            <li key={item.document_id} className="flex items-start justify-between gap-3 py-2.5">
              <div className="min-w-0">
                <Link
                  to={`/documents/${item.document_id}`}
                  className="block truncate text-sm font-medium text-slate-900 hover:text-brand-700"
                >
                  {item.title}
                </Link>
                <span className="block text-xs text-slate-500">{item.portfolio_name}</span>
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                {item.issues > 0 && (
                  <Badge
                    tone="red"
                    title="Pending values whose evidence did not check out: a quote that is not on the page, or a value that could not be read"
                  >
                    {count(item.issues)} issue{item.issues === 1 ? '' : 's'}
                  </Badge>
                )}
                <Badge tone="amber" title="Values waiting for a person">
                  {count(item.pending)} pending
                </Badge>
              </div>
            </li>
          ))}
        </ul>
      )}
      {items.length > SHOW_FIRST && (
        <div className="mt-3">
          <Button size="sm" variant="ghost" onClick={() => setShowAll((value) => !value)}>
            {showAll ? 'Show fewer' : `Show all ${count(items.length)}`}
          </Button>
        </div>
      )}
    </Card>
  )
}
