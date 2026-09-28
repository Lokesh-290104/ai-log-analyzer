import { useState } from 'react'

interface Props {
  busy: boolean
  onFile: (file: File) => void
  onText: (text: string) => void
  onSample: () => void
}

const MAX_BYTES = 5_000_000

export function UploadPanel({ busy, onFile, onText, onSample }: Props) {
  const [text, setText] = useState('')
  const [dragging, setDragging] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)

  function pick(file: File | undefined) {
    if (!file) return
    if (file.size > MAX_BYTES) {
      setLocalError('That file is larger than 5 MB. Upload a smaller slice of the log.')
      return
    }
    setLocalError(null)
    onFile(file)
  }

  return (
    <section className="card upload" aria-labelledby="upload-title">
      <h2 id="upload-title">1. Add a log</h2>
      <p className="muted">
        Plain text (<code>2026-09-28T10:00:00Z ERROR [payments] …</code>), JSON lines or logfmt. Up to 5 MB.
      </p>

      <label
        className={`dropzone${dragging ? ' dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          pick(e.dataTransfer.files[0])
        }}
      >
        <input
          type="file"
          accept=".log,.txt,.json,.jsonl,text/plain"
          disabled={busy}
          onChange={(e) => pick(e.target.files?.[0])}
        />
        <span>
          <strong>Choose a file</strong> or drop it here
        </span>
      </label>

      <div className="or">or paste</div>
      <textarea
        aria-label="Paste log lines"
        placeholder="Paste log lines here…"
        rows={5}
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="row">
        <button type="button" disabled={busy || !text.trim()} onClick={() => onText(text)}>
          Analyze pasted text
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onSample}>
          Try the sample incident log
        </button>
      </div>
      {localError && (
        <p className="error" role="alert">
          {localError}
        </p>
      )}
    </section>
  )
}
