import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import api from '../api'
import { ago } from '../time'
import { useToasts, ToastStack } from '../Toast'
import {
  Button, Card, Dot, HeaderRow, Heading, Helper, Label, Mono, Notice, PageTitle, Spinner, Surface, Tag,
} from '../ui'
import '../theme.css'

// Discovery diagnostics. Nothing here is needed to use JobNavigator — it is the
// window into the machinery the product deliberately hides: which internal
// queries the planner built from your preferences, which sources answered, what
// the Preference Gate threw away and why, and what is waiting on Candidate Fit.

const errMsg = (e, fb) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fb)

function Stat({ label, value, tone }) {
  return (
    <Surface radius="row" pad="10px 12px" style={{ flex: '1 1 130px', minWidth: 120, display: 'flex', flexDirection: 'column', gap: 3 }}>
      <Helper size="xs">{label}</Helper>
      <span style={{ fontSize: 'var(--t-19)', fontWeight: 'var(--weight-semibold)', color: tone ? `var(--${tone})` : 'var(--text)' }}>{value}</span>
    </Surface>
  )
}

function Section({ title, sub, children }) {
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '14px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <Heading size="sm">{title}</Heading>
        {sub && <Helper size="xs">{sub}</Helper>}
      </div>
      {children}
    </Card>
  )
}

export default function Advanced() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const { toasts, push: pushToast, dismiss } = useToasts()

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get('/discovery/status')
      setData(d); setErr(null)
    } catch (e) {
      setErr(errMsg(e, 'Could not read discovery status'))
    }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) load() }, 15000)
    return () => clearInterval(t)
  }, [load])

  const act = async (label, fn) => {
    if (busy) return
    setBusy(true)
    try {
      await fn()
      pushToast({ kind: 'progress', msg: `${label} started` })
      setTimeout(load, 2000)
    } catch (e) {
      const conflict = e?.response?.status === 409
      pushToast({ kind: conflict ? 'progress' : 'error', msg: conflict ? `${label} is already running` : errMsg(e, `${label} failed`) })
    } finally { setBusy(false) }
  }

  if (err) {
    return (
      <div style={{ padding: 24 }}>
        <Notice tone="bad" action={<Button size="sm" variant="secondary" onClick={load}>Retry</Button>}><Helper>{err}</Helper></Notice>
      </div>
    )
  }
  if (!data) return <div style={{ padding: 24 }}><Spinner /></div>

  const c = data.counts || {}
  const boards = Object.entries(data.boards || {})

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 18 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
          <PageTitle>Discovery diagnostics</PageTitle>
          <Helper>
            Internals. Ordinary use needs none of this — discovery, company monitoring and Candidate Fit all run on their own.
          </Helper>
        </div>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => act('Discovery', () => api.post('/discovery/refresh'))}>Run discovery now</Button>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => act('Re-gate', () => api.post('/discovery/regate'))}>Re-run preference gate</Button>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => act('Scrape', () => api.post('/scrape/run-all'))}>Run company scrapes</Button>
        </span>
      </HeaderRow>

      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto' }}>
        <div style={{ maxWidth: 1080, margin: '0 auto', padding: '16px 24px 40px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <Stat label="Jobs stored" value={c.total ?? 0} />
            <Stat label="Passed the gate" value={c.gate_passed ?? 0} tone="good" />
            <Stat label="Filtered out" value={c.gate_rejected ?? 0} />
            <Stat label="Not yet gated" value={c.ungated ?? 0} tone={c.ungated ? 'warn' : undefined} />
            <Stat label="Analyzed" value={c.analyzed ?? 0} />
            <Stat label="Analyses running" value={data.analysis_in_flight ?? 0} />
          </div>

          <Section title="Discovery plan"
            sub={`${(data.plan?.queries || []).length} internal queries${data.plan?.truncated ? ' (capped)' : ''} · last run ${data.last_run_at ? ago(data.last_run_at) : 'never'} · ${data.runs_24h || 0} runs in 24h`}>
            <Helper size="xs">
              Built from your Job Preferences every time discovery fires. You never edit this — change the preferences and the plan changes with them.
            </Helper>
            {(data.plan?.queries || []).length === 0 ? (
              <Helper>No queries — set at least one job function under <Link to="/settings">Settings → Job preferences</Link>.</Helper>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {(data.plan.queries || []).map((q, i) => (
                  <Mono key={`${q.search_term}-${q.location}-${i}`} size="lg" code style={{ display: 'block' }}>
                    {q.search_term} · {q.location} ({q.country}) · {q.job_type || 'any type'} · {q.is_remote === true ? 'remote' : q.is_remote === false ? 'non-remote' : 'any model'} · {Math.round((q.hours_old || 0) / 24)}d
                  </Mono>
                ))}
              </div>
            )}
            <Helper size="xs">Boards used: {(data.plan?.boards || []).join(', ') || 'none'}</Helper>
          </Section>

          <Section title="Source health" sub="last 40 discovery runs">
            {boards.length === 0 ? <Helper>No discovery runs recorded yet.</Helper> : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {boards.map(([name, b]) => (
                  <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <Dot tone={b.errors?.length ? 'warn' : b.seen ? 'good' : 'neutral'} />
                    <span style={{ flex: '0 0 140px', fontSize: 'var(--t-13)' }}>{name}</span>
                    <Helper size="xs" style={{ flex: '0 0 160px' }}>{b.seen} seen · {b.new} new</Helper>
                    <Helper size="xs" style={{ flex: 1, minWidth: 0, color: b.errors?.length ? 'var(--bad)' : 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {b.errors?.length ? `${b.errors.length} failure(s): ${b.errors[0]}` : 'no failures'}
                    </Helper>
                  </div>
                ))}
              </div>
            )}
            <Helper size="xs">
              A failed board costs that board only — every other source’s results are still stored, and nothing needs manual recovery.
            </Helper>
          </Section>

          <Section title="Company monitoring"
            sub={`${c.monitored_companies || 0} monitored directly · ${c.unresolved_companies || 0} with no supported careers source`}>
            <Helper size="xs">
              A company starts being monitored when one of its postings passes your Preference Gate and a supported ATS board can be derived from that posting’s own URL. No URL is ever guessed: unresolved companies keep arriving through the aggregators.
            </Helper>
            <Link to="/companies" style={{ fontSize: 'var(--t-12)' }}>Open the company list ↗</Link>
          </Section>

          <Section title="Preference gate rejects" sub="most recent 25">
            {(data.gate_rejects || []).length === 0 ? <Helper>Nothing has been rejected.</Helper> : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                {data.gate_rejects.map((j) => (
                  <div key={j.id} style={{ display: 'flex', gap: 10, alignItems: 'baseline', minWidth: 0 }}>
                    <Link to={`/jobs/${j.id}`} style={{ flex: '0 1 320px', minWidth: 0, fontSize: 'var(--t-13)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {j.title || 'Untitled'} — {j.company || 'Unknown'}
                    </Link>
                    <Helper size="xs" style={{ flex: 1, minWidth: 0 }}>{(j.reasons || []).join(' · ') || 'no reason recorded'}</Helper>
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section title="Current preferences" sub="the document both Jobs and Settings edit">
            <Label>Automatic Candidate Fit</Label>
            <Tag tone={data.auto_analysis_enabled ? 'good' : 'warn'}>{data.auto_analysis_enabled ? 'On' : 'Off'}</Tag>
            <Mono size="lg" code style={{ display: 'block', whiteSpace: 'pre-wrap' }}>
              {JSON.stringify(data.preferences, null, 2)}
            </Mono>
            {(data.saved_filters || []).length > 0 && (
              <>
                <Label>Saved filters</Label>
                <Mono size="lg" code style={{ display: 'block', whiteSpace: 'pre-wrap' }}>
                  {JSON.stringify(data.saved_filters, null, 2)}
                </Mono>
              </>
            )}
          </Section>

          <Section title="Hand-written configuration" sub="preserved, and not needed">
            <Helper size="xs">
              Custom searches and manually configured companies still run exactly as before. Discovery does not touch them.
            </Helper>
            <div style={{ display: 'flex', gap: 12 }}>
              <Link to="/searches" style={{ fontSize: 'var(--t-12)' }}>Custom searches ↗</Link>
              <Link to="/companies" style={{ fontSize: 'var(--t-12)' }}>Company monitors ↗</Link>
              <Link to="/stats#runs" style={{ fontSize: 'var(--t-12)' }}>Run history ↗</Link>
            </div>
          </Section>
        </div>
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
