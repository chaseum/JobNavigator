import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useParams, Link as RouterLink } from 'react-router-dom'
import { diffWords } from 'diff'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useTitle } from '../useTitle'
import { fetchRunOutcome, runFailed, runFailureReason } from '../hooks'
import { Button, Card, HeaderRow, Helper, Label, Notice, PageTitle, Pill, ScoreRing, Select, Tag, Textarea, Input } from '../ui'
import { headline } from './Profile'

// Job Detail: the main workflow. Analyze the posting -> Role Match (evidence
// coverage, not an employer's ATS score) -> gaps -> tailored résumé drafted from
// verified facts -> review every changed claim against its sources -> accept.

const STATUS_TONE = { MATCHED: 'good', PARTIAL: 'warn', MISSING: 'bad', UNKNOWN: 'neutral' }
const CLAIM_TONE = { SUPPORTED: 'good', AMBIGUOUS: 'warn', UNSUPPORTED: 'bad' }
const COMPONENT_LABEL = {
  eligibility: 'Eligibility / hard requirements', required: 'Required qualifications', preferred: 'Preferred qualifications',
  experience: 'Experience / responsibilities', technology: 'Technology / terminology', parser_health: 'Parser Health',
}
const GAP_GROUPS = [
  ['safe_to_add', 'Safe to add', 'In your profile, not on your current résumé.'],
  ['safe_to_rephrase', 'Safe to rephrase', 'On your résumé in different words; the job’s terms may apply where accurate.'],
  ['needs_clarification', 'Needs clarification', 'Possibly supported, but your profile does not say enough.'],
  ['cannot_claim', 'Cannot claim', 'Nothing in your profile supports these. They will not be added.'],
]
const errMsg = (e, fb) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fb)
const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)

function Section({ title, help, right, children }) {
  return (
    <Card style={{ padding: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '11px 14px', borderBottom: '1px solid var(--line-soft)' }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{title}</span>
        {help && <Helper style={{ flex: 1, minWidth: 0 }}>{help}</Helper>}
        {!help && <span style={{ flex: 1 }} />}
        {right}
      </div>
      <div style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>{children}</div>
    </Card>
  )
}

function MatchCard({ match, stale, minRecommended }) {
  if (!match) return null
  return (
    <Section title="Role Match" help="How much of this posting your verified profile can evidence. Not the employer’s ATS score."
      right={stale ? <Tag tone="warn" title="Your profile changed since this was computed">stale</Tag> : null}>
      <div style={{ display: 'flex', gap: 18, alignItems: 'center' }}>
        <ScoreRing value={match.score} size={64} />
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 5 }}>
          {match.components.filter((c) => c.coverage != null).map((c) => (
            <div key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
              <span style={{ width: 210 }}>{COMPONENT_LABEL[c.name] || c.name}</span>
              <div style={{ flex: 1, height: 6, background: 'var(--surface-2)', borderRadius: 3, overflow: 'hidden' }}>
                <div style={{ width: pct(c.coverage), height: '100%', background: 'var(--accent)' }} />
              </div>
              <span style={{ width: 44, textAlign: 'right' }}>{pct(c.coverage)}</span>
              <Helper size="xs" style={{ width: 70 }} title="share of the overall score">weight {c.effective_weight}%</Helper>
            </div>
          ))}
        </div>
      </div>
      <Helper>
        {Object.entries(match.counts).map(([k, n]) => `${n} ${k.toLowerCase()}`).join(' · ')}
        {minRecommended != null && ` · ${match.score >= minRecommended ? 'at or above' : 'below'} your recommended minimum (${minRecommended})`}
      </Helper>
      {match.hard_blockers?.length > 0 && (
        <Notice tone="bad"><strong style={{ fontSize: 12.5 }}>Hard requirements you do not meet</strong>
          {match.hard_blockers.map((b) => <Helper key={b}>{b}</Helper>)}</Notice>
      )}
    </Section>
  )
}

function RequirementMatrix({ requirements }) {
  if (!requirements.length) return null
  return (
    <Section title="Requirements" help="Every requirement, with the profile facts that support it.">
      <div className="v2-scroll" style={{ overflowX: 'auto' }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(160px, 2fr) 100px 72px 92px minmax(180px, 3fr)', gap: '6px 12px', fontSize: 12.5, alignItems: 'start', minWidth: 640 }}>
        <Label>Requirement</Label><Label>Category</Label><Label>Type</Label><Label>Status</Label><Label>Evidence</Label>
        {requirements.map((r) => (
          <React.Fragment key={r.id}>
            <span>{r.text}</span>
            <Helper>{r.category.replace(/_/g, ' ')}</Helper>
            <Helper>{r.required ? 'required' : 'preferred'}</Helper>
            <span><Tag tone={STATUS_TONE[r.status]}>{r.status}</Tag></span>
            <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              {r.evidence.length ? r.evidence.map((e) => <span key={e.ref}>• {e.headline} <Helper size="xs">({e.ref})</Helper></span>) : <Helper>none</Helper>}
              {r.explanation && <Helper size="xs">{r.explanation}</Helper>}
            </span>
          </React.Fragment>
        ))}
      </div>
      </div>
    </Section>
  )
}

function AddContext({ gap, jobId, entries, onSaved, pushToast }) {
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState('achievement')
  const [parent, setParent] = useState(entries[0]?.id ?? null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  if (!open) return <Pill size="sm" onClick={() => setOpen(true)}>+ Add context</Pill>
  const save = async () => {
    setBusy(true)
    try {
      const body = kind === 'achievement'
        ? { kind, parent_id: parent, data: { text } }
        : { kind: 'skill', data: { name: text, evidence_ids: parent != null ? [entries.find((e) => e.id === parent)?.ref].filter(Boolean) : [] } }
      await api.post(`/copilot/jobs/${jobId}/context`, body)
      pushToast({ kind: 'success', msg: 'Saved to your profile — re-matching' })
      setOpen(false); setText(''); onSaved()
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not save') }) } finally { setBusy(false) }
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 8, background: 'var(--surface-2)', borderRadius: 'var(--radius-row)' }}>
      <Helper>Only add what is true. It becomes a verified profile fact before anything uses it.</Helper>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <Select value={kind} options={[['achievement', 'Accomplishment'], ['skill', 'Skill used']]} onPick={setKind} width={150} />
        <Select value={parent == null ? '' : String(parent)} options={entries.map((e) => [String(e.id), e.label])} onPick={(v) => setParent(Number(v))} width={280} placeholder="Where it happened" />
      </div>
      {kind === 'achievement'
        ? <Textarea rows={2} value={text} onChange={setText} placeholder={`What you actually did that relates to “${gap.requirement}”`} />
        : <Input value={text} onChange={setText} placeholder="Skill name" />}
      <div style={{ display: 'flex', gap: 6 }}>
        <Button size="sm" busy={busy} disabled={!text.trim() || parent == null} onClick={save}>Save to profile</Button>
        <Button size="sm" variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
        <RouterLink to="/profile" style={{ fontSize: 12, alignSelf: 'center' }}>Other kind of fact → Profile</RouterLink>
      </div>
    </div>
  )
}

function Gaps({ gaps, jobId, entries, onSaved, pushToast }) {
  if (!gaps.length) return null
  return (
    <Section title="Gap analysis">
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 10 }}>
        {GAP_GROUPS.map(([kind, title, help]) => {
          const rows = gaps.filter((g) => g.kind === kind)
          return (
            <div key={kind} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <Label>{title} · {rows.length}</Label>
              <Helper size="xs">{help}</Helper>
              {rows.map((g) => (
                <div key={g.requirement_id} style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12.5, borderTop: '1px solid var(--line-soft)', paddingTop: 5 }}>
                  <span>{g.requirement}</span>
                  {(kind === 'cannot_claim' || kind === 'needs_clarification') &&
                    <AddContext gap={g} jobId={jobId} entries={entries} onSaved={onSaved} pushToast={pushToast} />}
                </div>
              ))}
            </div>
          )
        })}
      </div>
    </Section>
  )
}

// ── résumé review ────────────────────────────────────────────────────────────

function Diff({ from, to }) {
  return (
    <span>
      {diffWords(from || '', to || '').map((p, i) => (
        <span key={i} style={p.added ? { background: 'var(--change-soft)', borderRadius: 3 } : p.removed ? { background: 'var(--bad-soft)', textDecoration: 'line-through', opacity: 0.75, borderRadius: 3 } : undefined}>{p.value}</span>
      ))}
    </span>
  )
}

// pair each tailored bullet with the base bullet of the same entry that shares a source fact
function pairEntries(base, tailored) {
  const baseEntries = new Map()
  ;(base?.sections || []).forEach((s) => (s.entries || []).forEach((e) => baseEntries.set(e.fact_id, e)))
  return (tailored?.sections || []).map((s) => ({
    ...s,
    entries: (s.entries || []).map((e) => {
      const be = baseEntries.get(e.fact_id)
      const used = new Set()
      const bullets = (e.bullets || []).map((b) => {
        const match = (be?.bullets || []).find((x) => !used.has(x.id) && x.source_fact_ids.some((id) => b.source_fact_ids.includes(id)))
        if (match) used.add(match.id)
        return { ...b, baseText: match ? match.text : null }
      })
      const removed = (be?.bullets || []).filter((x) => !used.has(x.id))
      return { ...e, bullets, removed, newEntry: !be }
    }),
  }))
}

function BulletReview({ b, claim, version, requirements, factHeadlines, onAction, busy }) {
  const [inspect, setInspect] = useState(false)
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(b.text)
  const draft = version.status === 'draft'
  const changed = b.baseText == null || b.baseText !== b.text
  const kind = b.baseText == null ? 'added' : changed ? 'changed' : 'unchanged'
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, padding: '6px 0', borderTop: '1px solid var(--line-soft)' }}>
      <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>{b.baseText ?? <Helper size="xs">— not on the base résumé —</Helper>}</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div style={{ fontSize: 12.5 }}>{kind === 'changed' ? <Diff from={b.baseText} to={b.text} /> : kind === 'added' ? <span style={{ background: 'var(--change-soft)' }}>{b.text}</span> : b.text}</div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          <Tag tone={CLAIM_TONE[claim?.status] || 'neutral'}>{claim?.status || '—'}</Tag>
          <Helper size="xs">{kind}{b.edited ? ' · edited by you' : ''}{claim?.reviewed ? ' · reviewed' : ''}</Helper>
          <span style={{ flex: 1 }} />
          <Pill size="sm" onClick={() => setInspect((v) => !v)}>{inspect ? 'Hide' : 'Inspect'}</Pill>
          {draft && changed && <>
            <Pill size="sm" disabled={busy || claim?.status === 'UNSUPPORTED' || claim?.reviewed} onClick={() => onAction(b.id, 'accept')}>Accept</Pill>
            <Pill size="sm" disabled={busy} onClick={() => onAction(b.id, 'reject')}>Reject</Pill>
          </>}
          {draft && version.kind === 'tailored' && <Pill size="sm" disabled={busy} onClick={() => onAction(b.id, 'regenerate')}>Regenerate</Pill>}
          {draft && <Pill size="sm" disabled={busy} onClick={() => { setText(b.text); setEditing((v) => !v) }}>Edit</Pill>}
        </div>
        {claim?.reasons?.length > 0 && <Helper size="xs" style={{ color: claim.status === 'UNSUPPORTED' ? 'var(--bad)' : undefined }}>{claim.reasons.join(' · ')}</Helper>}
        {editing && (
          <div style={{ display: 'flex', gap: 6 }}>
            <Textarea rows={2} value={text} onChange={setText} style={{ flex: 1 }} />
            <Button size="sm" busy={busy} onClick={async () => { await onAction(b.id, 'edit', text); setEditing(false) }}>Save</Button>
          </div>
        )}
        {inspect && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, padding: 8, background: 'var(--surface-2)', borderRadius: 'var(--radius-row)', fontSize: 12 }}>
            <Label>Source facts</Label>
            {b.source_fact_ids.map((id) => <span key={id}>• {factHeadlines[id] || id} <Helper size="xs">({id})</Helper></span>)}
            <Label>Job requirements addressed</Label>
            {(b.requirement_ids || []).length ? b.requirement_ids.map((rid) => <span key={rid}>• {requirements[rid] || rid}</span>) : <Helper>none</Helper>}
            <Label>Reason for change</Label>
            <span>{b.reason || '—'}</span>
          </div>
        )}
      </div>
    </div>
  )
}

function ResumeReview({ versionId, factHeadlines, onChanged, pushToast }) {
  const [v, setV] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => api.get(`/resume-versions/${versionId}`).then(({ data }) => setV(data)), [versionId])
  useEffect(() => { load() }, [load])
  const requirements = useMemo(() => Object.fromEntries(((v?.job_analysis?.analysis?.requirements) || []).map((r) => [r.id, r.text])), [v])
  const claims = useMemo(() => Object.fromEntries(((v?.audit?.claims) || []).map((c) => [c.bullet_id, c])), [v])
  if (!v) return <Helper>Loading…</Helper>
  const run = async (fn, ok) => {
    setBusy(true)
    try { await fn(); if (ok) pushToast({ kind: 'success', msg: ok }); await load(); onChanged() } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Request failed') }) } finally { setBusy(false) }
  }
  const onAction = (bid, action, text) => run(() => (action === 'regenerate'
    ? api.post(`/resume-versions/${v.id}/bullets/${encodeURIComponent(bid)}/regenerate`)
    : api.post(`/resume-versions/${v.id}/bullets/${encodeURIComponent(bid)}`, { action, text })))
  const paired = pairEntries(v.base?.resume, v.resume)
  const draft = v.status === 'draft'
  const counts = v.counts || {}
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <Tag tone={v.status === 'accepted' ? 'good' : v.status === 'rejected' ? 'neutral' : 'accent'}>{v.status}</Tag>
        <Helper>{counts.SUPPORTED ?? 0} supported · {counts.AMBIGUOUS ?? 0} ambiguous · {counts.UNSUPPORTED ?? 0} unsupported
          {v.parser_health != null && ` · Parser Health ${v.parser_health}`}{v.pages ? ` · ${v.pages} page${v.pages === 1 ? '' : 's'}` : ''}</Helper>
        <span style={{ flex: 1 }} />
        {v.pages ? <Button size="sm" variant="secondary" as="a" href={`/api/resume-versions/${v.id}/pdf`} target="_blank" rel="noopener">PDF ↗</Button> : null}
        <Button size="sm" variant="secondary" as="a" href={`/api/resume-versions/${v.id}/tex`}>.tex</Button>
        <Button size="sm" variant="secondary" as="a" href={`/api/resume-versions/${v.id}/export.zip`} title="Overleaf-compatible project">Overleaf ZIP</Button>
        {v.status === 'accepted' && <Button size="sm" variant="secondary" busy={busy} title="Push this accepted version to your Overleaf Git remote (Settings › Copilot)"
          onClick={() => run(() => api.post(`/resume-versions/${v.id}/overleaf`), 'Pushed to Overleaf')}>Push to Overleaf</Button>}
        {draft && <>
          <Button size="sm" variant="secondary" busy={busy} onClick={() => run(() => api.post(`/resume-versions/${v.id}/rebuild`), 'PDF rebuilt')}>Rebuild PDF</Button>
          <Button size="sm" variant="secondary" busy={busy} onClick={() => window.confirm('Discard this draft?') && run(() => api.post(`/resume-versions/${v.id}/reject`), 'Draft rejected')}>Reject all</Button>
          <Button size="sm" busy={busy} disabled={v.blocked} title={v.blocked ? 'Unsupported claims must be rejected or edited first' : 'Accept every remaining change and freeze this version'}
            onClick={() => run(() => api.post(`/resume-versions/${v.id}/accept`, { all: true }), 'Accepted — autofill will upload this résumé for this job')}>Accept all</Button>
        </>}
      </div>
      {v.blocked && <Notice tone="bad"><strong style={{ fontSize: 12.5 }}>PDF blocked</strong><Helper>Some claims are not supported by your profile. Reject or edit them; nothing is fixed silently.</Helper></Notice>}
      {!v.base && <Notice tone="quiet"><Helper>No accepted base résumé yet, so every bullet shows as added. Generate and accept one on the <RouterLink to="/resumes">Résumés</RouterLink> screen for a real diff.</Helper></Notice>}
      {v.compile_log && !v.blocked && !v.pages && <Notice tone="bad"><strong style={{ fontSize: 12.5 }}>LaTeX did not compile</strong><pre style={{ fontSize: 11, whiteSpace: 'pre-wrap', margin: 0 }}>{v.compile_log}</pre></Notice>}
      {v.parser_health_detail && (
        <Helper>Parser Health: {v.parser_health_detail.checks.map((c) => `${c.ok ? '✓' : '✗'} ${c.name}${c.detail ? ` (${c.detail})` : ''}`).join(' · ')}</Helper>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}><Label>Base résumé</Label><Label>Tailored</Label></div>
      {paired.map((s) => (
        <div key={s.id} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <strong style={{ fontSize: 13 }}>{s.title}</strong>
          {(s.lines || []).map((l) => <Helper key={l.label}>{l.label}: {l.items.join(', ')}</Helper>)}
          {(s.entries || []).map((e) => (
            <div key={e.fact_id} style={{ paddingLeft: 4 }}>
              <div style={{ fontSize: 12.5, fontWeight: 600 }}>{e.heading} <Helper size="xs" style={{ display: 'inline' }}>· {e.subheading} · {e.date}</Helper>{e.newEntry && <Tag tone="accent" style={{ marginLeft: 6 }}>added</Tag>}</div>
              {e.bullets.map((b) => <BulletReview key={b.id} b={b} claim={claims[b.id]} version={v} requirements={requirements} factHeadlines={factHeadlines} onAction={onAction} busy={busy} />)}
              {e.removed.map((b) => (
                <div key={b.id} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, padding: '6px 0', borderTop: '1px solid var(--line-soft)' }}>
                  <span style={{ fontSize: 12.5, textDecoration: 'line-through', background: 'var(--bad-soft)' }}>{b.text}</span>
                  <Helper size="xs">removed for this job</Helper>
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

export default function JobDetail() {
  const { id } = useParams()
  const [ws, setWs] = useState(null)
  const [versions, setVersions] = useState([])
  const [selected, setSelected] = useState(null)
  const [profile, setProfile] = useState(null)
  const [privacy, setPrivacy] = useState(null)
  const [settings, setSettings] = useState({})
  const [pending, setPending] = useState([])   // [{run_id, type}]
  const [reviewKey, setReviewKey] = useState(0)
  const { toasts, push: pushToast, dismiss } = useToasts()
  useTitle(ws?.job ? `${ws.job.title} — ${ws.job.company}` : 'Job')

  const load = useCallback(async () => {
    try {
      const [w, vs] = await Promise.all([api.get(`/copilot/jobs/${id}`), api.get('/resume-versions', { params: { job_id: id } })])
      setWs(w.data); setVersions(vs.data)
      setSelected((cur) => cur && vs.data.some((x) => x.id === cur) ? cur : (vs.data.find((x) => x.status !== 'rejected') || {}).id || null)
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not load this job') }) }
  }, [id, pushToast])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    api.get('/profile').then(({ data }) => setProfile(data)).catch(() => {})
    api.get('/copilot/privacy').then(({ data }) => setPrivacy(data)).catch(() => {})
    api.get('/settings').then(({ data }) => setSettings(data || {})).catch(() => {})
  }, [])

  // poll while anything this screen started (or the server says is running) is in flight
  const pendingRef = useRef(pending)
  pendingRef.current = pending
  useEffect(() => {
    if (!pending.length && !(ws?.running?.length)) return undefined
    const t = setInterval(async () => {
      const still = []
      for (const p of pendingRef.current) {
        const out = await fetchRunOutcome(p.run_id, p.type)
        if (!out || out.status === 'running') still.push(p)
        else if (runFailed(out)) pushToast({ kind: 'error', msg: `${p.label} failed — ${runFailureReason(out)}` })
        else pushToast({ kind: 'success', msg: out.result_summary || `${p.label} finished` })
      }
      setPending(still)
      await load()
      setReviewKey((k) => k + 1)
    }, 2500)
    return () => clearInterval(t)
  }, [pending.length, ws?.running?.length, load, pushToast])

  const start = async (url, type, label) => {
    try {
      const { data } = await api.post(url)
      setPending((p) => [...p, { run_id: data.run_id, type, label }])
      pushToast({ kind: 'progress', msg: `${label}…` })
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, `${label} could not start`) }) }
  }

  const factHeadlines = useMemo(() => Object.fromEntries((profile?.facts || []).map((f) => [f.ref, headline(f.kind, f.data)])), [profile])
  const contextEntries = useMemo(() => (profile?.facts || []).filter((f) => f.verified && ['experience', 'internship', 'project', 'research', 'education'].includes(f.kind))
    .map((f) => ({ id: f.id, ref: f.ref, label: headline(f.kind, f.data).slice(0, 60) })), [profile])
  const copilotPrivacy = privacy?.features?.find((f) => f.feature === 'copilot')

  if (!ws) return <div style={{ padding: 30 }}><Helper>Loading…</Helper><ToastStack toasts={toasts} onClose={dismiss} /></div>
  const { job } = ws
  const busy = pending.length > 0 || ws.running.length > 0
  const canGenerate = ws.analysis && ws.match

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 10, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0, flex: '1 1 280px' }}>
          <PageTitle>{job.title || 'Untitled role'}</PageTitle>
          <Helper>{[job.company, job.location, ws.application ? `application: ${ws.application.status.replace(/_/g, ' ')}` : null].filter(Boolean).join(' · ')}
            {job.url && <> · <a href={job.url} target="_blank" rel="noopener noreferrer">posting ↗</a></>}</Helper>
        </div>
        <span style={{ flex: 1 }} />
        {copilotPrivacy && <Tag tone={copilotPrivacy.external ? 'warn' : 'neutral'} title={copilotPrivacy.external ? `Job text and your verified profile facts are sent to ${copilotPrivacy.provider}` : `Runs on ${copilotPrivacy.destination}`}>
          {copilotPrivacy.external ? `sends data to ${copilotPrivacy.provider}` : `local · ${copilotPrivacy.model || 'no model set'}`}</Tag>}
        <Button size="sm" variant="secondary" busy={busy} onClick={() => start(`/copilot/jobs/${id}/analyze`, 'copilot_analyze', 'Analysis')}>{ws.analysis ? 'Re-analyze' : 'Analyze job'}</Button>
        {ws.analysis && <Button size="sm" variant={ws.stale ? 'primary' : 'secondary'} busy={busy} onClick={() => start(`/copilot/jobs/${id}/match`, 'copilot_match', 'Re-match')}>Re-match</Button>}
        {canGenerate && <Button size="sm" busy={busy} onClick={() => start(`/resume-versions/for-job/${id}`, 'copilot_resume', 'Résumé draft')}>Generate résumé</Button>}
      </HeaderRow>

      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto', padding: '16px 26px 40px', display: 'flex', flexDirection: 'column', gap: 12 }}>
        {!ws.analysis && (
          <Notice tone="quiet"><Helper>
            {job.has_description || job.url ? 'Analyze this posting to extract its requirements and check them against your verified profile.' : 'This job has no description or URL to analyze.'}
            {profile && profile.facts.filter((f) => f.verified).length === 0 && <> Your <RouterLink to="/profile">profile</RouterLink> has no verified facts yet, so nothing can match.</>}
          </Helper></Notice>
        )}
        <MatchCard match={ws.match} stale={ws.stale} minRecommended={settings.role_match_min_recommended ? Number(settings.role_match_min_recommended) : null} />
        <Gaps gaps={ws.gaps} jobId={id} entries={contextEntries} onSaved={() => { load(); api.get('/profile').then(({ data }) => setProfile(data)) }} pushToast={pushToast} />
        <RequirementMatrix requirements={ws.requirements} />
        {ws.analysis && (
          <Section title="Résumé" help="Drafted only from verified facts. Every changed claim is checked against its sources before you can accept it."
            right={versions.length > 0 && <Select value={selected || ''} width={300}
              options={versions.map((x) => [x.id, `${x.status} · ${new Date(x.created_at).toLocaleString()}${x.blocked ? ' · blocked' : ''}`])} onPick={setSelected} />}>
            {selected ? <ResumeReview key={`${selected}:${reviewKey}`} versionId={selected} factHeadlines={factHeadlines} onChanged={load} pushToast={pushToast} />
              : <Helper>No résumé for this job yet. {canGenerate ? 'Generate one above.' : ''}</Helper>}
          </Section>
        )}
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
