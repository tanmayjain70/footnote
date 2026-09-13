import { cost, count, percent } from '../../lib/format'
import type { UsageDay } from '../../lib/types'
import { Table, Td, Th } from '../ui'

/*
 * Fourteen bars, drawn by hand. A charting library would be most of the
 * bundle for one picture, and the picture has one job: is spend flat,
 * climbing or spiking, and how far is any day from the budget line.
 *
 * The scale fits the spend, not the budget. A $5 budget over days that
 * cost cents would squash every bar into the baseline, so when the line
 * would not fit it is stated in words under the chart instead.
 */

const WIDTH = 720
const HEIGHT = 240
const PAD = { top: 30, right: 20, bottom: 30, left: 56 }
/** The rounded corners at the data end of a bar; the baseline end stays square. */
const RADIUS = 3
/** How many times the busiest day the budget may be and still be drawn. */
const LINE_HEADROOM = 4

function toNumber(value: string | number): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

/** `1 Sep`. Built in local time from the parts, so a UTC day never shows as the day before. */
function dayLabel(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number)
  return new Date(year, month - 1, day).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
  })
}

/** Costs carry six decimal places, so nothing finer than a millionth of a dollar is ever drawn. */
const SMALLEST_STEP = 0.000001

/** Round-number gridlines: at most five, stepping by 1, 2, 2.5 or 5 times a power of ten. */
function gridlines(max: number): number[] {
  const magnitude = Math.max(10 ** Math.floor(Math.log10(max / 4)), SMALLEST_STEP)
  // toFixed strips the float noise of 2.5 * 0.001 so the labels can count
  // the step's decimals.
  const candidates = [1, 2, 2.5, 5, 10].map((k) => Number((k * magnitude).toFixed(6)))
  const step = candidates.find((candidate) => max / candidate <= 5) ?? candidates[4]
  const steps = Math.max(1, Math.ceil(max / step - 1e-9))
  return Array.from({ length: steps + 1 }, (_, i) => Number((i * step).toFixed(6)))
}

/** As many decimals as the step has, so a step of 2.5 reads `$7.5`, not `$8`. */
function tickLabel(value: number, step: number): string {
  const decimals = Math.min(6, (String(step).split('.')[1] ?? '').length)
  return `$${value.toFixed(decimals)}`
}

/** Short enough to sit over a bar: two decimals from a dollar up, the fraction of a cent below it. */
function barLabel(value: number): string {
  return value >= 1 ? `$${value.toFixed(2)}` : cost(value)
}

/** A bar with rounded corners at the top only, anchored square to the baseline. */
function barPath(x: number, top: number, width: number, bottom: number): string {
  const height = bottom - top
  if (height <= 0) return ''
  const r = Math.min(RADIUS, height, width / 2)
  return [
    `M${x},${bottom}`,
    `V${top + r}`,
    `Q${x},${top} ${x + r},${top}`,
    `H${x + width - r}`,
    `Q${x + width},${top} ${x + width},${top + r}`,
    `V${bottom}`,
    'Z',
  ].join(' ')
}

export function SpendChart({ days, budget }: { days: UsageDay[]; budget: string }) {
  const values = days.map((day) => toNumber(day.cost_usd))
  const spendMax = Math.max(0, ...values)
  const budgetValue = toNumber(budget)
  // With nothing spent yet the budget is the only number worth drawing.
  const lineFits =
    budgetValue > 0 && (spendMax === 0 || budgetValue <= spendMax * LINE_HEADROOM)
  const domainMax = lineFits ? Math.max(spendMax, budgetValue) : spendMax
  const grid = gridlines(domainMax > 0 ? domainMax : 1)
  const yMax = grid[grid.length - 1]
  const step = grid.length > 1 ? grid[1] : 1

  const plotWidth = WIDTH - PAD.left - PAD.right
  const plotHeight = HEIGHT - PAD.top - PAD.bottom
  const baseline = PAD.top + plotHeight
  const slot = plotWidth / Math.max(days.length, 1)
  const barWidth = Math.min(32, slot * 0.62)
  const y = (value: number) => baseline - (value / yMax) * plotHeight
  const todayIndex = days.length - 1
  const totalCalls = days.reduce((sum, day) => sum + day.calls, 0)

  return (
    <div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full"
        role="img"
        aria-label={`Model spend by day over the last ${days.length} days, ${count(totalCalls)} calls in all`}
      >
        {grid.map((value) => (
          <g key={value}>
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={y(value)}
              y2={y(value)}
              strokeWidth={1}
              className={value === 0 ? 'stroke-slate-300' : 'stroke-slate-200'}
            />
            <text
              x={PAD.left - 8}
              y={y(value)}
              dy="0.35em"
              textAnchor="end"
              className="tabular fill-slate-500 text-[10px]"
            >
              {tickLabel(value, step)}
            </text>
          </g>
        ))}

        {days.map((day, i) => {
          const value = values[i]
          const x = PAD.left + i * slot + (slot - barWidth) / 2
          const top = y(value)
          const today = i === todayIndex
          // Every other day is labelled, counted back from today so today
          // always is.
          const labelled = (todayIndex - i) % 2 === 0
          const calls = `${count(day.calls)} call${day.calls === 1 ? '' : 's'}`
          return (
            <g key={day.day}>
              <title>{`${dayLabel(day.day)}: ${cost(value)} across ${calls}`}</title>
              <rect
                x={PAD.left + i * slot}
                y={PAD.top}
                width={slot}
                height={plotHeight}
                className="fill-transparent hover:fill-slate-100"
              />
              {value > 0 && (
                <path
                  d={barPath(x, top, barWidth, baseline)}
                  className={today ? 'fill-brand-700' : 'fill-brand-500'}
                />
              )}
              {value > 0 && (
                <text
                  x={x + barWidth / 2}
                  y={top - 5}
                  textAnchor="middle"
                  className="tabular fill-slate-600 text-[10px]"
                >
                  {barLabel(value)}
                </text>
              )}
              {labelled && (
                <text
                  x={x + barWidth / 2}
                  y={baseline + 16}
                  textAnchor="middle"
                  className={
                    today ? 'fill-slate-800 text-[10px] font-medium' : 'fill-slate-500 text-[10px]'
                  }
                >
                  {today ? 'Today' : dayLabel(day.day)}
                </text>
              )}
            </g>
          )
        })}

        {lineFits && (
          <g>
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={y(budgetValue)}
              y2={y(budgetValue)}
              strokeWidth={1.5}
              strokeDasharray="5 4"
              className="stroke-note-600"
            />
            <text
              x={WIDTH - PAD.right}
              y={y(budgetValue) - 5}
              textAnchor="end"
              className="fill-note-700 text-[10px] font-medium"
            >
              Daily budget {cost(budgetValue)}
            </text>
          </g>
        )}
      </svg>

      <p className="mt-2 text-xs text-slate-500">
        {budgetValue <= 0
          ? 'The daily budget is $0.00, so every question is refused until it is raised.'
          : lineFits
            ? 'The dashed line is the daily budget. Days are UTC days, because that is when the budget resets.'
            : `The daily budget of ${cost(budgetValue)} sits above this chart: the busiest day spent ${percent(spendMax / budgetValue, 1)} of it. Days are UTC days, because that is when the budget resets.`}
      </p>

      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-slate-600 hover:text-slate-800">
          Show the same figures as a table
        </summary>
        <div className="mt-2 px-5">
          <Table>
            <thead>
              <tr>
                <Th>Day</Th>
                <Th right>Calls</Th>
                <Th right>Cost</Th>
              </tr>
            </thead>
            <tbody>
              {days.map((day, i) => (
                <tr key={day.day}>
                  <Td className="whitespace-nowrap">
                    {dayLabel(day.day)}
                    {i === todayIndex ? ' (today)' : ''}
                  </Td>
                  <Td right>{count(day.calls)}</Td>
                  <Td right>{cost(day.cost_usd)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      </details>
    </div>
  )
}
