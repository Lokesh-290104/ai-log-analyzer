import { useState } from 'react'

const SUGGESTIONS = [
  'Which service has the most errors?',
  'When did the first database timeout happen?',
  'When was the error spike at its peak?',
  'What are the top 3 error messages?',
]

interface Props {
  busy: boolean
  error: string | null
  onAsk: (question: string) => void
}

export function AskPanel({ busy, error, onAsk }: Props) {
  const [question, setQuestion] = useState('')
  const trimmed = question.trim()

  function submit(q: string) {
    if (busy || q.trim().length < 3) return
    onAsk(q.trim())
  }

  return (
    <section className="card" aria-labelledby="ask-title">
      <h2 id="ask-title">3. Ask a question</h2>
      <form
        className="ask-form"
        onSubmit={(e) => {
          e.preventDefault()
          submit(question)
        }}
      >
        <input
          aria-label="Question about the log"
          placeholder="e.g. What happened between 10:40 and 10:50?"
          maxLength={500}
          value={question}
          disabled={busy}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <button type="submit" disabled={busy || trimmed.length < 3}>
          {busy ? 'Analyzing…' : 'Ask'}
        </button>
      </form>
      <div className="chips">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            className="chip"
            disabled={busy}
            onClick={() => {
              setQuestion(s)
              submit(s)
            }}
          >
            {s}
          </button>
        ))}
      </div>
      {busy && (
        <p className="loading" role="status">
          <span className="spinner" aria-hidden /> The agent is choosing tools; code is computing the numbers and
          checking the answer… (usually 5–20 s)
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
