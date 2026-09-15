import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useTitle } from '../useTitle'
import { Button, Card, DashedAdd, HeaderRow, Helper, Input, Label, Notice, PageTitle, Pill, Select, Tag, Textarea } from '../ui'

// The candidate fact database. Everything a tailored résumé may say is cited
// from these rows (by `ref`, e.g. experience_7). Put far more here than fits on
// one page: facts that never appear on your base résumé are what tailoring
// selects from. Imported facts stay "unverified" and are ignored until you verify them.

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
const NAV = [['personal', 'Personal info'], ...FACT_SECTIONS, ['workauth', 'Work authorization'], ['preferences', 'Job preferences']]
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

function FactCard({ fact, children: kids, schema, facts, api: ops, depth = 0 }) {
  const [editing, setEditing] = useState(false)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)
  const canParent = EVIDENCE_KINDS.includes(fact.kind)
  const run = async (fn) => { setBusy(true); try { await fn() } finally { setBusy(false) } }
  return (
    <Card style={{ padding: '10px 14px', marginLeft: depth * 18, display: 'flex', flexDirection: 'column', gap: 8, borderStyle: fact.verified ? undefined : 'dashed' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: depth ? 400 : 600 }}>{headline(fact.kind, fact.data)}</span>
        <Tag title="Provenance id cited by generated résumé claims">{fact.ref}</Tag>
        {!fact.verified && <Tag tone="warn" title={`From ${fact.source} — not used until verified`}>unverified</Tag>}
        {!fact.verified && <Button size="sm" busy={busy} onClick={() => run(() => ops.verify([fact.id]))}>Verify</Button>}
        <Button variant="secondary" size="sm" onClick={() => setEditing((v) => !v)}>{editing ? 'Close' : 'Edit'}</Button>
        <Button variant="secondary" size="sm" onClick={() => ops.remove(fact)}>Delete</Button>
      </div>
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

function ImportPanel({ onDone, pushToast }) {
  const [bases, setBases] = useState([])
  const [busy, setBusy] = useState(false)
  const fileRef = useRef(null)
  useEffect(() => { api.get('/resumes', { params: { is_base: true } }).then(({ data }) => setBases(Array.isArray(data) ? data : [])).catch(() => {}) }, [])
  const go = async (req, label) => {
    setBusy(true)
    try {
      const { data } = await req()
      pushToast({ kind: 'success', msg: `Imported ${data.created} fact${data.created === 1 ? '' : 's'} from ${label} — review and verify them below${data.skipped ? ` · ${data.skipped} already present` : ''}` })
      onDone()
    } catch (e) { pushToast({ kind: 'error', msg: 'Import failed — ' + errMsg(e, 'unknown error') }) } finally { setBusy(false) }
  }
  return (
    <Notice tone="quiet" glyph="↑">
      <strong style={{ fontSize: 12.5 }}>Import a résumé</strong>
      <Helper>Imported roles, bullets and skills arrive unverified. Nothing unverified is matched against jobs or put on a résumé.</Helper>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 6 }}>
        <Button size="sm" busy={busy} onClick={() => fileRef.current?.click()}>PDF…</Button>
        <input ref={fileRef} type="file" accept="application/pdf" hidden onChange={(e) => {
          const f = e.target.files?.[0]; e.target.value = ''
          if (!f) return
          const fd = new FormData(); fd.append('file', f)
          go(() => api.post('/profile/import', fd, { headers: { 'Content-Type': 'multipart/form-data' } }), f.name)
        }} />
        <Button variant="secondary" size="sm" busy={busy} onClick={() => go(() => api.post('/profile/import', { source: 'persona' }), 'Persona résumé content')}>From Persona</Button>
        {bases.map((r) => (
          <Button key={r.id} variant="secondary" size="sm" busy={busy} onClick={() => go(() => api.post('/profile/import', { resume_id: r.id }), r.name)}>{r.name}</Button>
        ))}
      </div>
    </Notice>
  )
}

export default function Profile() {
  useTitle('Profile')
  const [prof, setProf] = useState(null)
  const [section, setSection] = useState(() => { try { return localStorage.getItem('jn_profile_section') || 'experience' } catch { return 'experience' } })
  const [adding, setAdding] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const { toasts, push: pushToast, dismiss } = useToasts()

  const load = useCallback(() => api.get('/profile').then(({ data }) => setProf(data))
    .catch((e) => pushToast({ kind: 'error', msg: 'Could not load profile — ' + errMsg(e, 'server unreachable') })), [pushToast])
  useEffect(() => { load() }, [load])
  useEffect(() => { try { localStorage.setItem('jn_profile_section', section) } catch { /* ignore */ } setAdding(false) }, [section])

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

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 12 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
          <PageTitle>Profile</PageTitle>
          <Helper>{prof ? `${facts.length} facts · ${unverifiedIds.length} awaiting review · version ${prof.profile_version}` : ' '}</Helper>
        </div>
        <span style={{ marginLeft: 'auto' }} />
        {unverifiedIds.length > 0 && <Button variant="secondary" size="sm" onClick={() => {
          if (window.confirm(`Mark all ${unverifiedIds.length} imported facts as verified? Only do this after reading them.`)) ops.verify(unverifiedIds)
        }}>Verify all ({unverifiedIds.length})</Button>}
        <Button variant="secondary" size="sm" onClick={() => setShowImport((v) => !v)}>Import résumé ↑</Button>
      </HeaderRow>

      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <nav style={{ width: 210, borderRight: '1px solid var(--line)', padding: '12px 8px', display: 'flex', flexDirection: 'column', gap: 2, overflow: 'auto' }}>
          {NAV.map(([id, label]) => (
            <div key={id} role="button" tabIndex={0} onClick={() => setSection(id)} onKeyDown={(e) => e.key === 'Enter' && setSection(id)}
              className="v2-profnav" style={{ display: 'flex', padding: '6px 10px', borderRadius: 'var(--radius-row)', cursor: 'pointer', fontSize: 13,
                background: section === id ? 'var(--accent-soft)' : undefined, color: section === id ? 'var(--accent)' : 'var(--text)' }}>
              <span style={{ flex: 1 }}>{label}</span>
              {counts[id] ? <Helper size="xs">{counts[id]}</Helper> : null}
            </div>
          ))}
          <RouterLink to="/answer-bank" style={{ padding: '6px 10px', fontSize: 13, color: 'var(--muted)' }}>Common answers →</RouterLink>
        </nav>

        <div className="v2-scroll" style={{ flex: 1, overflow: 'auto', padding: '16px 26px 30px', display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0 }}>
          {showImport && <ImportPanel pushToast={pushToast} onDone={() => { setShowImport(false); load() }} />}
          {!prof ? null : isFactSection ? (
            <>
              <Helper>
                {section === 'skill'
                  ? 'Link each skill to where you actually used it. A skill with no evidence never counts as a match.'
                  : 'Add everything true, including work that is not on your current résumé — tailoring picks from this pool and never writes anything that is not here.'}
              </Helper>
              {rows.map((f) => (
                <FactCard key={f.id} fact={f} schema={prof.schema} facts={facts} api={ops}>
                  {childrenOf(f.id).map((c) => <FactCard key={c.id} fact={c} schema={prof.schema} facts={facts} api={ops} depth={1} />)}
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
