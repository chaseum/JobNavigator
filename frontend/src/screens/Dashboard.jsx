import React, { useState, useEffect } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import api from '../api'
import '../theme.css'
import { useTitle } from '../useTitle'
import { Card, HeaderRow, Helper, Label, Notice, PageTitle, Tag } from '../ui'

// Where the workflow stands: what to review, what to generate, what is ready to submit.
function Stat({ label, value, to }) {
  const body = (
    <Card style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 2, minWidth: 150 }}>
      <span style={{ fontSize: 22, fontWeight: 600 }}>{value ?? '—'}</span>
      <Helper>{label}</Helper>
    </Card>
  )
  return to ? <RouterLink to={to} style={{ textDecoration: 'none', color: 'inherit' }}>{body}</RouterLink> : body
}

function List({ title, help, rows, render, empty }) {
  return (
    <Card style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 6, flex: '1 1 300px', minWidth: 280 }}>
      <Label>{title}</Label>
      {help && <Helper size="xs">{help}</Helper>}
      {rows.length ? rows.map(render) : <Helper>{empty}</Helper>}
    </Card>
  )
}

const jobLink = (r, extra) => (
  <div key={`${r.job_id}:${r.version_id || r.application_id || ''}`} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
    <RouterLink to={`/jobs/${r.job_id}`} style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
      {r.title || 'Role'} — {r.company || '?'}
    </RouterLink>
    {extra}
  </div>
)

export default function Dashboard() {
  useTitle('Dashboard')
  const [d, setD] = useState(null)
  const [err, setErr] = useState(false)
  useEffect(() => { api.get('/copilot/dashboard').then(({ data }) => setD(data)).catch(() => setErr(true)) }, [])
  const apps = d?.counts.applications || {}
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 12 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <PageTitle>Dashboard</PageTitle>
          <Helper>{d ? `Profile version ${d.profile.version} · AI: ${d.llm.provider}${d.llm.model ? ` / ${d.llm.model}` : ''}` : ' '}</Helper>
        </div>
        <span style={{ flex: 1 }} />
        {d && <Tag tone={d.llm.external ? 'warn' : 'good'} title={d.llm.external ? 'Job text and verified profile facts go to this provider' : 'Everything stays on this machine'}>{d.llm.external ? 'external AI provider' : 'local AI'}</Tag>}
      </HeaderRow>
      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto', padding: '16px 26px 30px', display: 'flex', flexDirection: 'column', gap: 12 }}>
        {err && <Notice tone="bad"><Helper>Couldn’t load the dashboard. Is the backend running?</Helper></Notice>}
        {d && d.profile.verified === 0 && (
          <Notice tone="warn"><Helper>Your profile has no verified facts, so nothing can be matched or put on a résumé. Start in <RouterLink to="/profile">Profile</RouterLink>.</Helper></Notice>
        )}
        {d && d.profile.unverified > 0 && (
          <Notice tone="quiet"><Helper>{d.profile.unverified} imported fact{d.profile.unverified === 1 ? ' awaits' : 's await'} your review in <RouterLink to="/profile">Profile</RouterLink>.</Helper></Notice>
        )}
        {d && d.stale_matches > 0 && <Notice tone="quiet"><Helper>{d.stale_matches} Candidate Fit result{d.stale_matches === 1 ? ' is' : 's are'} older than your profile — re-match from the job’s workspace.</Helper></Notice>}
        {d && (
          <>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <Stat label="Saved jobs" value={d.counts.saved_jobs} to="/feed" />
              <Stat label="Analyzed" value={d.counts.analyzed} />
              <Stat label="Drafts to review" value={d.counts.drafts_to_review} />
              <Stat label="Accepted résumés" value={d.counts.accepted_resumes} to="/resumes" />
              <Stat label="Ready to apply" value={apps.ready_to_apply || 0} to="/applications" />
              <Stat label="Applied / in process" value={['applied', 'oa', 'recruiter_screen', 'interview', 'final'].reduce((n, s) => n + (apps[s] || 0), 0)} to="/applications" />
              <Stat label="Offers" value={apps.offer || 0} to="/applications" />
            </div>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-start' }}>
              <List title="Review drafts" help="Every changed claim is checked against your facts before you can accept." rows={d.review} empty="Nothing waiting."
                render={(r) => jobLink(r, r.blocked ? <Tag tone="bad">blocked</Tag> : <Helper size="xs">{r.counts?.AMBIGUOUS || 0} to review</Helper>)} />
              <List title="Generate a résumé" help="Analyzed, no résumé yet — best matches first." rows={d.generate} empty="No analyzed jobs without a résumé."
                render={(r) => jobLink(r, <Tag tone={r.score >= 70 ? 'good' : r.score >= 50 ? 'warn' : 'bad'}>{r.score}</Tag>)} />
              <List title="Ready to submit" help="The extension filled these. Review and submit on the company’s site." rows={d.apply} empty="None."
                render={(r) => jobLink(r)} />
            </div>
            <List title="Top Candidate Fit" help="How much of each posting your verified profile satisfies — not an employer ATS score." rows={d.top_matches} empty="Analyze a job to see its match."
              render={(r) => jobLink(r, <>{r.stale && <Tag tone="warn">stale</Tag>}<Tag tone={r.score >= 70 ? 'good' : r.score >= 50 ? 'warn' : 'bad'}>{r.score}</Tag></>)} />
          </>
        )}
      </div>
    </div>
  )
}
