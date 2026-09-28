import { useCallback, useEffect, useState } from 'react'
import * as api from './api'
import type { Analysis, UploadInfo } from './types'
import { UploadPanel } from './components/UploadPanel'
import { OverviewPanel } from './components/OverviewPanel'
import { AskPanel } from './components/AskPanel'
import { AnswerCard } from './components/AnswerCard'

// The upload id lives in the URL hash so a refresh (or a shared link) reopens the same log.
const readHashId = () => /^#\/u\/([\w-]+)$/.exec(window.location.hash)?.[1] ?? null

export default function App() {
  const [upload, setUpload] = useState<UploadInfo | null>(null)
  const [analyses, setAnalyses] = useState<Analysis[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [asking, setAsking] = useState(false)
  const [askError, setAskError] = useState<string | null>(null)

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

  async function onAsk(question: string) {
    if (!upload) return
    setAsking(true)
    setAskError(null)
    try {
      const analysis = await api.ask(upload.id, question)
      setAnalyses((prev) => [analysis, ...prev])
    } catch (e) {
      setAskError(e instanceof Error ? e.message : 'The question failed.')
    } finally {
      setAsking(false)
    }
  }

  function reset() {
    setUpload(null)
    setAnalyses([])
    setUploadError(null)
    window.history.replaceState(null, '', window.location.pathname)
  }

  return (
    <div className="page">
      <header className="header">
        <div>
          <h1>AI Log Analyzer</h1>
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
          <AskPanel busy={asking} error={askError} onAsk={onAsk} />
          {analyses.length > 0 && (
            <section className="answers" aria-label="Answers">
              {analyses.map((a) => (
                <AnswerCard key={a.id} analysis={a} />
              ))}
            </section>
          )}
        </>
      )}

      <footer className="footer muted small">
        FastAPI · PostgreSQL · React · Gemma via the Gemini API ·{' '}
        <a href="https://github.com/Lokesh-290104/ai-log-analyzer">source on GitHub</a>
      </footer>
    </div>
  )
}
