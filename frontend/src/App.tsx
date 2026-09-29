import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api'
import type { Analysis, UploadInfo } from './types'
import { UploadPanel } from './components/UploadPanel'
import { OverviewPanel } from './components/OverviewPanel'
import { AskPanel, type AskPanelHandle } from './components/AskPanel'
import { AnswerCard } from './components/AnswerCard'

// The upload id lives in the URL hash so a refresh (or a shared link) reopens the same log.
const readHashId = () => /^#\/u\/([\w-]+)$/.exec(window.location.hash)?.[1] ?? null

export default function App() {
  const [upload, setUpload] = useState<UploadInfo | null>(null)
  // Newest first, as the API returns them.
  const [analyses, setAnalyses] = useState<Analysis[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [pending, setPending] = useState<string | null>(null)
  const [askError, setAskError] = useState<string | null>(null)
  const askPanel = useRef<AskPanelHandle>(null)
  const current = useRef<HTMLDivElement>(null)
  const scrollOnChange = useRef(false)

  const open = useCallback(async (load: Promise<UploadInfo>) => {
    setUploading(true)
    setUploadError(null)
    try {
      const info = await load
      setUpload(info)
      setAnalyses([])
      setAskError(null)
      window.history.replaceState(null, '', `#/u/${info.id}`)
      api.listAnalyses(info.id).then(setAnalyses, () => undefined)
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : 'Upload failed.')
    } finally {
      setUploading(false)
    }
  }, [])

  useEffect(() => {
    // Loading the upload named in the URL is synchronizing with an external system (the server).
    const id = readHashId()
    // oxlint-disable-next-line react/set-state-in-effect
    if (id) void open(api.getUpload(id))
  }, [open])

  // After asking, bring the question (then its answer) into view above the pinned ask bar.
  const latestId = analyses[0]?.id
  useEffect(() => {
    if (scrollOnChange.current) current.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  }, [pending, latestId])

  async function onAsk(question: string) {
    if (!upload) return
    scrollOnChange.current = true
    setPending(question)
    setAskError(null)
    try {
      const analysis = await api.ask(upload.id, question)
      setAnalyses((prev) => [analysis, ...prev])
    } catch (e) {
      setAskError(e instanceof Error ? e.message : 'The question failed.')
      askPanel.current?.restore(question)
    } finally {
      setPending(null)
    }
  }

  function reset() {
    setUpload(null)
    setAnalyses([])
    setUploadError(null)
    setAskError(null)
    scrollOnChange.current = false
    window.history.replaceState(null, '', window.location.pathname)
  }

  // Only the latest exchange is shown; older ones fold into "Earlier questions".
  const latest = pending ? null : (analyses[0] ?? null)
  const earlier = pending ? analyses : analyses.slice(1)

  return (
    <div className={`page${upload ? ' with-ask-bar' : ''}`}>
      <header className="header">
        <div>
          <div className="brand">
            <div className="logo" aria-hidden>
              <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
                <rect x="3" y="11" width="3" height="6" rx="1" fill="#fff" />
                <rect x="8.5" y="4" width="3" height="13" rx="1" fill="#fff" />
                <rect x="14" y="8" width="3" height="9" rx="1" fill="#fde2f0" />
              </svg>
            </div>
            <h1>AI Log Analyzer</h1>
          </div>
          <p className="tagline">
            Ask questions about your logs. <strong>The LLM is the witness, code is the judge:</strong> the model picks
            tools and explains, Python computes every number, and answers with unverifiable numbers are rejected.
          </p>
        </div>
        {upload && (
          <button type="button" className="secondary" onClick={reset}>
            New log
          </button>
        )}
      </header>

      {!upload && (
        <UploadPanel
          busy={uploading}
          onFile={(file) => void open(api.uploadLogs(file, file.name))}
          onText={(text) => void open(api.uploadLogs(new Blob([text], { type: 'text/plain' }), 'pasted.log'))}
          onSample={() => void open(api.uploadSample())}
        />
      )}
      {uploading && (
        <p className="loading" role="status">
          <span className="spinner" aria-hidden /> Parsing and computing stats…
        </p>
      )}
      {uploadError && (
        <p className="error" role="alert">
          {uploadError}
        </p>
      )}

      {upload && (
        <>
          <OverviewPanel upload={upload} />

          <section className="conversation" aria-label="Questions and answers">
            {earlier.length > 0 && (
              <details className="earlier">
                <summary>Earlier questions ({earlier.length})</summary>
                <div className="earlier-list">
                  {earlier.map((a) => (
                    <AnswerCard key={a.id} analysis={a} />
                  ))}
                </div>
              </details>
            )}
            <div ref={current} className="current">
              {pending && (
                <article className="answer pending" aria-label={`Analyzing: ${pending}`}>
                  <div className="question">
                    <span className="muted small">You asked</span>
                    <div>{pending}</div>
                  </div>
                  <p className="loading" role="status">
                    <span className="spinner" aria-hidden /> The agent is choosing tools; code is computing the numbers
                    and checking the answer… (usually 5–30 s)
                  </p>
                </article>
              )}
              {latest && <AnswerCard analysis={latest} />}
              {!pending && !latest && (
                <p className="muted empty-hint">Ask a question below. Each answer shows the tool calls behind it.</p>
              )}
            </div>
          </section>

          <AskPanel
            ref={askPanel}
            busy={pending !== null}
            error={askError}
            showSuggestions={analyses.length === 0 && pending === null}
            onAsk={onAsk}
          />
        </>
      )}

      <footer className="footer muted small">
        FastAPI · PostgreSQL · React · Gemma via the Gemini API ·{' '}
        <a href="https://github.com/Lokesh-290104/ai-log-analyzer">source on GitHub</a>
      </footer>
    </div>
  )
}
