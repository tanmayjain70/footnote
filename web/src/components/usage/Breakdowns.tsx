import { cost, count, humanise, percent } from '../../lib/format'
import type { UsageByKind, UsageByUser, UsageKind } from '../../lib/types'
import { Card, Empty, Table, Td, Th } from '../ui'

/*
 * Where the money went, two ways. By kind says whether it is answers or
 * extractions that cost; by person says who is asking. Both cover the same
 * window as the chart, so the three add up to the same total.
 */

const KIND_LABELS: Record<UsageKind, string> = {
  answer: 'Answers',
  extraction: 'Extractions',
  eval: 'Evaluation runs',
  embedding: 'Embeddings',
}

/** A kind the API adds later still gets a readable label rather than breaking the table. */
function kindLabel(kind: string): string {
  return (KIND_LABELS as Record<string, string>)[kind] ?? humanise(kind)
}

interface Row {
  key: string
  label: string
  calls: number
  cost_usd: string
}

function Rows({ rows, heading }: { rows: Row[]; heading: string }) {
  const total = rows.reduce((sum, row) => sum + Number(row.cost_usd), 0)
  const calls = rows.reduce((sum, row) => sum + row.calls, 0)
  return (
    <Table>
      <thead>
        <tr>
          <Th>{heading}</Th>
          <Th right>Calls</Th>
          <Th right>Cost</Th>
          <Th right>Share</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key}>
            <Td className="text-slate-900">{row.label}</Td>
            <Td right>{count(row.calls)}</Td>
            <Td right>{cost(row.cost_usd)}</Td>
            <Td right className="text-slate-500">
              {total > 0 ? percent(Number(row.cost_usd) / total) : '—'}
            </Td>
          </tr>
        ))}
        <tr>
          <Td className="font-medium text-slate-700">Total</Td>
          <Td right className="font-medium">
            {count(calls)}
          </Td>
          <Td right className="font-medium">
            {cost(total)}
          </Td>
          <Td right />
        </tr>
      </tbody>
    </Table>
  )
}

export function ByKindTable({ rows, days }: { rows: UsageByKind[]; days: number }) {
  return (
    <Card title="By kind of call" subtitle={`The last ${count(days)} days`}>
      {rows.length ? (
        <Rows
          heading="Kind"
          rows={rows.map((row) => ({
            key: row.kind,
            label: kindLabel(row.kind),
            calls: row.calls,
            cost_usd: row.cost_usd,
          }))}
        />
      ) : (
        <Empty title="No model calls yet">
          Spend appears here once a question is asked or a document is extracted.
        </Empty>
      )}
    </Card>
  )
}

export function ByUserTable({ rows, days }: { rows: UsageByUser[]; days: number }) {
  return (
    <Card title="By person" subtitle={`The last ${count(days)} days`}>
      {rows.length ? (
        <Rows
          heading="Person"
          rows={rows.map((row) => ({
            key: row.user_id ?? 'nobody',
            // Spend whose account has since been removed still counts. It is
            // shown as its own line, not folded into somebody else.
            label: row.full_name ?? (row.user_id ? 'Unknown user' : 'Removed account'),
            calls: row.calls,
            cost_usd: row.cost_usd,
          }))}
        />
      ) : (
        <Empty title="Nobody has spent anything yet">
          Each person appears here with their share once they ask a question.
        </Empty>
      )}
    </Card>
  )
}
