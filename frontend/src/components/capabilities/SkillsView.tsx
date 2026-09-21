/* Skills: Markdown protocols that go into the model's system prompt. */

import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { skillsApi } from '../../api/skills'
import { getConfig } from '../../config'
import { useSessionStore } from '../../store'
import type { Skill } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'

export default function SkillsView() {
  const admin = getConfig().features.admin
  const adminToken = useSessionStore(s => s.adminToken)
  const canEdit = admin && Boolean(adminToken)
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState('')
  const [view, setView] = useState<Skill | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try { setSkills(await skillsApi.list()); setNotice('') }
    catch (e: unknown) { setNotice(`Cannot list skills: ${getErrorMessage(e)}`) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const guard = async (action: () => Promise<unknown>) => {
    try { await action(); await refresh() }
    catch (e: unknown) { setNotice(getErrorMessage(e)) }
  }

  const upload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const text = await file.text()
    const fm = text.match(/^---\s*\n([\s\S]*?)\n---\s*\n/)
    const meta: Record<string, string> = {}
    let content = text
    if (fm) {
      for (const line of fm[1].split('\n')) {
        const m = line.match(/^(\w+)\s*:\s*(.+)/)
        if (m) meta[m[1]] = m[2].replace(/^["']|["']$/g, '')
      }
      content = text.slice(fm[0].length)
    }
    await guard(() => skillsApi.create({
      name: meta.name || file.name.replace(/\.md$/, ''),
      description: meta.description || '',
      author: meta.author || '',
      content,
    }))
    if (fileRef.current) fileRef.current.value = ''
  }

  const builtin = skills.filter(s => s.readonly).length
  const custom = skills.length - builtin
  const active = skills.filter(s => s.enabled).length

  return (
    <div className="space-y-4">
      <p className="section-copy small">
        {builtin} built-in · {custom} custom · {active} active. Every enabled skill is appended to the system prompt.
        {admin && !adminToken && ' Enter the admin password on the Connect page to add or change skills.'}
        {!admin && ' This host has no admin password configured; skills are read-only here.'}
      </p>

      {canEdit && (
        <div className="flex gap-2 flex-wrap">
          <button className="btn-primary" onClick={() => setShowAdd(true)}>+ Add skill</button>
          <button className="btn-secondary" onClick={() => setShowImport(true)}>Import from GitHub</button>
          <button className="btn-secondary" onClick={() => fileRef.current?.click()}>Upload .md</button>
          <input ref={fileRef} type="file" accept=".md" style={{ display: 'none' }} onChange={upload} />
        </div>
      )}

      {notice && <p className="tool-warning-status">{notice}</p>}

      {loading ? (
        <p className="section-copy small">Loading...</p>
      ) : skills.length === 0 ? (
        <div className="empty-state">
          <h3 className="heading-display" style={{ fontSize: 15 }}>No skills yet</h3>
          <p className="section-copy">
            The package's own skills are listed unless <code>SKILLS_DIR</code> points at a directory without any;
            unset it to get them back, or add company standards and personal preferences here. They shape how the
            model asks and answers.
          </p>
        </div>
      ) : (
        <div className="card-grid">
          {skills.map(s => (
            <div key={s.id} className={`skill-card${s.enabled ? '' : ' disabled'}`} onClick={async () => {
              try { setView(await skillsApi.get(s.id)) } catch (e: unknown) { setNotice(getErrorMessage(e)) }
            }}>
              {s.readonly
                ? <span className="skill-badge">BUILT-IN</span>
                : canEdit && (
                  <button className={`toggle${s.enabled ? ' on' : ''}`} aria-label="Enable or disable"
                    onClick={e => { e.stopPropagation(); guard(() => skillsApi.toggle(s.id, !s.enabled)) }}>
                    <span />
                  </button>
                )}
              <div className="skill-name">{s.name}</div>
              <p className="skill-desc">{s.description || 'No description'}</p>
              <div className="skill-meta">
                <span className={`pill ${s.source}`}>{s.source}</span>
                {s.layer && <span className="pill">{s.layer}</span>}
                {s.version && s.version !== '-' && <span>v{s.version}</span>}
                {s.author && <span>by {s.author}</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {view && (
        <Modal onClose={() => setView(null)} width={760}>
          <div className="modal-head">
            <div>
              <h3 className="heading-display" style={{ fontSize: 16 }}>{view.name}</h3>
              <p className="section-copy small">{view.source}{view.layer ? ` / ${view.layer}` : ''}{view.version && view.version !== '-' ? ` · v${view.version}` : ''}{view.author ? ` · ${view.author}` : ''}</p>
            </div>
            <div className="flex gap-2">
              {!view.readonly && canEdit && (
                <button className="btn-ghost danger" onClick={() => {
                  if (window.confirm(`Delete skill '${view.name}'?`)) guard(() => skillsApi.remove(view.id)).then(() => setView(null))
                }}>Delete</button>
              )}
              <button className="btn-ghost" onClick={() => setView(null)}>Close</button>
            </div>
          </div>
          {view.description && <p className="section-copy">{view.description}</p>}
          <div className="markdown-body skill-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{view.content || '(empty)'}</ReactMarkdown></div>
        </Modal>
      )}

      {showAdd && <AddSkillModal onClose={() => setShowAdd(false)} onSaved={() => { setShowAdd(false); refresh() }} />}
      {showImport && <ImportSkillModal onClose={() => setShowImport(false)} onDone={() => { setShowImport(false); refresh() }} />}
    </div>
  )
}

function Modal({ children, onClose, width }: { children: React.ReactNode; onClose: () => void; width: number }) {
  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={{ ...modalStyle, maxWidth: width }} onClick={e => e.stopPropagation()}>{children}</div>
    </div>
  )
}

function AddSkillModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [author, setAuthor] = useState('')
  const [content, setContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const save = async () => {
    if (!name.trim()) return
    setSaving(true); setError('')
    try { await skillsApi.create({ name, description: desc, author, content }); onSaved() }
    catch (e: unknown) { setError(getErrorMessage(e)) }
    finally { setSaving(false) }
  }

  return (
    <Modal onClose={onClose} width={680}>
      <h3 className="heading-display" style={{ fontSize: 14, marginBottom: 16 }}>New skill</h3>
      <div className="form-grid">
        <label><span className="label-text">Name *</span><input className="input-field" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. office-layout" /></label>
        <label><span className="label-text">Author</span><input className="input-field" value={author} onChange={e => setAuthor(e.target.value)} /></label>
        <label className="form-wide"><span className="label-text">Description</span><input className="input-field" value={desc} onChange={e => setDesc(e.target.value)} /></label>
        <label className="form-wide"><span className="label-text">Content (Markdown)</span>
          <textarea className="code-editor" value={content} onChange={e => setContent(e.target.value)} placeholder="# Rules the model must follow..." /></label>
      </div>
      {error && <p className="tool-warning-status">{error}</p>}
      <div className="flex justify-end gap-2" style={{ marginTop: 12 }}>
        <button className="btn-secondary" onClick={onClose}>Cancel</button>
        <button className="btn-primary" onClick={save} disabled={saving || !name.trim()}>{saving ? 'Saving...' : 'Save skill'}</button>
      </div>
    </Modal>
  )
}

function ImportSkillModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<Skill | null>(null)

  const run = async () => {
    if (!url.trim()) return
    setBusy(true); setError('')
    try { setResult(await skillsApi.importFromGitHub(url.trim())) }
    catch (e: unknown) { setError(getErrorMessage(e)) }
    finally { setBusy(false) }
  }

  return (
    <Modal onClose={onClose} width={560}>
      <h3 className="heading-display" style={{ fontSize: 14, marginBottom: 8 }}>Import from GitHub</h3>
      {!result ? (
        <>
          <p className="section-copy small">A repository (its <code>skills/&lt;repo&gt;/SKILL.md</code> or root <code>SKILL.md</code>), a blob link or a raw link.</p>
          <input className="input-field" value={url} onChange={e => setUrl(e.target.value)} onKeyDown={e => e.key === 'Enter' && run()}
            placeholder="https://github.com/revitbridge/revit-bridge" style={{ margin: '12px 0' }} />
          {error && <p className="tool-warning-status">{error}</p>}
          <div className="flex justify-end gap-2">
            <button className="btn-secondary" onClick={onClose}>Cancel</button>
            <button className="btn-primary" onClick={run} disabled={busy || !url.trim()}>{busy ? 'Importing...' : 'Import'}</button>
          </div>
        </>
      ) : (
        <>
          <div className="card" style={{ padding: 16, marginBottom: 16 }}>
            <div className="skill-name">{result.name}</div>
            <p className="skill-desc">{result.description || 'No description'}</p>
          </div>
          <div className="flex justify-end"><button className="btn-primary" onClick={onDone}>Done</button></div>
        </>
      )}
    </Modal>
  )
}

const overlayStyle: CSSProperties = {
  position: 'fixed', inset: 0, background: 'rgba(20,20,19,0.4)', zIndex: 1000,
  display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
}

const modalStyle: CSSProperties = {
  background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 'var(--radius-sm)',
  padding: '24px 28px', width: '100%', maxHeight: '85vh', overflow: 'auto',
  boxShadow: '0 8px 32px rgba(0,0,0,0.12)',
}
