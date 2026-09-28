import type { Analysis } from '../types'
import { TraceView } from './TraceView'

function renderAnswer(text: string) {
  // The model may use simple bullets; render lines without interpreting HTML.
  return text.split('\n').map((line, i) => (
    <p key={i} className={line.trim().startsWith('* ') || line.trim().startsWith('- ') ? 'bullet' : undefined}>
      {line.replace(/^\s*[*-]\s+/, '').replace(/\*\*/g, '').replace(/`/g, '')}
    </p>
  ))
}

export function AnswerCard({ analysis }: { analysis: Analysis }) {
  const v = analysis.verification
  const evidence = v?.evidence ?? []
  return (
    <article className={`answer ${analysis.status}`} aria-label={`Answer to: ${analysis.question}`}>
      <div className="question">
        <span className="muted small">You asked</span>
        <div>{analysis.question}</div>
      </div>

      {analysis.status === 'answered' && analysis.answer ? (
        <>
          <div className="answer-text">{renderAnswer(analysis.answer)}</div>
          <div className="verdict ok" role="status">
            <span aria-hidden>✓</span> Verified:{' '}
            {v && v.checked_numbers.length > 0
              ? `${v.checked_numbers.length} number${v.checked_numbers.length === 1 ? '' : 's'}/time${
                  v.checked_numbers.length === 1 ? '' : 's'
                } (${v.checked_numbers.join(', ')}) found in tool result${evidence.length === 1 ? '' : 's'} ${evidence
                  .map((id) => `#${id}`)
                  .join(', ')}`
              : 'no figures to check'}
          </div>
        </>
      ) : (
        <div className="verdict failed" role="alert">
          <span aria-hidden>⚠</span> <strong>No answer shown (failed closed).</strong> {analysis.error}
        </div>
      )}

      <details className="evidence" open={analysis.status === 'failed'}>
        <summary>
          Evidence: {analysis.trace.filter((t) => t.kind === 'tool_call').length} tool call(s) ·{' '}
          {analysis.llm_calls} model call(s) · {(analysis.duration_ms / 1000).toFixed(1)} s · {analysis.model}
        </summary>
        <TraceView trace={analysis.trace} evidence={evidence} />
      </details>
    </article>
  )
}
