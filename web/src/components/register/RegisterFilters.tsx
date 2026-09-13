import type { MePortfolio } from '../../lib/types'
import { Button, Card, Input, Select } from '../ui'
import type { RegisterFilters } from './filters'

/*
 * One row of controls. The two checkboxes are the ones that change what the
 * numbers mean, not just which rows show: "include unreviewed" puts values
 * on screen that nobody has confirmed, and the table marks every one of them
 * so the reader can tell.
 */
export function RegisterFilterBar({
  filters,
  portfolios,
  onChange,
  onExport,
  exporting,
  refreshing,
}: {
  filters: RegisterFilters
  portfolios: MePortfolio[]
  onChange: (next: RegisterFilters) => void
  onExport: () => void
  exporting: boolean
  refreshing: boolean
}) {
  const set = <K extends keyof RegisterFilters>(key: K, value: RegisterFilters[K]) =>
    onChange({ ...filters, [key]: value })

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <Select
          value={filters.portfolio}
          onChange={(e) => set('portfolio', e.target.value)}
          aria-label="Portfolio"
        >
          <option value="">All portfolios</option>
          {portfolios.map((option) => (
            <option key={option.id} value={option.id}>
              {option.name}
              {option.confidential ? ' (confidential)' : ''}
            </option>
          ))}
        </Select>

        <label className="flex items-center gap-2 text-sm text-slate-600">
          <span className="whitespace-nowrap">Expiring before</span>
          <Input
            type="date"
            value={filters.expiringBefore}
            onChange={(e) => set('expiringBefore', e.target.value)}
            className="w-40"
            aria-label="Term ends before"
          />
        </label>

        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={filters.hasBreak}
            onChange={(e) => set('hasBreak', e.target.checked)}
            className="h-4 w-4 rounded border-slate-300 accent-brand-600"
          />
          Has a break clause
        </label>

        <label
          className="flex items-center gap-2 text-sm text-slate-700"
          title="Show values the model extracted that nobody has confirmed yet. They are marked."
        >
          <input
            type="checkbox"
            checked={filters.includeUnreviewed}
            onChange={(e) => set('includeUnreviewed', e.target.checked)}
            className="h-4 w-4 rounded border-slate-300 accent-brand-600"
          />
          Include unreviewed values
        </label>

        {refreshing && <span className="text-xs text-slate-400">Refreshing…</span>}

        <div className="ml-auto">
          <Button
            size="sm"
            onClick={onExport}
            disabled={exporting}
            title="The same rows and filters as the table, as a CSV file"
          >
            {exporting ? 'Preparing…' : 'Export CSV'}
          </Button>
        </div>
      </div>
    </Card>
  )
}
