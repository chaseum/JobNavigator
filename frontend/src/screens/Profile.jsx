import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useTitle } from '../useTitle'
import { Button, Card, Check, DashedAdd, HeaderRow, Helper, Input, Label, Notice, PageTitle, Pill, Select, Tag, Textarea } from '../ui'

// Career Evidence. You upload the résumés you already have; what they say is
// extracted, reconciled into one canonical fact per real thing, and shown here
// for review. Every claim a generated résumé makes is cited from these rows (by
// `ref`, e.g. experience_7), and nothing counts until you confirm it.
//
// Résumé Library  -> the source documents and what each produced
// Conflicts       -> fields two résumés state differently; you pick the truth
// the rest        -> the canonical facts, each showing which résumés support it

const FACT_SECTIONS = [
  ['education', 'Education'], ['experience', 'Employment'], ['internship', 'Internships'],
  ['project', 'Projects'], ['research', 'Research'], ['skill', 'Skills'],
  ['certification', 'Certifications'], ['achievement', 'Achievements'], ['publication', 'Publications'], ['link', 'Links'],
]
// identity answers live on the Persona nodes the extension already reads
const TRI = [['__unset__', '— not answered'], ['true', 'Yes'], ['false', 'No']]
const IDENTITY = {
  personal: ['Personal info', [
    ['contact', 'first_name', 'First name'], ['contact', 'last_name', 'Last name'], ['contact', 'preferred_name', 'Preferred name'],
    ['contact', 'email', 'Email'], ['contact', 'phone', 'Phone'], ['contact', 'city', 'City'], ['contact', 'state', 'State'],
    ['contact', 'country', 'Country'], ['contact', 'linkedin', 'LinkedIn'], ['contact', 'github', 'GitHub'],
    ['contact', 'portfolio', 'Portfolio'], ['contact', 'website', 'Personal website'],
  ]],
  workauth: ['Work authorization', [
    ['work_auth', 'authorized_us', 'Authorized to work in the US?', 'bool'],
    ['work_auth', 'requires_sponsorship_now', 'Require sponsorship now?', 'bool'],
    ['work_auth', 'requires_sponsorship_future', 'Require sponsorship in the future?', 'bool'],
    ['work_auth', 'work_auth_type', 'Authorization type', 'enum', [['__unset__', '— not answered'], ['citizen', 'U.S. citizen'], ['permanent_resident', 'Permanent resident'], ['visa', 'Visa holder'], ['other', 'Other']]],
  ]],
  preferences: ['Job preferences', [
    ['preferences', 'willing_to_relocate', 'Willing to relocate?', 'bool'],
    ['preferences', 'remote_preference', 'Work arrangement', 'enum', [['__unset__', '— not answered'], ['remote', 'Remote'], ['hybrid', 'Hybrid'], ['onsite', 'On-site'], ['any', 'Any']]],
    ['preferences', 'desired_locations', 'Desired locations'],
    ['preferences', 'earliest_start', 'Earliest start date'],
    ['compensation', 'desired_salary', 'Salary expectation'],
  ]],
}
const NAV = [['library', 'Résumé Library'], ['conflicts', 'Conflicts'], ['personal', 'Personal info'],
  ...FACT_SECTIONS, ['workauth', 'Work authorization'], ['preferences', 'Job preferences']]
const EVIDENCE_KINDS = ['experience', 'internship', 'project', 'research', 'education']
const humanize = (s) => s.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
const errMsg = (e, fallback) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fallback)

export function headline(kind, d = {}) {
  const span = (s, e) => (s || e ? ` · ${s || '?'} – ${e || '?'}` : '')
  switch (kind) {
    case 'experience': case 'internship': return `${d.title || '?'} — ${d.employer || '?'}${span(d.start_date, d.end_date)}`
    case 'education': return `${[d.degree, d.major].filter(Boolean).join(', ') || 'Studies'} — ${d.institution || '?'}${d.graduation_date ? ' · ' + d.graduation_date : ''}`
    case 'project': return `${d.name || '?'}${span(d.start_date, d.end_date)}`
    case 'research': return `${d.title || 'Research'} — ${d.organization || '?'}${span(d.start_date, d.end_date)}`
    case 'skill': return `${d.name || '?'}${d.category ? ' · ' + d.category : ''}`
    case 'certification': return `${d.name || '?'}${d.issuer ? ' — ' + d.issuer : ''}`
    case 'achievement': return d.text || '?'
    case 'publication': return `${d.title || '?'}${d.venue ? ' — ' + d.venue : ''}`
    case 'link': return `${d.label || d.url || '?'}`
    default: return kind
  }
}

function FactForm({ kind, fields, initial, facts, onSave, onCancel, busy }) {
  const [d, setD] = useState(() => ({ ...initial }))
  const set = (k, v) => setD((p) => ({ ...p, [k]: v }))
  const evidence = facts.filter((f) => EVIDENCE_KINDS.includes(f.kind))
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 9 }}>
      {fields.map((f) => {
        const wide = ['list', 'textarea', 'refs'].includes(f.type)
        const label = <Label>{humanize(f.name)}{f.required ? ' *' : ''}</Label>
        let control
        if (f.type === 'list') {
          control = <Textarea rows={3} value={(d[f.name] || []).join('\n')} placeholder="One per line"
            onChange={(v) => set(f.name, v.split('\n'))} />
        } else if (f.type === 'textarea') {
          control = <Textarea rows={3} value={d[f.name] || ''} onChange={(v) => set(f.name, v)} />
        } else if (f.type === 'enum') {
          control = <Select value={d[f.name] || ''} options={f.options.map((o) => [o, o ? humanize(o) : '—'])} onPick={(v) => set(f.name, v)} />
        } else if (f.type === 'refs') {
          const on = new Set(d[f.name] || [])
          control = (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {evidence.length === 0 && <Helper>Add employment, projects or research first — a skill needs somewhere it was used.</Helper>}
              {evidence.map((e) => (
                <Pill key={e.ref} size="sm" on={on.has(e.ref)}
                  onClick={() => { const n = new Set(on); n.has(e.ref) ? n.delete(e.ref) : n.add(e.ref); set(f.name, [...n]) }}>
                  {on.has(e.ref) ? '✓ ' : ''}{headline(e.kind, e.data).slice(0, 48)}
                </Pill>
              ))}
            </div>
          )
        } else {
          control = <Input value={d[f.name] || ''} placeholder={f.type === 'date' ? 'YYYY-MM or present' : ''} onChange={(v) => set(f.name, v)} />
        }
        return <div key={f.name} style={{ display: 'flex', flexDirection: 'column', gap: 4, gridColumn: wide ? '1 / -1' : undefined }}>{label}{control}</div>
      })}
      <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <Button variant="secondary" size="sm" onClick={onCancel}>Cancel</Button>
        <Button size="sm" busy={busy} onClick={() => {
          const clean = { ...d }
          fields.forEach((f) => { if (f.type === 'list') clean[f.name] = (clean[f.name] || []).map((x) => x.trim()).filter(Boolean) })
          onSave(clean)
        }}>Save</Button>
      </div>
    </div>
  )
}

function FactCard({ fact, children: kids, schema, facts, api: ops, depth = 0, selected, onSelect }) {
  const [editing, setEditing] = useState(false)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)
  const canParent = EVIDENCE_KINDS.includes(fact.kind)
  const run = async (fn) => { setBusy(true); try { await fn() } finally { setBusy(false) } }
  const sources = fact.sources || []
  return (
    <Card style={{ padding: '10px 14px', marginLeft: depth * 18, display: 'flex', flexDirection: 'column', gap: 8, borderStyle: fact.verified ? undefined : 'dashed' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {!fact.verified && onSelect && (
          <Check checked={!!selected} onChange={() => onSelect(fact.id)} ariaLabel={`Select ${headline(fact.kind, fact.data)}`} />
        )}
        <span style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: depth ? 400 : 600 }}>{headline(fact.kind, fact.data)}</span>
        <Tag title="Provenance id cited by generated résumé claims">{fact.ref}</Tag>
        {fact.open_conflicts > 0 && <Tag tone="warn" title="Your résumés disagree about a field here">needs a decision</Tag>}
        {!fact.verified && <Tag tone="warn" title={`From ${fact.source} — not used until verified`}>unverified</Tag>}
        {!fact.verified && <Button size="sm" busy={busy} onClick={() => run(() => ops.verify([fact.id]))}>Confirm</Button>}
        <Button variant="secondary" size="sm" onClick={() => setEditing((v) => !v)}>{editing ? 'Close' : 'Edit'}</Button>
        <Button variant="secondary" size="sm" onClick={() => ops.remove(fact)}>Delete</Button>
      </div>
      {sources.length > 0 && (
        <Helper size="xs" title="The uploaded résumés this fact came from">
          Used in: {sources.map((s) => s.filename).join(', ')}
        </Helper>
      )}
      {editing && (
        <FactForm kind={fact.kind} fields={schema[fact.kind]} initial={fact.data} facts={facts} busy={busy}
          onCancel={() => setEditing(false)}
          onSave={(data) => run(async () => { if (await ops.update(fact.id, { data })) setEditing(false) })} />
      )}
      {kids}
      {canParent && !adding && <DashedAdd onClick={() => setAdding(true)}>+ Add accomplishment under this</DashedAdd>}
      {adding && (
        <FactForm kind="achievement" fields={schema.achievement} initial={{}} facts={facts} busy={busy}
          onCancel={() => setAdding(false)}
          onSave={(data) => run(async () => { if (await ops.create('achievement', data, fact.id)) setAdding(false) })} />
      )}
    </Card>
  )
}

function IdentityForm({ section, identity, onSave }) {
  const [, fields] = IDENTITY[section]
  return (
    <Card style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, padding: 14 }}>
      {fields.map(([node, key, label, kind, opts]) => {
        const val = (identity[node] || {})[key]
        const write = (v) => onSave(node, key, v)
        return (
          <div key={key} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <Label>{label}</Label>
            {kind === 'bool' ? (
              <Select value={val === true ? 'true' : val === false ? 'false' : '__unset__'} options={TRI}
                onPick={(v) => write(v === '__unset__' ? undefined : v === 'true')} />
            ) : kind === 'enum' ? (
              <Select value={val || '__unset__'} options={opts} onPick={(v) => write(v === '__unset__' ? undefined : v)} />
            ) : (
              <Input defaultValue={val || ''} onBlur={(e) => { if ((e.target.value || '') !== (val || '')) write(e.target.value.trim() || undefined) }} />
            )}
          </div>
        )
      })}
      {section === 'workauth' && (
        <Helper style={{ gridColumn: '1 / -1' }}>
          Only what you set here is ever used. Autofill never lets a model guess work authorization, sponsorship, salary or EEO answers —
          unanswered stays empty. Demographic/EEO answers are on <RouterLink to="/persona">Persona</RouterLink>.
        </Helper>
      )}
    </Card>
  )
}

const STATUS_TONE = { imported: 'good', failed: 'bad', pending: 'warn', parsing: 'warn' }
const STATUS_TEXT = { imported: 'imported', failed: 'failed', pending: 'queued', parsing: 'reading…' }

function ResumeLibrary({ onChanged, pushToast }) {
  const [lib, setLib] = useState(null)
  const [bases, setBases] = useState([])
  const [busy, setBusy] = useState(false)
  const dropRef = useRef(null)
  const fileRef = useRef(null)

  const load = useCallback(() => api.get('/profile/resumes').then(({ data }) => setLib(data)).catch(() => {}), [])
  useEffect(() => { load(); api.get('/resumes', { params: { is_base: true } }).then(({ data }) => setBases(Array.isArray(data) ? data : [])).catch(() => {}) }, [load])
  // an import is N provider calls in the background; poll only while one is running
  useEffect(() => {
    if (!lib?.importing) return undefined
    const t = setInterval(() => { load(); onChanged() }, 3000)
    return () => clearInterval(t)
  }, [lib?.importing, load, onChanged])

  const upload = async (files) => {
    const pdfs = [...files].filter((f) => f.name.toLowerCase().endsWith('.pdf'))
    if (!pdfs.length) return pushToast({ kind: 'error', msg: 'Résumés must be PDFs' })
    setBusy(true)
    try {
      const fd = new FormData()
      pdfs.forEach((f) => fd.append('files', f))
      const { data } = await api.post('/profile/resumes', fd, { headers: { 'Content-Type': 'multipart/form-data' } })
      const parts = [`${data.accepted.length} queued`]
      if (data.skipped.length) parts.push(`${data.skipped.length} already in the library`)
      if (data.rejected.length) parts.push(`${data.rejected.length} rejected`)
      pushToast({ kind: data.accepted.length ? 'success' : 'error', msg: parts.join(' · ') })
      data.rejected.forEach((r) => pushToast({ kind: 'error', msg: `${r.filename}: ${r.reason}` }))
      await load(); onChanged()
    } catch (e) { pushToast({ kind: 'error', msg: 'Upload failed — ' + errMsg(e, 'unknown error') }) } finally { setBusy(false) }
  }

  const importOther = async (req, label) => {
    setBusy(true)
    try {
      const { data } = await req()
      pushToast({ kind: 'success', msg: `${label}: ${data.novel} new, ${data.enriched} enriched, ${data.duplicate} already known${data.conflict ? `, ${data.conflict} to resolve` : ''}` })
      await load(); onChanged()
    } catch (e) { pushToast({ kind: 'error', msg: 'Import failed — ' + errMsg(e, 'unknown error') }) } finally { setBusy(false) }
  }

  const remove = async (s) => {
    if (!window.confirm(`Forget "${s.filename}"? The facts it supported stay — other résumés may support them too.`)) return
    await api.delete(`/profile/resumes/${s.id}`); await load(); onChanged()
  }

  const sources = lib?.sources || []
  return (
    <>
      <div ref={dropRef} role="button" tabIndex={0}
        onClick={() => fileRef.current?.click()} onKeyDown={(e) => e.key === 'Enter' && fileRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); dropRef.current.style.borderColor = 'var(--accent)' }}
        onDragLeave={() => { dropRef.current.style.borderColor = 'var(--line)' }}
        onDrop={(e) => { e.preventDefault(); dropRef.current.style.borderColor = 'var(--line)'; upload(e.dataTransfer.files) }}
        style={{ border: '1px dashed var(--line)', borderRadius: 'var(--radius-card)', padding: '22px 16px',
          textAlign: 'center', cursor: 'pointer', background: 'var(--surface-2, transparent)' }}>
        <div style={{ fontSize: 13.5, fontWeight: 600 }}>Drop your résumés here</div>
        <Helper>Select or drag several PDFs at once — every résumé you have ever sent out. Each one is read, and what it
          says is merged into one entry per real job, degree or project. You review the result; nothing is used until you confirm it.</Helper>
        <input ref={fileRef} type="file" accept="application/pdf" multiple hidden
          onChange={(e) => { const f = e.target.files; e.target.value = ''; if (f?.length) upload(f) }} />
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <Helper size="xs">Or import from what is already here:</Helper>
        <Button variant="secondary" size="sm" busy={busy} onClick={() => importOther(() => api.post('/profile/import', { source: 'persona' }), 'Persona résumé content')}>Persona content</Button>
        {bases.map((r) => (
          <Button key={r.id} variant="secondary" size="sm" busy={busy} onClick={() => importOther(() => api.post('/profile/import', { resume_id: r.id }), r.name)}>{r.name}</Button>
        ))}
      </div>

      {lib?.importing && <Notice tone="quiet" glyph="◴">Reading your résumés… this page updates as each one finishes.</Notice>}
      {sources.length === 0 && !lib?.importing && <Helper>No résumés uploaded yet.</Helper>}

      {sources.map((s) => (
        <Card key={s.id} style={{ padding: '10px 14px', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ flex: 1, minWidth: 160, fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.filename}</span>
          <Tag tone={STATUS_TONE[s.status] || 'neutral'}>{STATUS_TEXT[s.status] || s.status}</Tag>
          {s.status === 'imported' && (
            <Helper size="xs">
              {s.facts} fact{s.facts === 1 ? '' : 's'}
              {s.result?.novel ? ` · ${s.result.novel} new` : ''}
              {s.result?.enriched ? ` · ${s.result.enriched} enriched` : ''}
              {s.result?.duplicate ? ` · ${s.result.duplicate} already known` : ''}
            </Helper>
          )}
          {s.conflicts > 0 && <Tag tone="warn">{s.conflicts} conflict{s.conflicts === 1 ? '' : 's'}</Tag>}
          {s.error && <Helper size="xs" style={{ color: 'var(--bad)' }}>{s.error}</Helper>}
          <Button variant="secondary" size="sm" onClick={() => remove(s)}>Forget</Button>
        </Card>
      ))}
    </>
  )
}

function Conflicts({ onChanged, pushToast }) {
  const [rows, setRows] = useState(null)
  const [custom, setCustom] = useState({})
  const load = useCallback(() => api.get('/profile/conflicts').then(({ data }) => setRows(data)).catch(() => {}), [])
  useEffect(() => { load() }, [load])

  const resolve = async (c, body) => {
    try {
      await api.post(`/profile/conflicts/${c.id}/resolve`, body)
      await load(); onChanged()
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not resolve') }) }
  }

  if (!rows) return null
  if (!rows.length) return <Helper>Nothing to resolve. When two résumés state the same field differently, it waits here — no model ever picks for you.</Helper>
  return (
    <>
      <Helper>Two of your résumés disagree. Choose the true value; until you do, the stored one stands and nothing is overwritten.</Helper>
      {rows.map((c) => (
        <Card key={c.id} style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>{c.headline}</div>
          <Helper size="xs">{humanize(c.field)}{c.from_resume ? ` · ${c.from_resume} disagrees` : ''}</Helper>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <Button variant="secondary" size="sm" onClick={() => resolve(c, { choice: 'current' })}>Keep “{c.current_value}”</Button>
            <Button variant="secondary" size="sm" onClick={() => resolve(c, { choice: 'proposed' })}>Use “{c.proposed_value}”</Button>
            <Input value={custom[c.id] ?? ''} placeholder="or type the correct value"
              onChange={(v) => setCustom((p) => ({ ...p, [c.id]: v }))} />
            <Button size="sm" disabled={!((custom[c.id] || '').trim())} onClick={() => resolve(c, { value: (custom[c.id] || '').trim() })}>Save</Button>
          </div>
        </Card>
      ))}
    </>
  )
}

export default function Profile() {
  useTitle('Career Evidence')
  const [prof, setProf] = useState(null)
  const [section, setSection] = useState(() => { try { return localStorage.getItem('jn_profile_section') || 'library' } catch { return 'library' } })
  const [adding, setAdding] = useState(false)
  const [picked, setPicked] = useState(() => new Set())
  const { toasts, push: pushToast, dismiss } = useToasts()

  const load = useCallback(() => api.get('/profile').then(({ data }) => setProf(data))
    .catch((e) => pushToast({ kind: 'error', msg: 'Could not load profile — ' + errMsg(e, 'server unreachable') })), [pushToast])
  useEffect(() => { load() }, [load])
  useEffect(() => { try { localStorage.setItem('jn_profile_section', section) } catch { /* ignore */ } setAdding(false); setPicked(new Set()) }, [section])

  const ops = useMemo(() => ({
    create: async (kind, data, parent_id) => {
      try { await api.post('/profile/facts', { kind, data, parent_id }); await load(); return true } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not save') }); return false }
    },
    update: async (id, body) => {
      try { await api.patch(`/profile/facts/${id}`, body); await load(); return true } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not save') }); return false }
    },
    verify: async (ids) => { await api.post('/profile/facts/verify', { ids }); await load() },
    remove: async (fact) => {
      if (!window.confirm(`Delete "${headline(fact.kind, fact.data)}"${fact.kind !== 'achievement' ? ' and anything under it' : ''}?`)) return
      await api.delete(`/profile/facts/${fact.id}`); await load()
    },
  }), [load, pushToast])

  const saveIdentity = async (node, key, value) => {
    const next = { ...((prof?.identity || {})[node] || {}) }
    if (value === undefined) delete next[key]; else next[key] = value
    try { await api.patch('/persona', { [node]: next }); await load() } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not save') }) }
  }

  const facts = prof?.facts || []
  const counts = useMemo(() => facts.reduce((m, f) => { m[f.kind] = (m[f.kind] || 0) + 1; return m }, {}), [facts])
  const unverifiedIds = facts.filter((f) => !f.verified).map((f) => f.id)
  const isFactSection = FACT_SECTIONS.some(([k]) => k === section)
  const rows = facts.filter((f) => f.kind === section && (section !== 'achievement' || f.parent_id == null))
  const childrenOf = (id) => facts.filter((f) => f.parent_id === id)

  const navCount = (id) => (id === 'library' ? prof?.resume_count : id === 'conflicts' ? prof?.open_conflicts : counts[id])
  const toggle = (id) => setPicked((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n })
  const pendingHere = rows.flatMap((f) => [f, ...childrenOf(f.id)]).filter((f) => !f.verified).map((f) => f.id)

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 12 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
          <PageTitle>Career Evidence</PageTitle>
          <Helper>{prof ? `${prof.resume_count} résumé${prof.resume_count === 1 ? '' : 's'} · ${facts.length} facts · `
            + `${unverifiedIds.length} to confirm${prof.open_conflicts ? ` · ${prof.open_conflicts} conflicts` : ''} · version ${prof.profile_version}` : ' '}</Helper>
        </div>
        <span style={{ marginLeft: 'auto' }} />
        {prof?.open_conflicts > 0 && section !== 'conflicts' && (
          <Button variant="secondary" size="sm" onClick={() => setSection('conflicts')}>Resolve {prof.open_conflicts} conflict{prof.open_conflicts === 1 ? '' : 's'}</Button>
        )}
        <Button variant="secondary" size="sm" onClick={() => setSection('library')}>Upload résumés ↑</Button>
      </HeaderRow>

      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <nav style={{ width: 210, borderRight: '1px solid var(--line)', padding: '12px 8px', display: 'flex', flexDirection: 'column', gap: 2, overflow: 'auto' }}>
          {NAV.map(([id, label]) => (
            <div key={id} role="button" tabIndex={0} onClick={() => setSection(id)} onKeyDown={(e) => e.key === 'Enter' && setSection(id)}
              className="v2-profnav" style={{ display: 'flex', padding: '6px 10px', borderRadius: 'var(--radius-row)', cursor: 'pointer', fontSize: 13,
                background: section === id ? 'var(--accent-soft)' : undefined, color: section === id ? 'var(--accent)' : 'var(--text)' }}>
              <span style={{ flex: 1 }}>{label}</span>
              {navCount(id) ? <Helper size="xs">{navCount(id)}</Helper> : null}
            </div>
          ))}
          <RouterLink to="/answer-bank" style={{ padding: '6px 10px', fontSize: 13, color: 'var(--muted)' }}>Common answers →</RouterLink>
        </nav>

        <div className="v2-scroll" style={{ flex: 1, overflow: 'auto', padding: '16px 26px 30px', display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0 }}>
          {!prof ? null : section === 'library' ? (
            <ResumeLibrary pushToast={pushToast} onChanged={load} />
          ) : section === 'conflicts' ? (
            <Conflicts pushToast={pushToast} onChanged={load} />
          ) : isFactSection ? (
            <>
              <Helper>
                {section === 'skill'
                  ? 'Link each skill to where you actually used it. A skill with no evidence never counts as a match.'
                  : 'Everything here came from your résumés or from you. Add anything true they left out — tailoring picks from this pool and never writes anything that is not in it.'}
              </Helper>
              {pendingHere.length > 0 && (
                <Card style={{ padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <Helper size="xs">{pendingHere.length} imported fact{pendingHere.length === 1 ? '' : 's'} here await your confirmation.</Helper>
                  <Button variant="secondary" size="sm" onClick={() => setPicked(new Set(pendingHere))}>Select all</Button>
                  {picked.size > 0 && <Button variant="secondary" size="sm" onClick={() => setPicked(new Set())}>Clear</Button>}
                  <Button size="sm" disabled={picked.size === 0}
                    onClick={async () => { await ops.verify([...picked]); setPicked(new Set()) }}>Confirm {picked.size || ''}</Button>
                </Card>
              )}
              {rows.map((f) => (
                <FactCard key={f.id} fact={f} schema={prof.schema} facts={facts} api={ops}
                  selected={picked.has(f.id)} onSelect={toggle}>
                  {childrenOf(f.id).map((c) => (
                    <FactCard key={c.id} fact={c} schema={prof.schema} facts={facts} api={ops} depth={1}
                      selected={picked.has(c.id)} onSelect={toggle} />
                  ))}
                </FactCard>
              ))}
              {adding ? (
                <Card style={{ padding: 14 }}>
                  <FactForm kind={section} fields={prof.schema[section]} initial={section === 'internship' ? { employment_type: 'internship' } : {}}
                    facts={facts} onCancel={() => setAdding(false)}
                    onSave={async (data) => { if (await ops.create(section, data)) setAdding(false) }} />
                </Card>
              ) : <DashedAdd big onClick={() => setAdding(true)}>+ Add {(FACT_SECTIONS.find(([k]) => k === section) || [])[1]}</DashedAdd>}
            </>
          ) : (
            <IdentityForm key={section + prof.profile_version} section={section} identity={prof.identity} onSave={saveIdentity} />
          )}
        </div>
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
