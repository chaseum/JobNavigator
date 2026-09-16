import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate, useParams, Link as RouterLink } from 'react-router-dom'
import { FileText, MoreHorizontal, Plus } from 'lucide-react'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useTitle } from '../useTitle'
import { ago } from '../time'
import { Button, Helper, IconButton, Menu, MenuItem, Notice, Spinner, Tag } from '../ui'
import { headline } from './Profile'
import { ResumeReview } from './JobDetail'

// Resume: every fact-based résumé version as one table. A base version renders
// the verified profile verbatim; a tailored one is drafted for a job and diffs
// against the primary base (the newest accepted one), which is also what
// autofill uploads when a job has no accepted tailored version. The legacy JSON
// résumé editor lives in /classic.

const errMsg = (e, fb) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fb)
const byTime = (r) => new Date(r.accepted_at || r.created_at).getTime()
const versionName = (r, n) => (r.kind === 'base' ? `Base résumé${n ? ` v${n}` : ''}` : `${r.company || 'Unknown company'} — tailored`)
const statusOf = (r) => (r.status === 'accepted' ? ['good', 'Accepted'] : r.status === 'rejected' ? ['neutral', 'Rejected'] : r.blocked ? ['bad', 'Blocked'] : ['accent', 'Draft'])
const COLS = [['Resume', '38%'], ['Target Job Title', '28%'], ['Last Modified', '14%'], ['Created', '14%'], ['', '56px']]
const CELL = { padding: '12px 16px', borderBottom: '1px solid var(--line-soft)', verticalAlign: 'middle' }

// Role-family bases: Software Engineering, Product/TPM, Data/ML (plus anything
// configured). Each one is a *selection* over the same verified Career Evidence —
// which experience, projects and skills lead — not a second copy of the truth, and
// never different wording. A tailored resume normally derives from its family base.
function RoleFamilyBases({ busy, onGenerate, refresh }) {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const load = useCallback(() => api.get('/resume-versions/role-families').then(({ data }) => setData(data)).catch(() => {}), [])
  useEffect(() => { load() }, [load, refresh])
  if (!data?.families?.length) return null
  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--line)', borderRadius: 'var(--radius-card)', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div>
        <strong style={{ fontSize: 'var(--t-13)' }}>Role resumes</strong>
        <Helper>One base per kind of role. Same verified evidence, ordered for that role — a tailored resume starts from the base matching the job.</Helper>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 10 }}>
        {data.families.map((f) => (
          <div key={f.id} style={{ border: '1px solid var(--line-soft)', borderRadius: 'var(--radius-row)', padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ flex: 1, fontSize: 'var(--t-13)', fontWeight: 'var(--weight-semibold)' }}>{f.label}</span>
              {f.base && <Tag tone={f.base.status === 'accepted' ? 'good' : 'accent'}>{f.base.status}</Tag>}
            </div>
            <Helper size="xs">{f.base ? `Built ${ago(f.base.created_at)}${f.base.pages ? ` · ${f.base.pages}pp` : ''}` : 'Not generated yet'}</Helper>
            <div style={{ display: 'flex', gap: 6 }}>
              {f.base && <Button variant="secondary" size="sm" onClick={() => navigate(`/resumes/versions/${f.base.id}`)}>View</Button>}
              <Button variant="secondary" size="sm" busy={busy === f.id} onClick={() => onGenerate(f.id)}>{f.base ? 'Regenerate' : 'Generate'}</Button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Resumes() {
  useTitle('Resume')
  const navigate = useNavigate()
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(false)
  const [showRejected, setShowRejected] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [rowMenu, setRowMenu] = useState(null)
  const [busy, setBusy] = useState('')
  const fileRef = useRef(null)
  const { toasts, push: pushToast, dismiss } = useToasts()

  const load = useCallback(() => api.get('/resume-versions')
    .then(({ data }) => { setRows(data || []); setErr(false) })
    .catch(() => setErr(true)), [])
  useEffect(() => { load() }, [load])

  const act = async (key, fn, ok) => {
    setBusy(key)
    try { const r = await fn(); if (ok) pushToast({ kind: 'success', msg: ok }); await load(); return r }
    catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Request failed') }); return null }
    finally { setBusy('') }
  }
  const generateBase = async (roleFamily) => {
    setAddOpen(false)
    const r = await act(roleFamily || 'base', () => api.post('/resume-versions/base', { role_family: roleFamily || null }),
      roleFamily ? 'Role résumé drafted — review and accept it' : 'Base résumé drafted — review and accept it')
    if (r?.data?.id) navigate(`/resumes/versions/${r.data.id}`)
  }
  const importPdf = async (file) => {
    if (!file) return
    const fd = new FormData(); fd.append('file', file)
    const r = await act('import', () => api.post('/profile/import', fd, { headers: { 'Content-Type': 'multipart/form-data' } }))
    if (r) pushToast({ kind: 'success', msg: `Imported ${r.data.created} fact${r.data.created === 1 ? '' : 's'} from ${file.name} — verify them in Profile, then generate a base résumé` })
  }

  const all = rows || []
  const primaryId = useMemo(() => all.filter((r) => r.kind === 'base' && r.status === 'accepted').sort((a, b) => byTime(b) - byTime(a))[0]?.id, [all])
  const baseNo = useMemo(() => {
    const m = {}
    all.filter((r) => r.kind === 'base').sort((a, b) => new Date(a.created_at) - new Date(b.created_at)).forEach((r, i) => { m[r.id] = i + 1 })
    return m
  }, [all])
  const rejected = all.filter((r) => r.status === 'rejected').length
  const visible = all.filter((r) => showRejected || r.status !== 'rejected')
    .sort((a, b) => (b.id === primaryId) - (a.id === primaryId) || byTime(b) - byTime(a))

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <header style={{ flex: '0 0 auto', height: 56, display: 'flex', alignItems: 'center', gap: 12, padding: '0 24px', background: 'var(--surface)', borderBottom: '1px solid var(--line)' }}>
        <h1 style={{ margin: 0, fontSize: 'var(--t-19)', fontWeight: 'var(--weight-semibold)' }}>Resume</h1>
      </header>
      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto' }}>
        <div style={{ maxWidth: 1180, margin: '0 auto', padding: '16px 24px 40px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <Helper style={{ fontSize: 'var(--t-13)' }}>
              {rows ? `${visible.length} résumé${visible.length === 1 ? '' : 's'} · built only from verified ` : ' '}
              {rows && <RouterLink to="/profile">Profile</RouterLink>}{rows && ' facts, every claim traceable'}
            </Helper>
            <span style={{ flex: 1 }} />
            <div style={{ position: 'relative' }}>
              <Button size="sm" busy={busy === 'base' || busy === 'import'} onClick={() => setAddOpen((v) => !v)} ariaExpanded={addOpen} ariaHaspopup="menu">
                <Plus size={15} aria-hidden="true" /> Add Resume
              </Button>
              {addOpen && (
                <Menu onDismiss={() => setAddOpen(false)} style={{ position: 'absolute', right: 0, top: 'calc(100% + 6px)', zIndex: 50, width: 280 }}>
                  <MenuItem onClick={() => generateBase()} hint="from Career Evidence">Generate base résumé</MenuItem>
                  <MenuItem onClick={() => { setAddOpen(false); fileRef.current?.click() }}>Import a PDF into your profile…</MenuItem>
                  <MenuItem onClick={() => navigate('/feed')}>Tailor for a job…</MenuItem>
                </Menu>
              )}
              <input ref={fileRef} type="file" accept="application/pdf" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; importPdf(f) }} />
            </div>
          </div>

          <RoleFamilyBases busy={busy} onGenerate={generateBase} refresh={rows} />

          {err ? (
            <Notice tone="bad" action={<Button size="sm" variant="secondary" onClick={load}>Retry</Button>}><Helper>Couldn’t load your résumés. Check that the backend is running.</Helper></Notice>
          ) : !rows ? (
            <Helper><Spinner /> Loading…</Helper>
          ) : all.length === 0 ? (
            <Notice tone="quiet" glyph="○" action={<Button size="sm" busy={busy === 'base'} onClick={() => generateBase()}>Generate base résumé</Button>}>
              <strong style={{ fontSize: 'var(--t-13)' }}>No résumés yet</strong>
              <Helper>A base résumé renders your verified Profile facts through the LaTeX template. Tailored versions for each job diff against it.</Helper>
            </Notice>
          ) : (
            <>
              {!primaryId && <Notice tone="warn"><Helper>No accepted base résumé yet. Tailored résumés diff against it and autofill falls back to it, so review and accept one.</Helper></Notice>}
              <div style={{ background: 'var(--surface)', border: '1px solid var(--line)', borderRadius: 'var(--radius-card)' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed', fontSize: 'var(--t-13)' }}>
                  <thead>
                    <tr>
                      {COLS.map(([h, w], i) => (
                        <th key={i} scope="col" style={{ ...CELL, width: w, textAlign: 'left', fontWeight: 'var(--weight-semibold)', color: 'var(--text)', borderBottom: '1px solid var(--line)', whiteSpace: 'nowrap' }}>
                          {h || <span className="sr-only">Actions</span>}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((r) => {
                      const [tone, label] = statusOf(r)
                      const name = versionName(r, baseNo[r.id])
                      const open = () => navigate(`/resumes/versions/${r.id}`)
                      return (
                        <tr key={r.id} className="v2-trow" onClick={open} style={{ cursor: 'pointer' }}>
                          <td style={CELL}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                              <span aria-hidden="true" style={{ flex: '0 0 30px', height: 30, borderRadius: 'var(--radius-round)', background: `var(--tag-${tone}-bg)`, color: `var(--tag-${tone}-ink)`, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                                <FileText size={15} />
                              </span>
                              <RouterLink to={`/resumes/versions/${r.id}`} onClick={(e) => e.stopPropagation()} title={name}
                                style={{ minWidth: 0, color: 'var(--text)', fontWeight: 'var(--weight-medium)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</RouterLink>
                              {r.id === primaryId && <Tag tone="good" title="The newest accepted base: tailored résumés diff against it, and autofill uploads it when a job has no accepted tailored version">★ Primary</Tag>}
                              <Tag tone={tone} title={r.blocked ? 'Unsupported claims block the PDF until reviewed' : undefined}>{label}</Tag>
                            </div>
                          </td>
                          <td style={{ ...CELL, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {r.kind === 'tailored'
                              ? (r.job_id ? <RouterLink to={`/jobs/${r.job_id}`} onClick={(e) => e.stopPropagation()} style={{ color: 'var(--text-2)' }}>{r.title || 'Role'}</RouterLink> : <span>{r.title || '—'}</span>)
                              : <Helper style={{ fontSize: 'var(--t-13)' }}>All jobs</Helper>}
                            {r.match_score != null && <Helper> · Candidate Fit {r.match_score}</Helper>}
                          </td>
                          <td style={{ ...CELL, color: 'var(--muted)' }} title={new Date(r.accepted_at || r.created_at).toLocaleString()}>{ago(r.accepted_at || r.created_at)}</td>
                          <td style={{ ...CELL, color: 'var(--muted)' }} title={new Date(r.created_at).toLocaleString()}>{ago(r.created_at)}</td>
                          <td style={{ ...CELL, position: 'relative' }} onClick={(e) => e.stopPropagation()}>
                            <IconButton title={`Actions for ${name}`} ariaHaspopup="menu" ariaExpanded={rowMenu === r.id} onClick={() => setRowMenu(rowMenu === r.id ? null : r.id)}>
                              <MoreHorizontal size={16} aria-hidden="true" />
                            </IconButton>
                            {rowMenu === r.id && (
                              <Menu onDismiss={() => setRowMenu(null)} style={{ position: 'absolute', right: 12, top: 'calc(100% - 6px)', zIndex: 40, width: 220 }}>
                                <MenuItem onClick={open}>Open</MenuItem>
                                {r.job_id && <MenuItem onClick={() => navigate(`/jobs/${r.job_id}?tab=resume`)}>Open job</MenuItem>}
                                {r.pages ? <MenuItem href={`/api/resume-versions/${r.id}/pdf`} target="_blank">PDF ↗</MenuItem> : null}
                                <MenuItem href={`/api/resume-versions/${r.id}/tex`}>Download .tex</MenuItem>
                                <MenuItem href={`/api/resume-versions/${r.id}/export.zip`}>Overleaf ZIP</MenuItem>
                                {r.status === 'draft' && !r.blocked && (
                                  <MenuItem divider onClick={() => { setRowMenu(null); act(r.id, () => api.post(`/resume-versions/${r.id}/accept`, { all: true }), 'Résumé accepted') }}>Accept</MenuItem>
                                )}
                                {r.status === 'draft' && (
                                  <MenuItem danger onClick={() => { setRowMenu(null); if (window.confirm('Discard this draft?')) act(r.id, () => api.post(`/resume-versions/${r.id}/reject`), 'Draft rejected') }}>Reject draft</MenuItem>
                                )}
                              </Menu>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {rejected > 0 && (
                <Button variant="ghost" size="xs" onClick={() => setShowRejected((v) => !v)} style={{ alignSelf: 'flex-start' }}>
                  {showRejected ? 'Hide rejected' : `Show ${rejected} rejected`}
                </Button>
              )}
            </>
          )}
          <Helper>The previous JSON résumé editor is still available in the <a href="/classic/resumes">classic interface</a>.</Helper>
        </div>
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}

// /resumes/versions/:id — one version's review page
export function ResumeVersionPage() {
  const { id } = useParams()
  const [v, setV] = useState(null)
  const [profile, setProfile] = useState(null)
  const { toasts, push: pushToast, dismiss } = useToasts()
  const loadHead = useCallback(() => api.get(`/resume-versions/${id}`).then(({ data }) => setV(data)).catch(() => setV(false)), [id])
  useEffect(() => { loadHead() }, [loadHead])
  useEffect(() => { api.get('/profile').then(({ data }) => setProfile(data)).catch(() => {}) }, [])
  const factHeadlines = useMemo(() => Object.fromEntries((profile?.facts || []).map((f) => [f.ref, headline(f.kind, f.data)])), [profile])
  const name = v ? versionName(v) : 'Resume'
  useTitle(name)
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <header style={{ flex: '0 0 auto', display: 'flex', flexDirection: 'column', gap: 2, padding: '12px 24px', background: 'var(--surface)', borderBottom: '1px solid var(--line)' }}>
        <RouterLink to="/resumes" style={{ fontSize: 'var(--t-12)', color: 'var(--muted)' }}>‹ Resume</RouterLink>
        <h1 style={{ margin: 0, fontSize: 'var(--t-19)', fontWeight: 'var(--weight-semibold)' }}>{name}</h1>
        {v?.job_id && <Helper style={{ fontSize: 'var(--t-13)' }}>For <RouterLink to={`/jobs/${v.job_id}?tab=resume`}>{v.job_analysis?.analysis?.title || 'this job'}</RouterLink></Helper>}
      </header>
      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto' }}>
        <div style={{ maxWidth: 1080, margin: '0 auto', padding: '16px 24px 40px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {v === false
            ? <Notice tone="bad"><Helper>This résumé version no longer exists. <RouterLink to="/resumes">Back to Resume</RouterLink></Helper></Notice>
            : <ResumeReview versionId={id} factHeadlines={factHeadlines} onChanged={loadHead} pushToast={pushToast} />}
        </div>
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
