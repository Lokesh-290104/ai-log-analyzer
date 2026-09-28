import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { Bucket } from '../types'
import { fmtInt, fmtTime } from '../format'

interface Props {
  buckets: Bucket[]
  bucketMinutes: number | null
}

interface Row {
  start: string
  errors: number
  other: number
  total: number
}

function ChartTooltip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  if (!active || !payload?.length) return null
  const row = payload[0].payload
  return (
    <div className="chart-tooltip">
      <div className="tooltip-title">{fmtTime(row.start, false)} UTC</div>
      <div>
        <span className="swatch swatch-errors" /> Errors <strong>{fmtInt(row.errors)}</strong>
      </div>
      <div>
        <span className="swatch swatch-other" /> Other entries <strong>{fmtInt(row.other)}</strong>
      </div>
      <div className="muted">Total {fmtInt(row.total)}</div>
    </div>
  )
}

/** Entries per time bucket, errors stacked at the baseline so the spike reads at a glance. */
export function TimelineChart({ buckets, bucketMinutes }: Props) {
  const rows: Row[] = buckets.map((b) => ({ start: b.start, errors: b.errors, other: b.count - b.errors, total: b.count }))
  return (
    <figure className="chart">
      <figcaption>
        Log entries per {bucketMinutes ?? '?'} min (UTC)
      </figcaption>
      <div className="chart-box">
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -12 }} barCategoryGap={1}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis
              dataKey="start"
              tickFormatter={(v: string) => fmtTime(v, false)}
              tick={{ fill: 'var(--text-muted)', fontSize: 12 }}
              axisLine={{ stroke: 'var(--grid)' }}
              tickLine={false}
              minTickGap={24}
            />
            <YAxis
              allowDecimals={false}
              tick={{ fill: 'var(--text-muted)', fontSize: 12 }}
              axisLine={false}
              tickLine={false}
              width={48}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ fill: 'var(--hover)' }} />
            <Legend
              verticalAlign="top"
              align="right"
              height={24}
              iconType="square"
              formatter={(value) => <span className="legend-label">{value}</span>}
            />
            {/* Surface-colored stroke = the 2px gap between stacked segments and neighbors. */}
            <Bar dataKey="errors" name="Errors" stackId="a" fill="var(--series-errors)" stroke="var(--surface)" strokeWidth={1} />
            <Bar
              dataKey="other"
              name="Other entries"
              stackId="a"
              fill="var(--series-other)"
              stroke="var(--surface)"
              strokeWidth={1}
              radius={[4, 4, 0, 0]}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <details className="table-view">
        <summary>Show as table</summary>
        <table>
          <thead>
            <tr>
              <th>Bucket start (UTC)</th>
              <th>Errors</th>
              <th>Total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.start}>
                <td>{fmtTime(r.start, false)}</td>
                <td>{fmtInt(r.errors)}</td>
                <td>{fmtInt(r.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  )
}
