import { forwardRef, useImperativeHandle, useRef, useState } from 'react'

// Generic on purpose: they must make sense for any uploaded log, not just the sample.
const SUGGESTIONS = [
  'What is causing the errors?',
  'Which service has the most errors?',
  'When did the errors start?',
  'When was the error spike at its peak?',
]

interface Props {
  busy: boolean
  error: string | null
  showSuggestions: boolean
  onAsk: (question: string) => void
}

export interface AskPanelHandle {
  /** Put a question back in the box (e.g. after a failed request) and focus it. */
  restore: (question: string) => void
}

/** The question box, pinned to the bottom of the screen so it's always reachable. */
export const AskPanel = forwardRef<AskPanelHandle, Props>(function AskPanel(
  { busy, error, showSuggestions, onAsk },
  ref,
) {
  const [question, setQuestion] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const trimmed = question.trim()

  useImperativeHandle(ref, () => ({
    restore(q: string) {
      setQuestion(q)
      input.current?.focus()
    },
  }))

  function submit(q: string) {
    if (busy || q.trim().length < 3) return
    setQuestion('')
    onAsk(q.trim())
  }

  return (
    <div className="ask-bar">
      <div className="ask-inner">
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {showSuggestions && (
          <div className="chips">
            {SUGGESTIONS.map((s) => (
              <button key={s} type="button" className="chip" disabled={busy} onClick={() => submit(s)}>
                {s}
              </button>
            ))}
          </div>
        )}
        <form
          className="ask-form"
          onSubmit={(e) => {
            e.preventDefault()
            submit(question)
          }}
        >
          <input
            ref={input}
            aria-label="Question about the log"
            placeholder="Ask about this log, e.g. What is causing the errors?"
            maxLength={500}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <button type="submit" disabled={busy || trimmed.length < 3}>
            {busy ? 'Analyzing…' : 'Ask'}
          </button>
        </form>
      </div>
    </div>
  )
})
