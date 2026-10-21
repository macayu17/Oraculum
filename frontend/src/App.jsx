import { useEffect, useRef, useState } from 'react'

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '')

const iconPaths = {
  mark: <><path d="M4 5.5h16v13H4z"/><path d="m9 10-2 2 2 2m6-4 2 2-2 2m-4 1 2-6"/></>,
  upload: <><path d="M12 16V4m-5 5 5-5 5 5"/><path d="M5 14v5h14v-5"/></>,
  arrow: <><path d="M5 12h14m-6-6 6 6-6 6"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  file: <><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M9 13h6m-6 3h6"/></>,
  book: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 1 4 17.5z"/><path d="M4 17.5A2.5 2.5 0 0 1 6.5 15H20M8 7h7"/></>,
  chat: <><path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 9 9 0 0 1-4-.9L4 20l1.5-3.4A7.4 7.4 0 0 1 4 12c0-4.2 3.6-7.5 8-7.5s8 2.8 8 7.5Z"/><path d="M8 12h.01M12 12h.01M16 12h.01"/></>,
  chevron: <path d="m7 10 5 5 5-5"/>,
}

function Icon({ name, size = 18 }) {
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{iconPaths[name]}</svg>
}

function formatBytes(bytes) {
  return bytes < 1048576 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1048576).toFixed(1)} MB`
}

function FileList({ files, kind, onRemove }) {
  if (!files.length) return null
  const label = kind === 'papers' ? 'Question papers' : 'Reference material'
  return <ul className="file-list" aria-label={label}>
    {files.map((file, index) => <li className="file-row" key={`${file.name}-${index}`}>
      <Icon name={kind === 'papers' ? 'file' : 'book'} size={17}/>
      <span className="file-name">{file.name}</span>
      <span className="file-size">{formatBytes(file.size)}</span>
      <button className="icon-button" type="button" onClick={() => onRemove(kind, index)} aria-label={`Remove ${file.name}`}><Icon name="close" size={16}/></button>
    </li>)}
  </ul>
}

export default function App() {
  const paperInput = useRef(null)
  const materialInput = useRef(null)
  const [papers, setPapers] = useState([])
  const [materials, setMaterials] = useState([])
  const [syllabus, setSyllabus] = useState('')
  const [report, setReport] = useState(null)
  const [tab, setTab] = useState('revise')
  const [busy, setBusy] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')
  const [threadId, setThreadId] = useState('')
  const [messages, setMessages] = useState([])
  const [question, setQuestion] = useState('')
  const [sending, setSending] = useState(false)
  const [apiReady, setApiReady] = useState(null)

  useEffect(() => {
    fetch(`${API_BASE_URL}/api/health`).then(response => response.json()).then(data => setApiReady(Boolean(data.ready))).catch(() => setApiReady(false))
  }, [])

  useEffect(() => {
    if (!busy) return
    const started = Date.now()
    setElapsed(0)
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [busy])

  function addFiles(fileList, kind) {
    const files = Array.from(fileList)
    const accepted = files.filter(file => /\.(pdf|png|jpe?g)$/i.test(file.name))
    if (accepted.length !== files.length) setError('Choose PDF, PNG, or JPEG files.')
    if (kind === 'papers') setPapers(current => [...current, ...accepted].slice(0, 12))
    else setMaterials(current => [...current, ...accepted].slice(0, 8))
  }

  function removeFile(kind, index) {
    if (kind === 'papers') setPapers(files => files.filter((_, i) => i !== index))
    else setMaterials(files => files.filter((_, i) => i !== index))
  }

  async function analyze(event) {
    event.preventDefault()
    setError('')
    if (!papers.length) return setError('Add at least one past paper to begin.')
    setBusy(true)
    setReport(null)
    setMessages([])
    try {
      const body = new FormData()
      papers.forEach(file => body.append('papers', file))
      materials.forEach(file => body.append('materials', file))
      body.append('syllabus', syllabus)
      const response = await fetch(`${API_BASE_URL}/api/analyze`, { method: 'POST', body })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Analysis could not be completed.')
      let result = data
      while (result.status === 'processing') {
        await new Promise(resolve => setTimeout(resolve, 2000))
        const statusResponse = await fetch(`${API_BASE_URL}/api/analyze/${data.job_id}`)
        const status = await statusResponse.json()
        if (!statusResponse.ok) throw new Error(status.detail || 'Analysis status could not be retrieved.')
        if (status.status === 'failed') throw new Error(status.detail || 'Analysis could not be completed.')
        if (status.status === 'complete') result = status.result
        else result = status
      }
      setReport(result)
      setThreadId(result.thread_id)
      document.getElementById('revision-map')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch (cause) {
      setError(cause instanceof TypeError ? 'Connection to the analysis service was interrupted. Check your connection and try again.' : cause.message || 'Something went wrong. Please try again.')
    } finally { setBusy(false) }
  }

  async function sendMessage(event) {
    event.preventDefault()
    const text = question.trim()
    if (!text || !threadId || sending) return
    setQuestion('')
    setMessages(current => [...current, { role: 'user', content: text }])
    setSending(true)
    setError('')
    try {
      const response = await fetch(`${API_BASE_URL}/api/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ thread_id: threadId, message: text }) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'The tutor could not answer right now.')
      setMessages(current => [...current, { role: 'assistant', content: data.reply }])
    } catch (cause) {
      setError(cause.message || 'The tutor could not answer right now.')
    } finally { setSending(false) }
  }

  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to workspace</a>
    <header className="topbar" id="top">
      <a className="wordmark" href="#top" aria-label="Oraculum home"><span className="wordmark-icon"><Icon name="mark" size={19}/></span><span>ORACULUM<span className="wordmark-dot">.</span></span></a>
      <div className="topbar-right"><span className="topbar-section">PAPER ANALYSIS <span>/</span> WORKSPACE</span><span className="model-state"><i className={`status-dot ${apiReady === false ? 'status-needed' : ''}`}/>{apiReady === false ? 'MODEL UNAVAILABLE' : apiReady === null ? 'CHECKING MODEL' : 'NEMOTRON 3.5 · SUPER VL'}</span></div>
    </header>

    <main className="workspace" id="main">
      <section className="masthead">
        <div className="masthead-copy"><p className="eyebrow"><span>PAST PAPER ANALYSIS</span><span className="eyebrow-divider">/</span><span>01</span></p><h1>See what <em>comes back.</em></h1><p className="intro-lede">Compare exam years. Find the questions and themes that deserve another look.</p></div>
        <div className="masthead-mark" aria-hidden="true"><span>FIELD<br/>RECORD</span><b>01</b><i>✳</i></div>
      </section>

      <section className="workspace-grid" aria-label="Paper analysis workspace">
        <form className="ingest-panel panel" onSubmit={analyze}>
          <div className="panel-heading"><div><p className="section-kicker">01 <span>/</span> THE ARCHIVE</p><h2>Start with past papers</h2><p className="panel-subtitle">Add the exams you want to compare.</p></div><span className="quiet-count">{String(papers.length).padStart(2, '0')} <i>/ 12</i></span></div>
          <input ref={paperInput} className="visually-hidden" type="file" accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" multiple onChange={event => addFiles(event.target.files, 'papers')}/>
          <div className={`dropzone ${dragging ? 'dragging' : ''}`} onDragOver={event => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files, 'papers') }}>
            <span className="drop-icon"><Icon name="upload" size={21}/></span><div className="drop-copy"><strong>Drop question papers here</strong><span>PDF, PNG or JPEG <i>·</i> 20 MB max per file</span></div><button className="outline-button" type="button" onClick={() => paperInput.current?.click()}>Choose files <Icon name="arrow" size={16}/></button>
          </div>
          <FileList files={papers} kind="papers" onRemove={removeFile}/>
          <div className="archive-footer"><span>QUESTIONS, MARKS, STRUCTURE</span><span>UP TO 12 PAPERS</span></div>
          {busy && <p className="analysis-status" role="status">Analyzing {papers.length} paper{papers.length === 1 ? '' : 's'} · {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, '0')} elapsed. Reading scans and comparing questions can take a few minutes.</p>}
          {error && !report && <p className="error-message" role="alert">{error}</p>}
          <div className="submit-row"><p><span className="privacy-dot"/>{papers.length ? `${papers.length} paper${papers.length === 1 ? "" : "s"} ready to compare.` : "Choose files, then compare."}</p><button className="primary-button" type="submit" disabled={busy}>{busy ? <><span className="spinner"/> Reading papers…</> : <>Compare papers <Icon name="arrow" size={17}/></>}</button></div>
        </form>

        <section className="context-panel panel" aria-labelledby="context-title">
          <div className="panel-heading"><div><p className="section-kicker">02 <span>/</span> EXAM CONTEXT</p><h2 id="context-title">Give it the syllabus</h2><p className="panel-subtitle">Optional context keeps the analysis on course.</p></div><Icon name="book" size={20}/></div>
          <label className="field-label" htmlFor="syllabus">Subject, board or units <span>OPTIONAL</span></label>
          <textarea id="syllabus" className="syllabus-input" placeholder="e.g. Grade 12 biology · cell division and genetics" value={syllabus} onChange={event => setSyllabus(event.target.value)} rows="3"/>
          <div className="material-heading"><span>TEXTBOOK OR CLASS NOTES</span><span>OPTIONAL</span></div>
          <input ref={materialInput} className="visually-hidden" type="file" accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" multiple onChange={event => addFiles(event.target.files, 'materials')}/>
          <button type="button" className="reference-button" onClick={() => materialInput.current?.click()}><span className="reference-plus"><Icon name="plus" size={16}/></span><span>Add reference material</span><span className="reference-limit">UP TO 8</span></button>
          <FileList files={materials} kind="materials" onRemove={removeFile}/>
          <div className="context-note"><span className="note-stamp">NOTE</span><p>Every revision priority links back to the paper it came from.</p></div>
        </section>
      </section>

      <section className={`results-section ${report ? 'has-report' : ''}`} id="revision-map" aria-live="polite">
        {report ? <>
          <div className="results-heading"><div><p className="section-kicker">03 <span>/</span> REVISION MAP</p><h2>What keeps coming back</h2></div><div className="result-meta"><span>{report.paper_count} paper{report.paper_count === 1 ? '' : 's'}</span><b>·</b><span>{report.questions.length} questions</span></div></div>
          {report.summary && <p className="report-summary">{report.summary}</p>}
          <div className="results-tabs" role="tablist" aria-label="Analysis views">{[['revise', 'Revise first'], ['patterns', 'Question patterns'], ['practice', 'Practice set']].map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? 'selected' : ''} onClick={() => setTab(id)}>{label}<span>{id === 'revise' ? report.topics.length : id === 'patterns' ? report.patterns?.length || 0 : report.practice?.length || 0}</span></button>)}</div>
          <div className="result-list">
            {tab === 'revise' && report.topics.map((topic, index) => <article className="topic-row" key={`${topic.concept}-${index}`}><span className="topic-rank">{String(index + 1).padStart(2, '0')}</span><div className="topic-main"><h3>{topic.concept}</h3><p>{topic.count} appearance{topic.count === 1 ? '' : 's'} <i>·</i> {topic.papers.join(', ')}</p></div><span className={`priority priority-${topic.priority.toLowerCase()}`}>{topic.priority}</span><details className="source-details"><summary aria-label={`Show questions for ${topic.concept}`}><Icon name="chevron" size={17}/></summary><div className="source-content">{topic.questions.map((item, itemIndex) => <div className="source-question" key={itemIndex}><span>{item.paper}{item.page ? ` · page ${item.page}` : ''}</span><p>{item.question}</p></div>)}</div></details></article>)}
            {tab === 'patterns' && report.patterns?.map((pattern, index) => <article className="insight-row" key={index}><span className="topic-rank">{String(index + 1).padStart(2, '0')}</span><p>{pattern}</p></article>)}
            {tab === 'practice' && report.practice?.map((prompt, index) => <article className="insight-row practice-row" key={index}><span className="topic-rank">{String(index + 1).padStart(2, '0')}</span><p>{prompt}</p><span className="practice-label">PRACTICE</span></article>)}
          </div>
          <p className="results-caveat">Priorities come from the papers you supplied. Practice questions help you revise; they are not exam predictions.</p>
        </> : <div className="empty-results"><div className="empty-copy"><p className="section-kicker">03 <span>/</span> REVISION MAP</p><h2>The next paper is unknown.<br/><em>Your past papers aren’t.</em></h2><p>Add papers above to see recurring questions, concepts and the sources behind each priority.</p></div><div className="empty-index"><div><span>01</span><b>REPEATS</b><small>Question and concept recurrence</small></div><div><span>02</span><b>STRUCTURE</b><small>Marks, format and topic coverage</small></div><div><span>03</span><b>PRACTICE</b><small>New prompts based on your syllabus</small></div></div></div>}
      </section>

      <section className={`tutor-section ${report ? 'unlocked' : ''}`} id="tutor">
        <div className="tutor-heading"><span className="tutor-glyph"><Icon name="chat" size={19}/></span><div><p className="section-kicker">04 <span>/</span> STUDY CHAT</p><h2>Ask about the material</h2></div><span className="tutor-status">{report ? 'SESSION OPEN' : 'AVAILABLE AFTER ANALYSIS'}</span></div>
        <div className="tutor-body"><div className="tutor-prompt"><p>{report ? 'Take a question further.' : 'A study chat grounded in your papers.'}</p><span>{report ? 'Ask for an explanation, a worked example or a source reference.' : 'Analysis opens a tutor that can point to your uploaded papers and notes.'}</span></div><div className="chat-area">{messages.length > 0 && <div className="chat-messages">{messages.map((message, index) => <div className={`chat-message ${message.role}`} key={index}><span className="chat-role">{message.role === 'user' ? 'YOU' : 'ORACULUM'}</span><p>{message.content}</p></div>)}{sending && <div className="chat-message assistant"><span className="chat-role">ORACULUM</span><p>Thinking through your material…</p></div>}</div>}
          <form className="chat-form" onSubmit={sendMessage}><label className="visually-hidden" htmlFor="chat-question">Ask about your papers or study notes</label><input id="chat-question" value={question} onChange={event => setQuestion(event.target.value)} placeholder={report ? 'Ask a question about your papers…' : 'Analyse your papers to begin…'} disabled={!report || sending} maxLength="4000"/><button type="submit" aria-label="Send question" disabled={!report || sending || !question.trim()}><Icon name="arrow" size={18}/></button></form>{report && error && <p className="error-message" role="alert">{error}</p>}</div></div>
      </section>
    </main>
    <footer className="site-footer"><span>ORACULUM <i>·</i> PAST QUESTIONS, BETTER REVISION.</span><span>YOUR PAPERS ARE THE EVIDENCE.</span></footer>
  </div>
}
