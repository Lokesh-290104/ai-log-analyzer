import type { TraceItem } from '../types'

const STAGE_LABEL = {
  schema: 'Invalid reply rejected by schema',
  tool: 'Invalid tool call rejected',
  judge: 'Answer rejected by the judge',
} as const

/** Every step the agent took: tool calls with their exact args and results, and rejections. */
export function TraceView({ trace, evidence }: { trace: TraceItem[]; evidence: number[] }) {
  if (trace.length === 0) {
    return <p className="muted small">No tools were called.</p>
  }
  return (
    <ol className="trace" aria-label="Agent steps">
      {trace.map((item, index) =>
        item.kind === 'tool_call' ? (
          <li key={index} className={`step tool${evidence.includes(item.id) ? ' cited' : ''}`}>
            <div className="step-head">
              <span className="step-id">#{item.id}</span>
              <code className="tool-name">{item.tool}</code>
              <code className="args">{JSON.stringify(item.args)}</code>
              {evidence.includes(item.id) && <span className="badge">cited</span>}
              <span className="muted small">{item.ms} ms</span>
            </div>
            {item.reason && <div className="muted small">{item.reason}</div>}
            <details>
              <summary>Result (computed by Python)</summary>
              <pre>{JSON.stringify(item.result, null, 2)}</pre>
            </details>
          </li>
        ) : (
          <li key={index} className="step rejected">
            <div className="step-head">
              <span className="step-id" aria-hidden>
                ✕
              </span>
              <strong>{STAGE_LABEL[item.stage]}</strong>
            </div>
            <div className="small">{item.problem}</div>
            {item.answer && <div className="small muted">Rejected answer: “{item.answer}”</div>}
          </li>
        ),
      )}
    </ol>
  )
}
