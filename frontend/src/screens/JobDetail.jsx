import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useParams, useSearchParams, useNavigate, Link as RouterLink } from 'react-router-dom'
import { diffWords } from 'diff'
import { Heart } from 'lucide-react'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useTitle } from '../useTitle'
import { ago } from '../time'
import { fetchRunOutcome, runFailed, runFailureReason } from '../hooks'
import { Button, Card, Helper, IconButton, Input, Label, Notice, Pill, ScoreRing, Select, Spinner, Tag, Textarea, scoreTone } from '../ui'
import { headline } from './Profile'
import { ARRANGEMENTS, Logo, MATCH_LABEL, cap, fmtSalary } from './Jobs'

// Job Detail: one workspace per posting. Three separate measurements, never mixed:
// Candidate Fit (what the whole verified profile satisfies — the header), Resume
// Applicability (what one specific résumé shows of that, with the truthful maximum)
// and Parser Health (does the PDF extract cleanly). Tabs: Overview (fit, blockers,
// strongest matches, gaps), Resume (audit → tailor → re-audit, review), Application
// (what autofill sends) and Evidence (formula, requirement matrix, provenance).

const TABS = [['overview', 'Overview'], ['resume', 'Resume'], ['application', 'Application'], ['evidence', 'Evidence']]
const STATUS_TONE = { MATCHED: 'good', PARTIAL: 'warn', MISSING: 'bad', UNKNOWN: 'neutral' }
const CLAIM_TONE = { SUPPORTED: 'good', AMBIGUOUS: 'warn', UNSUPPORTED: 'bad' }
const RESUME_TONE = { PRESENT: 'good', WEAK: 'warn', OMITTED_SUPPORTED: 'accent', UNSUPPORTED: 'bad', UNKNOWN: 'neutral' }
const RESUME_LABEL = { PRESENT: 'Present', WEAK: 'Weak', OMITTED_SUPPORTED: 'Omitted, supported', UNSUPPORTED: 'Unsupported', UNKNOWN: 'Unknown' }
const GAP_GROUPS = [
  ['SAFE_TO_ADD', '✓', 'Safe to add', 'Verified in your Profile but not shown on this résumé.'],
  ['SAFE_TO_REPHRASE', '~', 'Truthful rephrase', 'Shown, but not in the posting’s terms; its wording may be used where accurate.'],
  ['NEEDS_CONTEXT', '?', 'Needs context', 'Possibly true, but your verified Profile does not establish it.'],
  ['CANNOT_CLAIM', '✗', 'Cannot claim', 'Your verified Profile does not support these. Tailoring will not add them.'],
  ['ALREADY_VISIBLE', '●', 'Already visible', 'Supported and shown in the posting’s terms.'],
]
const ACTIONABLE = ['SAFE_TO_ADD', 'SAFE_TO_REPHRASE']
const errMsg = (e, fb) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fb)
const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)
const byWeight = (a, b) => (b.required - a.required) || ((b.importance || 2) - (a.importance || 2))
const when = (iso) => (iso ? new Date(iso).toLocaleString() : '—')

function Section({ title, help, right, children, id }) {
  return (
    <Card id={id} style={{ padding: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', borderBottom: '1px solid var(--line-soft)', flexWrap: 'wrap' }}>
        <span style={{ fontSize: 'var(--t-14)', fontWeight: 'var(--weight-semibold)' }}>{title}</span>
        {help && <Helper style={{ flex: '1 1 240px', minWidth: 0 }}>{help}</Helper>}
        {!help && <span style={{ flex: 1 }} />}
        {right}
      </div>
      <div style={{ padding: '12px 16px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>{children}</div>
    </Card>
  )
}

// ── Evidence tab ─────────────────────────────────────────────────────────────

function EvidenceList({ refs, headlines, empty = 'none' }) {
  if (!refs?.length) return <Helper size="xs">{empty}</Helper>
  return refs.map((ref) => (
    <span key={ref} style={{ fontSize: 'var(--t-12)' }}>• {headlines[ref] || ref} <Helper size="xs" style={{ display: 'inline' }}>({ref})</Helper></span>
  ))
}

function ScoringMethod({ match, audit }) {
  if (!match) return null
  return (
    <Section title="How the numbers are computed" help="Deterministic from the requirement matrix below; no model ever produces a score.">
      {match.formula
        ? <Helper>{match.formula}</Helper>
        : <Notice tone="warn"><Helper>This match was computed by the retired Role Match scorer. Re-match to compute Candidate Fit.</Helper></Notice>}
      {match.weights && <Helper>Weights: {Object.entries(match.weights).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(' · ')} (Settings › Scoring weights)</Helper>}
      <Helper>Credit — Candidate Fit: MATCHED 1, PARTIAL 0.5, UNKNOWN or MISSING 0. Resume Applicability: PRESENT 1, WEAK 0.5, omitted, unsupported or unknown 0, and never more than the profile supports.</Helper>
      {match.coverage && <Helper>Profile coverage: required {pct(match.coverage.required)} · preferred {pct(match.coverage.preferred)}</Helper>}
      {match.unavailable && <Notice tone="bad"><Helper>Match unavailable: {match.unavailable}</Helper></Notice>}
      {audit && <Helper>{audit.disclaimer}</Helper>}
    </Section>
  )
}

function RequirementMatrix({ requirements, audit, headlines }) {
  if (!requirements.length) return null
  const resume = Object.fromEntries((audit?.rows || []).map((r) => [r.requirement_id, r]))
  return (
    <Section title="Requirement matrix" help={`Candidate evidence comes from your whole verified Profile; résumé evidence from ${audit ? 'the base résumé' : 'a base résumé, once one exists'}.`}>
      <div className="v2-scroll" style={{ overflowX: 'auto' }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(180px, 2fr) minmax(220px, 3fr) minmax(200px, 2fr)', gap: '10px 14px', fontSize: 'var(--t-13)', alignItems: 'start', minWidth: 720 }}>
          <Label>Requirement</Label><Label>Candidate (verified Profile)</Label><Label>Base résumé</Label>
          {requirements.map((r) => {
            const rr = resume[r.id]
            return (
              <React.Fragment key={r.id}>
                <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  <span>{r.text}</span>
                  <Helper size="xs">{r.category.replace(/_/g, ' ')} · {r.required ? 'required' : 'preferred'} · importance {r.importance || 2}{rr?.weight ? ` · weight ${rr.weight}` : ''}</Helper>
                </span>
                <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  <span><Tag tone={STATUS_TONE[r.status]}>{r.status}</Tag></span>
                  {r.evidence.length ? r.evidence.map((e) => <span key={e.ref} style={{ fontSize: 'var(--t-12)' }}>• {e.headline} <Helper size="xs" style={{ display: 'inline' }}>({e.ref})</Helper></span>) : <Helper size="xs">no evidence</Helper>}
                  {r.explanation && <Helper size="xs">{r.explanation}</Helper>}
                  <Helper size="xs">
                    {r.rules?.length ? `decided by rule: ${r.rules.join(', ')}` : 'model citation, checked'}
                    {r.llm_status && r.llm_status !== r.status ? ` · model said ${r.llm_status}` : ''}
                  </Helper>
                  {r.dropped_source_fact_ids?.length > 0 && <Helper size="xs" style={{ color: 'var(--bad)' }}>dropped citations: {r.dropped_source_fact_ids.join(', ')}</Helper>}
                </span>
                <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  {rr?.resume_status
                    ? <><span><Tag tone={RESUME_TONE[rr.resume_status]}>{RESUME_LABEL[rr.resume_status]}</Tag></span><EvidenceList refs={rr.resume_evidence} headlines={headlines} empty="not shown" /></>
                    : <Helper size="xs">{rr ? 'eligibility — never scored' : '—'}</Helper>}
                </span>
              </React.Fragment>
            )
          })}
        </div>
      </div>
    </Section>
  )
}

function Provenance({ ws, privacy }) {
  const a = ws.analysis
  if (!a) return null
  return (
    <Section title="Provenance" help="Where this match came from, so it can be reproduced.">
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '10px 16px' }}>
        {[
          ['Analyzed', when(a.created_at)],
          ['Model', [a.provider, a.model].filter(Boolean).join(' / ') || '—'],
          ['Analysis record', `#${a.id}`],
          ['Requirements extracted', ws.requirements.length],
          ['Profile match', ws.stale ? 'Stale: your profile changed since' : 'Current profile version'],
          ['Data sent', privacy ? (privacy.external ? `Job text and verified facts to ${privacy.provider}` : `Local · ${privacy.destination}`) : '—'],
        ].map(([k, v]) => (
          <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <Label>{k}</Label>
            <span style={{ fontSize: 'var(--t-13)' }}>{v}</span>
          </div>
        ))}
      </div>
    </Section>
  )
}

function AddContext({ gap, jobId, entries, onSaved, pushToast }) {
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState('achievement')
  const [parent, setParent] = useState(null)   // the user picks where it happened; nothing is inferred
  const [text, setText] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  if (!open) return <Pill size="sm" onClick={() => setOpen(true)} style={{ alignSelf: 'flex-start' }}>+ Add context</Pill>
  const save = async () => {
    setBusy(true)
    try {
      const body = kind === 'achievement'
        ? { kind, parent_id: parent, data: { text }, confirmed }
        : { kind: 'skill', data: { name: text, evidence_ids: parent != null ? [entries.find((e) => e.id === parent)?.ref].filter(Boolean) : [] }, confirmed }
      await api.post(`/copilot/jobs/${jobId}/context`, body)
      pushToast({ kind: 'success', msg: 'Verified and saved to your Profile — re-matching and re-auditing' })
      setOpen(false); setText(''); setConfirmed(false); onSaved()
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not save') }) } finally { setBusy(false) }
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 10, background: 'var(--surface-2)', borderRadius: 'var(--radius-row)' }}>
      {gap.question && <span style={{ fontSize: 'var(--t-13)' }}>{gap.question}</span>}
      <Helper>If it isn’t true, leave it out. Nothing you type is used until you confirm it below.</Helper>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <Select value={kind} options={[['achievement', 'Accomplishment'], ['skill', 'Skill used']]} onPick={setKind} width="150px" />
        <Select value={parent == null ? '' : String(parent)} options={entries.map((e) => [String(e.id), e.label])} onPick={(v) => setParent(Number(v))} width="280px" placeholder="Where it happened" />
      </div>
      {kind === 'achievement'
        ? <Textarea rows={2} value={text} onChange={setText} placeholder={`What you actually did that relates to “${gap.requirement}”`} />
        : <Input value={text} onChange={setText} placeholder="Skill name" />}
      <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 'var(--t-13)', cursor: 'pointer' }}>
        <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} style={{ accentColor: 'var(--accent)', marginTop: 3 }} />
        <span>I confirm this is true and happened where I selected. Verify it as a Profile fact that résumés may cite.</span>
      </label>
      <div style={{ display: 'flex', gap: 6 }}>
        <Button size="sm" busy={busy} disabled={!text.trim() || parent == null || !confirmed} onClick={save}>Verify and save</Button>
        <Button size="sm" variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
        <RouterLink to="/profile" style={{ fontSize: 'var(--t-12)', alignSelf: 'center' }}>Other kind of fact → Profile</RouterLink>
      </div>
    </div>
  )
}

function GapRow({ r, headlines, jobId, entries, onSaved, pushToast }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5, padding: '10px 0', borderTop: '1px solid var(--line-soft)', fontSize: 'var(--t-13)' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <span style={{ flex: '1 1 260px', minWidth: 0 }}>{r.requirement}</span>
        <Helper size="xs">{r.required ? 'required' : 'preferred'}</Helper>
        {ACTIONABLE.includes(r.gap) && r.points_now > 0 && <Tag tone="accent" title="What this truthful change can add to Resume Applicability">+{r.points_now} pts</Tag>}
        {!ACTIONABLE.includes(r.gap) && r.points_with_context > 0 && <Tag title="Only reachable with new, verified context">up to +{r.points_with_context} pts with context</Tag>}
      </div>
      {r.source_quote && <Helper>Posting: “{r.source_quote}”</Helper>}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 10 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}><Label>Profile</Label><Tag tone={STATUS_TONE[r.candidate_status]}>{r.candidate_status}</Tag></div>
          <EvidenceList refs={r.profile_evidence} headlines={headlines} />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}><Label>This résumé</Label><Tag tone={RESUME_TONE[r.resume_status]}>{RESUME_LABEL[r.resume_status]}</Tag></div>
          <EvidenceList refs={r.resume_evidence} headlines={headlines} empty="not shown" />
        </div>
      </div>
      <Helper>Why: {r.reason}</Helper>
      {!ACTIONABLE.includes(r.gap) && r.gap !== 'ALREADY_VISIBLE' && <AddContext gap={r} jobId={jobId} entries={entries} onSaved={onSaved} pushToast={pushToast} />}
    </div>
  )
}

function GapAudit({ audit, title, headlines, jobId, entries, onSaved, pushToast }) {
  const [showVisible, setShowVisible] = useState(false)
  if (!audit) return null
  const eligibility = audit.rows.filter((r) => r.resume_status == null)
  return (
    <Section title={title} help="Each requirement: what your verified Profile proves, what this résumé shows, and why.">
      {GAP_GROUPS.map(([kind, glyph, label, help]) => {
        const rows = audit.rows.filter((r) => r.gap === kind)
        if (!rows.length) return null
        if (kind === 'ALREADY_VISIBLE' && !showVisible) {
          return <Button key={kind} size="xs" variant="ghost" onClick={() => setShowVisible(true)} style={{ alignSelf: 'flex-start' }}>Show {rows.length} already visible</Button>
        }
        return (
          <div key={kind} style={{ display: 'flex', flexDirection: 'column', paddingTop: 6 }}>
            <Label size="lg">{glyph} {label} · {rows.length}</Label>
            <Helper size="xs">{help}</Helper>
            {rows.map((r) => <GapRow key={r.requirement_id} r={r} headlines={headlines} jobId={jobId} entries={entries} onSaved={onSaved} pushToast={pushToast} />)}
          </div>
        )
      })}
      {eligibility.length > 0 && (
        <Helper>Eligibility (from your Profile answers, never scored): {eligibility.map((r) => `${r.requirement} — ${r.candidate_status}`).join(' · ')}</Helper>
      )}
    </Section>
  )
}

function Figure({ label, value, sub }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
      <ScoreRing value={value} label="—" size={52} ariaLabel={`${label}: ${value ?? 'not available'}`} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
        <Label>{label}</Label>
        {sub && <Helper size="xs">{sub}</Helper>}
      </div>
    </div>
  )
}

function ApplicabilitySummary({ ra, busy, auditing, onAudit, onGenerate, onCreateBase, creatingBase }) {
  const { base, tailored: t, comparison: c } = ra
  if (!base) {
    return (
      <Notice tone="quiet" glyph="○" action={<Button size="sm" busy={creatingBase} onClick={onCreateBase}>Create base résumé from Profile</Button>}>
        <strong style={{ fontSize: 'var(--t-13)' }}>No base résumé to audit yet</strong>
        <Helper>Resume Applicability audits one specific résumé. A base résumé renders your verified Profile facts; review and accept it on the <RouterLink to="/resumes">Resume</RouterLink> screen.</Helper>
      </Notice>
    )
  }
  const g = base.gap_counts || {}
  return (
    <Section title="Resume Applicability" help={ra.disclaimer}
      right={<Button size="xs" variant="secondary" busy={auditing} onClick={onAudit} title="Re-run the audit on the base and tailored résumés and store the result">Audit résumé</Button>}>
      <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', alignItems: 'center' }}>
        <Figure label="Base résumé" value={base.score} sub={`${cap(base.version_status)} base${base.parser_health != null ? ` · Parser Health ${base.parser_health}` : ''}`} />
        <Figure label="Maximum currently achievable" value={base.maximum} sub="what your verified Profile supports" />
        {t && <Figure label="Tailored résumé" value={t.score} sub={`${c?.delta != null ? `${c.delta >= 0 ? '+' : ''}${c.delta} points · ` : ''}${cap(t.version_status)}`} />}
      </div>
      <span style={{ fontSize: 'var(--t-13)' }}>{(t || base).message}</span>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        <Label>What keeps the base résumé from {ra.target}–95%</Label>
        {[['SAFE_TO_ADD', '✓', 'var(--good)', 'safe addition(s) from your verified Profile'], ['SAFE_TO_REPHRASE', '~', 'var(--warn)', 'truthful rephrase(s)'],
          ['NEEDS_CONTEXT', '?', 'var(--warn)', 'item(s) need more context'], ['CANNOT_CLAIM', '✗', 'var(--bad)', 'requirement(s) your Profile does not support']].map(([k, glyph, color, text]) => (
          <span key={k} style={{ fontSize: 'var(--t-13)' }}><span aria-hidden="true" style={{ color, fontWeight: 'var(--weight-semibold)' }}>{glyph}</span> {g[k] || 0} {text}</span>
        ))}
      </div>
      {!base.target_reachable && base.maximum != null && (
        <Notice tone="warn"><Helper>{ra.target}–95% is not currently achievable without additional truthful experience or context. Tailoring will not invent the difference; answer the “Needs context” questions below if they apply to you.</Helper></Notice>
      )}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
        <Button size="sm" busy={busy} onClick={onGenerate}>{t ? 'Tailor again toward best truthful match' : 'Tailor toward best truthful match'}</Button>
        <Helper size="xs">Verified facts only. Every bullet keeps its source facts, and a bullet that fails the claim audit counts for nothing.</Helper>
      </div>
    </Section>
  )
}

function BeforeAfter({ ra, headlines }) {
  const { base: b, tailored: t, comparison: c } = ra
  if (!b || !t || !c) return null
  const rows = [
    ['Resume Applicability', `${b.score ?? '—'}%`, `${t.score ?? '—'}%`],
    ['Parser Health', b.parser_health ?? '—', t.parser_health ?? '—'],
    ['Required coverage', pct(b.coverage.required?.resume), pct(t.coverage.required?.resume)],
    ['Preferred coverage', pct(b.coverage.preferred?.resume), pct(t.coverage.preferred?.resume)],
  ]
  return (
    <Section title="Before and after" help="The same Resume Applicability audit, run on both résumés.">
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(160px, 1fr) 110px 110px', gap: '6px 12px', fontSize: 'var(--t-13)', maxWidth: 460 }}>
        <span /><Label>Base</Label><Label>Tailored</Label>
        {rows.map(([k, x, y]) => <React.Fragment key={k}><span>{k}</span><span>{x}</span><strong>{y}</strong></React.Fragment>)}
      </div>
      <span style={{ fontSize: 'var(--t-14)' }}>Improvement: <strong>{c.delta >= 0 ? '+' : ''}{c.delta} points</strong> · maximum supported {c.maximum}%</span>
      {c.changes.length === 0 && <Helper>No requirement changed status.</Helper>}
      {c.changes.map((ch) => (
        <div key={ch.requirement_id} style={{ display: 'flex', flexDirection: 'column', gap: 2, borderTop: '1px solid var(--line-soft)', paddingTop: 6 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap', fontSize: 'var(--t-13)' }}>
            <span style={{ flex: '1 1 240px', minWidth: 0 }}>{ch.requirement}</span>
            <Tag tone={RESUME_TONE[ch.from]}>{RESUME_LABEL[ch.from]}</Tag><span aria-hidden="true">→</span><Tag tone={RESUME_TONE[ch.to]}>{RESUME_LABEL[ch.to]}</Tag>
            <Helper size="xs">{ch.points >= 0 ? '+' : ''}{ch.points} pts</Helper>
          </div>
          {ch.caused_by.length > 0 && <><Helper size="xs">Caused by these verified facts now on the page:</Helper><EvidenceList refs={ch.caused_by} headlines={headlines} /></>}
          {ch.lost?.length > 0 && <><Helper size="xs">No longer counted — the bullet showing these is removed, failed the claim audit or awaits your review:</Helper><EvidenceList refs={ch.lost} headlines={headlines} /></>}
        </div>
      ))}
      {t.excluded_bullets?.length > 0 && (
        <Notice tone="warn"><Helper>{t.excluded_bullets.length} tailored bullet{t.excluded_bullets.length === 1 ? '' : 's'} failed the claim audit or still need review, and count for nothing until resolved below.</Helper></Notice>
      )}
    </Section>
  )
}

// ── Overview tab ─────────────────────────────────────────────────────────────

function ReqLine({ r, ok }) {
  const mark = ok ? ['✓', 'var(--good)'] : r.status === 'UNKNOWN' ? ['?', 'var(--warn)'] : ['✗', 'var(--bad)']
  const note = ok
    ? (r.evidence.length ? `Evidence: ${r.evidence.slice(0, 2).map((e) => e.headline).join('; ')}` : '')
    : (r.explanation || (r.status === 'UNKNOWN' ? 'Your profile does not say enough to tell.' : 'Nothing in your profile supports this.'))
  return (
    <div style={{ display: 'flex', gap: 10, padding: '8px 0', borderTop: '1px solid var(--line-soft)' }}>
      <span aria-hidden="true" style={{ flex: '0 0 14px', color: mark[1], fontWeight: 'var(--weight-semibold)' }}>{mark[0]}</span>
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
        <span style={{ fontSize: 'var(--t-13)' }}>{r.text}{!r.required && <Helper size="xs"> · preferred</Helper>}</span>
        {r.source_quote && <Helper>Posting: “{r.source_quote}”</Helper>}
        {note && <Helper>{note}</Helper>}
      </div>
    </div>
  )
}

function Overview({ ws, job, busy, onAnalyze, onRematch, goEvidence, profile }) {
  if (!ws.analysis) {
    return (
      <Notice tone={ws.needs_job_details ? 'warn' : 'quiet'} glyph="○" action={job?.description || ws.job.url ? <Button size="sm" busy={busy} onClick={onAnalyze}>{ws.needs_job_details ? 'Get job details' : 'Analyze job'}</Button> : null}>
        <strong style={{ fontSize: 'var(--t-13)' }}>{ws.needs_job_details ? 'Needs job details' : 'Not analyzed yet'}</strong>
        <Helper>
          {ws.needs_job_details ? 'Could not verify a complete job description yet. Retrieve the employer posting before analyzing requirements.' : ws.job.has_description || ws.job.url ? 'Analyze this posting to extract its requirements and check each one against your verified profile.' : 'This job has no description or URL to analyze.'}
          {profile && profile.facts.filter((f) => f.verified).length === 0 && <> Your <RouterLink to="/profile">profile</RouterLink> has no verified facts yet, so nothing can match.</>}
        </Helper>
      </Notice>
    )
  }
  const m = ws.match
  const reqs = ws.requirements
  const strong = reqs.filter((r) => r.status === 'MATCHED').sort(byWeight).slice(0, 6)
  const missing = reqs.filter((r) => r.status === 'MISSING' || r.status === 'UNKNOWN').sort(byWeight).slice(0, 6)
  const c = m?.counts || {}
  return (
    <>
      <Section title="Candidate Fit" help="How much of this job everything verified in your Profile satisfies — independent of what fits on one résumé. Not an ATS score."
        right={<Button size="xs" variant="ghost" onClick={goEvidence}>Full breakdown →</Button>}>
        {!m ? (
          <Helper>The requirements are extracted but not matched yet. <Button size="xs" variant="secondary" busy={busy} onClick={onRematch} style={{ display: 'inline-flex', marginLeft: 6 }}>Match now</Button></Helper>
        ) : (
          <>
            <span style={{ fontSize: 'var(--t-14)', lineHeight: '22px' }}>
              Your verified profile evidences <strong>{c.MATCHED || 0}</strong> of {reqs.length} requirements
              {c.PARTIAL ? <>, <strong>{c.PARTIAL}</strong> partially</> : null}.
              {' '}{(c.MISSING || 0) + (c.UNKNOWN || 0) === 1 ? '1 has' : `${(c.MISSING || 0) + (c.UNKNOWN || 0)} have`} no evidence.
              {m.coverage && <> Required coverage <strong>{pct(m.coverage.required)}</strong>, preferred <strong>{pct(m.coverage.preferred)}</strong>.</>}
            </span>
            {m.unavailable && <Notice tone="bad" action={<Button size="sm" variant="secondary" busy={busy} onClick={onRematch}>Re-match</Button>}><Helper>Match unavailable: {m.unavailable}. No score is shown rather than a misleading one.</Helper></Notice>}
            {ws.stale && <Notice tone="warn" action={<Button size="sm" variant="secondary" busy={busy} onClick={ws.jd_stale ? onAnalyze : onRematch}>{ws.jd_stale ? 'Re-analyze' : 'Re-match'}</Button>}><Helper>{ws.jd_stale ? 'The job description changed since this analysis.' : ws.outdated ? 'This match used the retired Role Match scorer; re-match to compute Candidate Fit.' : 'Your profile changed since this match was computed.'}</Helper></Notice>}
            {m.hard_blockers?.length > 0 && (
              <Notice tone="bad">
                <strong style={{ fontSize: 'var(--t-13)' }}>Hard requirements you do not meet</strong>
                {m.hard_blockers.map((b) => <Helper key={b}>{b}</Helper>)}
              </Notice>
            )}
          </>
        )}
      </Section>
      {m && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 12 }}>
          <Section title="Strongest matches" help={strong.length ? null : 'No requirement is fully evidenced yet.'}>
            <div>{strong.map((r) => <ReqLine key={r.id} r={r} ok />)}</div>
          </Section>
          <Section title="Important gaps" help={missing.length ? null : 'Every requirement has at least partial evidence.'}>
            <div>{missing.map((r) => <ReqLine key={r.id} r={r} />)}</div>
          </Section>
        </div>
      )}
      <Section title="Job description" right={ws.job.url ? <Button size="xs" variant="secondary" href={ws.job.url} target="_blank">Posting ↗</Button> : null}>
        {job?.description && !ws.job.needs_job_details
          ? <div style={{ fontSize: 'var(--t-13)', lineHeight: '21px', whiteSpace: 'pre-wrap', maxWidth: '80ch', color: 'var(--text-2)' }}>{job.description}</div>
          : <Helper>{job ? ws.job.needs_job_details ? 'Needs job details. Generic careers-page text was rejected.' : 'No description stored for this job.' : 'Loading…'}</Helper>}
      </Section>
    </>
  )
}

// ── Resume tab ───────────────────────────────────────────────────────────────

function Diff({ from, to }) {
  return (
    <span>
      {diffWords(from || '', to || '').map((p, i) => (
        <span key={i} style={p.added ? { background: 'var(--change-soft)', borderRadius: 'var(--radius-mark)' } : p.removed ? { background: 'var(--bad-soft)', textDecoration: 'line-through', opacity: 0.75, borderRadius: 'var(--radius-mark)' } : undefined}>{p.value}</span>
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
  const unsupported = claim?.status === 'UNSUPPORTED'
  return (
    <div id={`bullet-${b.id}`} style={{
      display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, padding: '8px 8px', borderTop: '1px solid var(--line-soft)',
      ...(unsupported ? { background: 'var(--bad-faint)', borderLeft: '3px solid var(--bad)',borderRadius: 'var(--radius-mark)' } : null),
    }}>
      <div style={{ fontSize: 'var(--t-13)', color: 'var(--muted)' }}>{b.baseText ?? <Helper size="xs">— not on the base résumé —</Helper>}</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        <div style={{ fontSize: 'var(--t-13)' }}>{kind === 'changed' ? <Diff from={b.baseText} to={b.text} /> : kind === 'added' ? <span style={{ background: 'var(--change-soft)' }}>{b.text}</span> : b.text}</div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          <Tag tone={CLAIM_TONE[claim?.status] || 'neutral'}>{claim?.status || '—'}</Tag>
          <Helper size="xs">{kind}{b.edited ? ' · edited by you' : ''}{claim?.reviewed ? ' · reviewed' : ''}</Helper>
          <span style={{ flex: 1 }} />
          <Pill size="sm" onClick={() => setInspect((v) => !v)}>{inspect ? 'Hide sources' : 'Sources'}</Pill>
          {draft && changed && <>
            <Pill size="sm" disabled={busy || unsupported || claim?.reviewed} onClick={() => onAction(b.id, 'accept')}>Accept</Pill>
            <Pill size="sm" disabled={busy} onClick={() => onAction(b.id, 'reject')}>Reject</Pill>
          </>}
          {draft && version.kind === 'tailored' && <Pill size="sm" disabled={busy} onClick={() => onAction(b.id, 'regenerate')}>Regenerate</Pill>}
          {draft && <Pill size="sm" disabled={busy} onClick={() => { setText(b.text); setEditing((v) => !v) }}>Edit</Pill>}
        </div>
        {claim?.reasons?.length > 0 && <Helper size="xs" style={{ color: unsupported ? 'var(--bad)' : undefined }}>{claim.reasons.join(' · ')}</Helper>}
        {editing && (
          <div style={{ display: 'flex', gap: 6 }}>
            <Textarea rows={2} value={text} onChange={setText} style={{ flex: 1 }} />
            <Button size="sm" busy={busy} onClick={async () => { await onAction(b.id, 'edit', text); setEditing(false) }}>Save</Button>
          </div>
        )}
        {inspect && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, padding: 10, background: 'var(--surface-2)', borderRadius: 'var(--radius-row)', fontSize: 'var(--t-12)' }}>
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

// One résumé version: status + actions, unsupported claims up front, then the
// base-vs-tailored review. Exported for the Resume screen's version page.
export function ResumeReview({ versionId, factHeadlines, onChanged, pushToast, onRegenerate, regenerating }) {
  const [v, setV] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => api.get(`/resume-versions/${versionId}`).then(({ data }) => setV(data))
    .catch((e) => pushToast({ kind: 'error', msg: errMsg(e, 'Could not load this résumé') })), [versionId, pushToast])
  useEffect(() => { load() }, [load])
  const requirements = useMemo(() => Object.fromEntries(((v?.job_analysis?.analysis?.requirements) || []).map((r) => [r.id, r.text])), [v])
  const claims = useMemo(() => Object.fromEntries(((v?.audit?.claims) || []).map((c) => [c.bullet_id, c])), [v])
  const bulletText = useMemo(() => {
    const out = {}
    ;(v?.resume?.sections || []).forEach((s) => (s.entries || []).forEach((e) => (e.bullets || []).forEach((b) => { out[b.id] = b.text })))
    return out
  }, [v])
  if (!v) return <Helper><Spinner /> Loading résumé…</Helper>
  const run = async (fn, ok) => {
    setBusy(true)
    try { await fn(); if (ok) pushToast({ kind: 'success', msg: ok }); await load(); onChanged?.() } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Request failed') }) } finally { setBusy(false) }
  }
  const onAction = (bid, action, text) => run(() => (action === 'regenerate'
    ? api.post(`/resume-versions/${v.id}/bullets/${encodeURIComponent(bid)}/regenerate`)
    : api.post(`/resume-versions/${v.id}/bullets/${encodeURIComponent(bid)}`, { action, text })))
  const paired = pairEntries(v.base?.resume, v.resume)
  const draft = v.status === 'draft'
  const counts = v.counts || {}
  const unsupported = (v.audit?.claims || []).filter((c) => c.status === 'UNSUPPORTED' && bulletText[c.bullet_id] != null)
  const statusTone = v.status === 'accepted' ? 'good' : v.status === 'rejected' ? 'neutral' : v.blocked ? 'bad' : 'accent'
  const statusText = v.status === 'draft' ? (v.blocked ? 'Draft · blocked' : 'Draft · needs review') : cap(v.status)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Card style={{ padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Tag tone={statusTone}>{statusText}</Tag>
          <span style={{ fontSize: 'var(--t-14)', fontWeight: 'var(--weight-semibold)' }}>{v.kind === 'base' ? 'Base résumé' : 'Tailored résumé'}</span>
          <Helper>created {ago(v.created_at)}{v.accepted_at ? ` · accepted ${ago(v.accepted_at)}` : ''}</Helper>
          <span style={{ flex: 1 }} />
          {onRegenerate && <Button size="sm" variant="secondary" busy={regenerating} onClick={onRegenerate}>Regenerate</Button>}
          {draft && <>
            <Button size="sm" variant="secondary" busy={busy} onClick={() => window.confirm('Discard this draft?') && run(() => api.post(`/resume-versions/${v.id}/reject`), 'Draft rejected')}>Reject draft</Button>
            <Button size="sm" busy={busy} disabled={v.blocked} title={v.blocked ? 'Unsupported claims must be rejected or edited first' : 'Accept every remaining change and freeze this version'}
              onClick={() => run(() => api.post(`/resume-versions/${v.id}/accept`, { all: true }), 'Accepted — autofill will upload this résumé')}>Accept</Button>
          </>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', fontSize: 'var(--t-13)' }}>
          <span><span style={{ color: 'var(--good)' }}>●</span> {counts.SUPPORTED ?? 0} supported</span>
          <span><span style={{ color: 'var(--warn)' }}>●</span> {counts.AMBIGUOUS ?? 0} ambiguous</span>
          <span style={unsupported.length ? { color: 'var(--bad)', fontWeight: 'var(--weight-semibold)' } : undefined}><span style={{ color: 'var(--bad)' }}>●</span> {counts.UNSUPPORTED ?? 0} unsupported</span>
          {v.parser_health != null && <Helper>Parser Health {v.parser_health}</Helper>}
          {v.pages ? <Helper>{v.pages} page{v.pages === 1 ? '' : 's'}</Helper> : null}
          <span style={{ flex: 1 }} />
          {v.pages ? <Button size="xs" variant="secondary" href={`/api/resume-versions/${v.id}/pdf`} target="_blank">PDF ↗</Button> : null}
          <Button size="xs" variant="secondary" href={`/api/resume-versions/${v.id}/tex`}>.tex</Button>
          <Button size="xs" variant="secondary" href={`/api/resume-versions/${v.id}/export.zip`} title="Overleaf-compatible project">Overleaf ZIP</Button>
          {v.status === 'accepted' && <Button size="xs" variant="secondary" busy={busy} title="Push this accepted version to your Overleaf Git remote (Settings › Copilot)"
            onClick={() => run(() => api.post(`/resume-versions/${v.id}/overleaf`), 'Pushed to Overleaf')}>Push to Overleaf</Button>}
          {draft && <Button size="xs" variant="secondary" busy={busy} onClick={() => run(() => api.post(`/resume-versions/${v.id}/rebuild`), 'PDF rebuilt')}>Rebuild PDF</Button>}
        </div>
        {v.parser_health_detail && (
          <Helper>Parser Health: {v.parser_health_detail.checks.map((c) => `${c.ok ? '✓' : '✗'} ${c.name}${c.detail ? ` (${c.detail})` : ''}`).join(' · ')}</Helper>
        )}
      </Card>

      {unsupported.length > 0 && (
        <Notice tone="bad">
          <strong style={{ fontSize: 'var(--t-14)' }}>{unsupported.length} unsupported claim{unsupported.length === 1 ? '' : 's'} — the PDF is blocked</strong>
          <Helper>Nothing in your profile supports these. Reject or edit each one; nothing is fixed silently.</Helper>
          {unsupported.map((c) => (
            <div key={c.bullet_id} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', paddingTop: 6 }}>
              <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
                <span style={{ fontSize: 'var(--t-13)' }}>“{bulletText[c.bullet_id]}”</span>
                {c.reasons?.length > 0 && <Helper size="xs">{c.reasons.join(' · ')}</Helper>}
              </div>
              <Button size="xs" variant="secondary" onClick={() => document.getElementById(`bullet-${c.bullet_id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>Review ↓</Button>
            </div>
          ))}
        </Notice>
      )}
      {!v.base && v.kind === 'base' && <Notice tone="quiet"><Helper>The first base résumé, so there is no earlier version to compare against.</Helper></Notice>}
      {!v.base && v.kind === 'tailored' && <Notice tone="quiet"><Helper>No accepted base résumé yet, so every bullet shows as added. Generate and accept one on the <RouterLink to="/resumes">Resume</RouterLink> screen for a real diff.</Helper></Notice>}
      {v.compile_log && !v.blocked && !v.pages && <Notice tone="bad"><strong style={{ fontSize: 'var(--t-13)' }}>LaTeX did not compile</strong><pre style={{ fontSize: 'var(--t-11)', fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', margin: 0 }}>{v.compile_log}</pre></Notice>}

      <Card style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, padding: '0 8px' }}><Label size="lg">{v.base ? 'Base résumé' : 'Before'}</Label><Label size="lg">{v.kind === 'base' ? 'This version' : 'Tailored'}</Label></div>
        {paired.map((s) => (
          <div key={s.id} style={{ display: 'flex', flexDirection: 'column', gap: 4, paddingTop: 8 }}>
            <strong style={{ fontSize: 'var(--t-14)', padding: '0 8px' }}>{s.title}</strong>
            {(s.lines || []).map((l) => <Helper key={l.label} style={{ padding: '0 8px' }}>{l.label}: {l.items.join(', ')}</Helper>)}
            {(s.entries || []).map((e) => (
              <div key={e.fact_id}>
                <div style={{ fontSize: 'var(--t-13)', fontWeight: 'var(--weight-semibold)', padding: '4px 8px' }}>{e.heading} <Helper size="xs" style={{ display: 'inline' }}>· {e.subheading} · {e.date}</Helper>{e.newEntry && <Tag tone="accent" style={{ marginLeft: 6 }}>added</Tag>}</div>
                {e.bullets.map((b) => <BulletReview key={b.id} b={b} claim={claims[b.id]} version={v} requirements={requirements} factHeadlines={factHeadlines} onAction={onAction} busy={busy} />)}
                {e.removed.map((b) => (
                  <div key={b.id} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, padding: '8px', borderTop: '1px solid var(--line-soft)' }}>
                    <span style={{ fontSize: 'var(--t-13)', textDecoration: 'line-through', background: 'var(--bad-soft)' }}>{b.text}</span>
                    <Helper size="xs">removed for this job</Helper>
                  </div>
                ))}
              </div>
            ))}
          </div>
        ))}
      </Card>
    </div>
  )
}

// Which role family this posting is. It decides which base resume a tailored
// draft derives from (SWE, Product/TPM, Data/ML, or one you configured) and
// nothing else — it is deliberately no part of Candidate Fit. Override it when
// the classifier reads an ambiguous title the wrong way.
function RoleFamilyPicker({ rf, jobId, onChanged, pushToast }) {
  const [saving, setSaving] = useState(false)
  if (!rf?.options?.length) return null
  const pick = async (id) => {
    if (id === rf.id) return
    setSaving(true)
    try { await api.put(`/copilot/jobs/${jobId}/role-family`, { role_family: id }); await onChanged() }
    catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not set the role family') }) }
    finally { setSaving(false) }
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <Label size="lg" title="Chooses the base résumé this job is tailored from. Not part of Candidate Fit.">Role</Label>
      {rf.options.map((o) => (
        <Pill key={o.id} size="sm" on={o.id === rf.id} disabled={saving} onClick={() => pick(o.id)}>{o.label}</Pill>
      ))}
      <Helper size="xs">{rf.source === 'user' ? 'You chose this.' : rf.reason}</Helper>
    </div>
  )
}

function ResumeTab({ ws, versions, selected, setSelected, reviewKey, busy, onAnalyze, onRematch, onGenerate, onAudit, auditing, onCreateBase, creatingBase,
  factHeadlines, headlines, entries, onContextSaved, onChanged, pushToast }) {
  const [which, setWhich] = useState('base')
  if (!ws.analysis) {
    return (
      <Notice tone="quiet" glyph="○" action={<Button size="sm" busy={busy} onClick={onAnalyze}>Analyze job</Button>}>
        <strong style={{ fontSize: 'var(--t-13)' }}>Analyze the job first</strong>
        <Helper>A tailored résumé is drafted from the requirements the analysis extracts, using only your verified profile facts.</Helper>
      </Notice>
    )
  }
  if (!ws.match || ws.outdated) {
    return (
      <Notice tone="quiet" action={<Button size="sm" busy={busy} onClick={onRematch}>Re-match</Button>}>
        <Helper>{ws.outdated ? 'This job was scored by the retired Role Match formula. Re-match to compute Candidate Fit and audit your résumé.' : 'Waiting for Candidate Fit before a résumé can be audited or drafted.'}</Helper>
      </Notice>
    )
  }
  const ra = ws.resume_audit
  const shown = which === 'tailored' && ra?.tailored ? ra.tailored : ra?.base
  return (
    <>
      <RoleFamilyPicker rf={ws.role_family} jobId={ws.job.id} onChanged={onChanged} pushToast={pushToast} />
      {ra && <ApplicabilitySummary ra={ra} busy={busy} auditing={auditing} onAudit={onAudit} onGenerate={onGenerate} onCreateBase={onCreateBase} creatingBase={creatingBase} />}
      {ra && <BeforeAfter ra={ra} headlines={headlines} />}
      {ra?.base && ra?.tailored && (
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <Label size="lg">Gap audit of</Label>
          <Pill size="sm" on={which === 'base'} onClick={() => setWhich('base')}>Base résumé</Pill>
          <Pill size="sm" on={which === 'tailored'} onClick={() => setWhich('tailored')}>Tailored résumé</Pill>
        </div>
      )}
      <GapAudit audit={shown} title={`Gap audit · ${shown === ra?.tailored ? 'tailored' : 'base'} résumé`} headlines={headlines} jobId={ws.job.id}
        entries={entries} onSaved={onContextSaved} pushToast={pushToast} />
      {versions.length > 1 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Label size="lg">Version</Label>
          <Select value={selected || ''} width="340px" ariaLabel="Résumé version"
            options={versions.map((x) => [x.id, `${cap(x.status)}${x.blocked ? ' · blocked' : ''} · ${new Date(x.created_at).toLocaleString()}`])} onPick={setSelected} />
        </div>
      )}
      {selected
        ? <ResumeReview key={`${selected}:${reviewKey}`} versionId={selected} factHeadlines={factHeadlines} onChanged={onChanged} pushToast={pushToast} onRegenerate={onGenerate} regenerating={busy} />
        : versions.length > 0 && <Helper>Every version for this job was rejected. <Button size="xs" busy={busy} onClick={onGenerate} style={{ display: 'inline-flex', marginLeft: 6 }}>Generate a new one</Button></Helper>}
    </>
  )
}

// ── Application tab ──────────────────────────────────────────────────────────

function ApplicationTab({ ws, job, versions, acceptedBase, onChanged, pushToast, goResume }) {
  const [busy, setBusy] = useState(false)
  const app = ws.application
  const tailored = versions.find((x) => x.status === 'accepted')
  const upload = tailored || acceptedBase
  const markApplied = async () => {
    setBusy(true)
    try {
      await api.post('/applications', { title: ws.job.title || '', company: ws.job.company || '', url: ws.job.url || '', location: ws.job.location || undefined })
      pushToast({ kind: 'success', msg: 'Marked as applied' }); onChanged()
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not record the application') }) } finally { setBusy(false) }
  }
  return (
    <>
      <Section title="Application status">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          {app
            ? <><Tag tone="good">{cap(app.status.replace(/_/g, ' '))}</Tag><RouterLink to="/applications" style={{ fontSize: 'var(--t-13)' }}>Open in Applications →</RouterLink></>
            : <><Helper>Not applied yet.</Helper><span style={{ flex: 1 }} /><Button size="sm" variant="secondary" busy={busy} onClick={markApplied}>Mark as applied</Button></>}
          <span style={{ flex: 1 }} />
          <Button size="sm" href={ws.job.url || undefined} target="_blank" disabled={!ws.job.url}>Apply with autofill ↗</Button>
        </div>
        <Helper>Apply opens the posting. The JobNavigator browser extension fills the form there; you review and submit it yourself.</Helper>
      </Section>
      <Section title="What autofill will use">
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 300px', display: 'flex', flexDirection: 'column', gap: 3 }}>
            <Label size="lg">Résumé</Label>
            {upload ? (
              <span style={{ fontSize: 'var(--t-13)' }}>
                {tailored ? 'Tailored résumé for this job' : 'Base résumé (no accepted tailored version for this job)'}
                <Helper> · accepted {ago(upload.accepted_at)}</Helper>
              </span>
            ) : <span style={{ fontSize: 'var(--t-13)', color: 'var(--warn)' }}>No accepted résumé. Autofill has nothing to upload.</span>}
          </div>
          {upload?.pages ? <Button size="xs" variant="secondary" href={`/api/resume-versions/${upload.id}/pdf`} target="_blank">PDF ↗</Button> : null}
          {!tailored && <Button size="xs" variant="secondary" onClick={goResume}>Tailor for this job</Button>}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, paddingTop: 8, borderTop: '1px solid var(--line-soft)' }}>
          <Label size="lg">Answers</Label>
          <Helper>
            Work authorization, sponsorship, salary and EEO answers come only from your <RouterLink to="/profile">Profile</RouterLink>; unanswered questions are left empty.
            Free-text answers draw on your <RouterLink to="/answer-bank">Answer bank</RouterLink>.
          </Helper>
        </div>
        {job?.discovered_at && <Helper>Found {ago(job.discovered_at)} · source {job.source || 'unknown'}</Helper>}
      </Section>
    </>
  )
}

// ── screen ───────────────────────────────────────────────────────────────────

export default function JobDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const tab = TABS.some(([k]) => k === params.get('tab')) ? params.get('tab') : 'overview'
  const setTab = (k) => setParams(k === 'overview' ? {} : { tab: k }, { replace: true })
  const [ws, setWs] = useState(null)
  const [job, setJob] = useState(null)
  const [versions, setVersions] = useState([])
  const [acceptedBase, setAcceptedBase] = useState(null)
  const [selected, setSelected] = useState(null)
  const [profile, setProfile] = useState(null)
  const [privacy, setPrivacy] = useState(null)
  const [auditing, setAuditing] = useState(false)
  const [creatingBase, setCreatingBase] = useState(false)
  const [pending, setPending] = useState([])   // [{run_id, type, label}]
  const [reviewKey, setReviewKey] = useState(0)
  const [loadErr, setLoadErr] = useState(false)
  const { toasts, push: pushToast, dismiss } = useToasts()
  useTitle(ws?.job ? `${ws.job.title} — ${ws.job.company}` : 'Job')

  const load = useCallback(async () => {
    try {
      const [w, vs] = await Promise.all([api.get(`/copilot/jobs/${id}`), api.get('/resume-versions', { params: { job_id: id } })])
      setWs(w.data); setVersions(vs.data); setLoadErr(false)
      setSelected((cur) => (cur && vs.data.some((x) => x.id === cur) ? cur : (vs.data.find((x) => x.status !== 'rejected') || {}).id || null))
    } catch (e) { setLoadErr(true); pushToast({ kind: 'error', msg: errMsg(e, 'Could not load this job') }) }
  }, [id, pushToast])
  const loadJob = useCallback(() => api.get(`/jobs/${id}`).then(({ data }) => setJob(data)).catch(() => { /* the header falls back to the workspace fields */ }), [id])
  useEffect(() => { load(); loadJob() }, [load, loadJob])
  useEffect(() => {
    api.get('/profile').then(({ data }) => setProfile(data)).catch(() => {})
    api.get('/copilot/privacy').then(({ data }) => setPrivacy(data)).catch(() => {})
    api.get('/resume-versions', { params: { status: 'accepted' } }).then(({ data }) => setAcceptedBase((data || []).find((x) => x.kind === 'base') || null)).catch(() => {})
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

  if (!ws) {
    return (
      <div style={{ padding: 30 }}>
        {loadErr ? <Notice tone="bad" action={<Button size="sm" variant="secondary" onClick={load}>Retry</Button>}><Helper>Couldn’t load this job.</Helper></Notice> : <Helper><Spinner /> Loading…</Helper>}
        <ToastStack toasts={toasts} onClose={dismiss} />
      </div>
    )
  }
  const busy = pending.length > 0 || ws.running.length > 0
  const analyze = () => start(`/copilot/jobs/${id}/analyze`, 'copilot_analyze', 'Analysis')
  const rematch = () => start(`/copilot/jobs/${id}/match`, 'copilot_match', 'Re-match')
  const generate = () => start(`/resume-versions/for-job/${id}`, 'copilot_resume', 'Tailored résumé')
  const auditNow = async () => {
    setAuditing(true)
    try { await api.post(`/copilot/jobs/${id}/resume-audit`, {}); pushToast({ kind: 'success', msg: 'Résumé audited and stored' }); await load() }
    catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Audit failed') }) } finally { setAuditing(false) }
  }
  const createBase = async () => {
    setCreatingBase(true)
    try { await api.post('/resume-versions/base'); pushToast({ kind: 'success', msg: 'Base résumé drafted from your Profile — accept it on the Resume screen when it looks right' }); await load() }
    catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not create a base résumé') }) } finally { setCreatingBase(false) }
  }
  const reloadProfile = () => { load(); api.get('/profile').then(({ data }) => setProfile(data)).catch(() => {}) }
  const headlines = { ...factHeadlines, ...(ws.headlines || {}) }
  const unavailable = ws.match?.unavailable
  // a stale or unavailable match is not shown as a number
  const score = ws.stale || unavailable ? null : (ws.match?.score ?? null)
  const tone = scoreTone(score)
  const a = ws.analysis || {}
  const saved = job ? job.status === 'saved' || !!job.saved : false
  const toggleSave = async () => {
    const on = !saved
    const changes = job.status === 'applied' ? { saved: on } : { saved: on, status: on ? 'saved' : 'new' }
    setJob((j) => ({ ...j, ...changes }))
    try { await api.patch(`/jobs/${id}`, changes, { params: { legacy_score: false } }) } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not update this job') }); loadJob() }
  }
  const meta = [
    ws.job.location,
    job && ARRANGEMENTS.filter(([k]) => job[`arr_${k}`]).map(([, l]) => l).join(' / '),
    cap(a.employment_type), cap(a.experience_level),
    job && [fmtSalary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period, job.salary_source),
      job.salary_source === 'lca_estimate' ? 'H-1B filing estimate' : ['posting_ats', 'posting_description'].includes(job.salary_source) ? 'posting' : null].filter(Boolean).join(' · '),
    job?.discovered_at && `found ${ago(job.discovered_at)}`,
  ].filter(Boolean)

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <header style={{ flex: '0 0 auto', background: 'var(--surface)', borderBottom: '1px solid var(--line)', padding: '16px 24px 0' }}>
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <Logo company={ws.job.company} />
          <div style={{ flex: '1 1 360px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <RouterLink to="/feed" style={{ fontSize: 'var(--t-12)', color: 'var(--muted)' }}>‹ Jobs</RouterLink>
              <span style={{ fontSize: 'var(--t-13)', color: 'var(--text-2)', fontWeight: 'var(--weight-medium)' }}>{ws.job.company || 'Unknown company'}</span>
              {ws.application && <Tag tone="good">{cap(ws.application.status.replace(/_/g, ' '))}</Tag>}
              {copilotPrivacy && <Tag tone={copilotPrivacy.external ? 'warn' : 'neutral'} title={copilotPrivacy.external ? `Job text and your verified profile facts are sent to ${copilotPrivacy.provider}` : `Runs on ${copilotPrivacy.destination}`}>
                {copilotPrivacy.external ? `AI: ${copilotPrivacy.provider}` : `Local AI · ${copilotPrivacy.model || 'no model set'}`}</Tag>}
            </div>
            <h1 style={{ margin: 0, fontSize: 'var(--t-22)', lineHeight: '28px', fontWeight: 'var(--weight-semibold)', letterSpacing: '-.01em' }}>{ws.job.title || 'Untitled role'}</h1>
            <Helper style={{ fontSize: 'var(--t-13)' }}>{meta.join(' · ')}</Helper>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '4px 14px 4px 4px', border: '1px solid var(--line)', borderRadius: 'var(--radius-card)', background: 'var(--recessed)' }}>
            {busy && ws.running.length ? <ScoreRing busy size={56} /> : <ScoreRing value={score} label={ws.stale && ws.match ? 'stale' : '—'} size={56} ariaLabel={score != null ? `Candidate Fit ${score} of 100` : 'No current Candidate Fit'} />}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
              <Label title="Based on everything verified in your Profile. Not an ATS score.">Candidate Fit</Label>
              <span style={{ fontSize: 'var(--t-14)', fontWeight: 'var(--weight-semibold)', color: score != null ? `var(--${tone})` : 'var(--muted)' }}>
                {score != null ? MATCH_LABEL[tone] : ws.needs_job_details ? 'Needs job details' : unavailable ? 'Match unavailable'
                  : ws.stale && ws.match ? (ws.outdated ? 'Old scoring — re-match' : ws.jd_stale ? 'Job changed — re-analyze' : 'Profile changed — re-match')
                    : ws.analysis ? 'Not matched' : 'Not analyzed'}
              </span>
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
          <div role="tablist" aria-label="Job workspace" style={{ display: 'flex', gap: 22, height: 40 }}>
            {TABS.map(([k, label]) => (
              <button key={k} type="button" role="tab" id={`tab-${k}`} aria-controls="job-tabpanel" aria-selected={tab === k} className="v2-tabbtn" onClick={() => setTab(k)}>{label}</button>
            ))}
          </div>
          <span style={{ flex: 1 }} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingBottom: 8, flexWrap: 'wrap' }}>
            {job && <IconButton size={36} on={saved} title={saved ? 'Saved — click to unsave' : 'Save'} onClick={toggleSave}><Heart size={15} fill={saved ? 'currentColor' : 'none'} aria-hidden="true" /></IconButton>}
            <Button size="sm" variant="secondary" busy={busy} onClick={analyze}>{ws.analysis ? 'Re-analyze' : 'Analyze'}</Button>
            {ws.analysis && <Button size="sm" variant={ws.stale ? 'primary' : 'secondary'} busy={busy} onClick={rematch}>Re-match</Button>}
            <Button size="sm" variant="secondary" onClick={() => setTab('resume')}>Tailor resume</Button>
            <Button size="sm" href={ws.job.url || undefined} target="_blank" disabled={!ws.job.url}
              title="Opens the posting; the extension fills the form from your accepted résumé and answers">Apply with autofill</Button>
          </div>
        </div>
      </header>

      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto' }}>
        <div id="job-tabpanel" role="tabpanel" aria-labelledby={`tab-${tab}`} style={{ maxWidth: 1080, margin: '0 auto', padding: '16px 24px 40px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {tab === 'overview' && <Overview ws={ws} job={job} busy={busy} profile={profile} onAnalyze={analyze} onRematch={rematch} goEvidence={() => setTab('evidence')} />}
          {tab === 'resume' && (
            <ResumeTab ws={ws} versions={versions} selected={selected} setSelected={setSelected} reviewKey={reviewKey} busy={busy}
              onAnalyze={analyze} onRematch={rematch} onGenerate={generate} onAudit={auditNow} auditing={auditing} onCreateBase={createBase} creatingBase={creatingBase}
              factHeadlines={factHeadlines} headlines={headlines} entries={contextEntries} onContextSaved={reloadProfile} onChanged={load} pushToast={pushToast} />
          )}
          {tab === 'application' && (
            <ApplicationTab ws={ws} job={job} versions={versions} acceptedBase={acceptedBase} pushToast={pushToast}
              onChanged={() => { load(); loadJob() }} goResume={() => setTab('resume')} />
          )}
          {tab === 'evidence' && (
            ws.analysis ? (
              <>
                <ScoringMethod match={ws.match} audit={ws.resume_audit?.base} />
                <RequirementMatrix requirements={ws.requirements} audit={ws.resume_audit?.base} headlines={headlines} />
                <Provenance ws={ws} privacy={copilotPrivacy} />
              </>
            ) : <Notice tone="quiet"><Helper>No evidence yet — analyze the job first.</Helper></Notice>
          )}
        </div>
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
