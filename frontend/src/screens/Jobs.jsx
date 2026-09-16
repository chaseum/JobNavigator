import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate, Link as RouterLink } from 'react-router-dom'
import { MapPin, Building2, Clock, GraduationCap, DollarSign, Heart, EyeOff } from 'lucide-react'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useEscape, fetchRunOutcome, runFailed, runFailureReason } from '../hooks'
import { ago } from '../time'
import { Button, Check, Helper, IconButton, Input, Label, Menu, MenuHead, MenuItem, Notice, Pill, ScoreRing, SearchInput, Select, ShowMore, Spinner, Tag, scoreTone } from '../ui'
import {
  COUNTRY_OPTS, DATE_OPTS, SPONSORSHIP_OPTS, YEARS_OPTS, listToText, refreshDiscovery, textToList, useJobPreferences,
} from '../jobPrefs'

// Jobs: the feed as cards, and the place job criteria are stated.
//
// The filter chips across the top ARE the Job Preferences document — the same one
// Settings edits — so changing one does two things at once: it narrows this list,
// and it changes what JobNavigator goes looking for on its next pass. Nothing here
// mentions a job board, a source, a result count or a run interval; deciding all
// of that is the application's job, not the job seeker's.
//
// The one score on screen is Candidate Fit — how much of the posting the whole
// verified profile satisfies. Not an ATS score, never mixed with legacy AI scores
// (those stay in /classic). A stale or failed match shows as such, never as a number.

const PAGE = 30
const PREFS_KEY = 'jn_jobs_prefs'
const TABS = [
  ['recommended', 'Recommended', 'new,saved'],
  ['saved', 'Saved', 'saved'],
  ['applied', 'Applied', 'applied'],
]
// DATE_OPTS / YEARS_OPTS / COUNTRY_OPTS come from jobPrefs.js so the Jobs chips and
// the Settings form offer exactly the same choices.
// `jn_jobs_prefs` now holds VIEW state only (tab, sort, picked places); the criteria
// themselves are server-side, shared with every other screen.
const SORT_OPTS = [['match', 'Best Candidate Fit'], ['date', 'Newest'], ['salary', 'Highest salary']]
export const ARRANGEMENTS = [['remote', 'Remote'], ['hybrid', 'Hybrid'], ['onsite', 'On-site']]
export const MATCH_LABEL = { good: 'Strong match', warn: 'Partial match', bad: 'Low match' }
// generated logo tints: borrowed from the ATS badge hues, which exist in every theme
const LOGO_TONES = ['greenhouse', 'lever', 'ashby', 'phenom', 'smartrecruiters', 'workday', 'oraclehcm', 'tier2']
// Derived-metadata labels (ids come from backend/discovery/taxonomy.py).
const LEVEL_LABELS = {
  intern: 'Internship', new_grad: 'New Grad', entry: 'Entry', mid: 'Mid', senior: 'Senior',
  staff: 'Staff', lead: 'Lead', manager: 'Manager', director: 'Director',
}
const LEVEL_LABEL = (id) => LEVEL_LABELS[id] || ''
const JOB_TYPE_LABEL = {
  fulltime: 'Full-time', internship: 'Internship', contract: 'Contract', parttime: 'Part-time',
}

const errMsg = (e, fb) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fb)
// sentence case: "full-time" -> "Full-time", "entry level" -> "Entry level"
export const cap = (s) => { const t = String(s || ''); return t.charAt(0).toUpperCase() + t.slice(1) }
export const fmtSalary = (min, max, currency, period, source) => {
  if (!min && !max) return null
  const symbol = currency === 'USD' ? '$' : currency ? `${currency} ` : ''
  const f = (v) => `${symbol}${v >= 10000 ? `${Math.round(v / 1000)}K` : Math.round(v).toLocaleString()}`
  const suffix = ({ yearly: '/yr', monthly: '/mo', weekly: '/wk', daily: '/day', hourly: '/hr' })[String(period || '').toLowerCase()] || ''
  const approximate = source === 'lca_estimate' ? '~' : ''
  return `${approximate}${min && max && min !== max ? `${f(min)} – ${f(max)}` : f(min || max)}${suffix}`
}
const loadPrefs = () => { try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {} } catch { return {} } }

export function Logo({ company }) {
  const name = (company || '?').trim()
  let h = 0
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  const t = LOGO_TONES[h % LOGO_TONES.length]
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase()
  return (
    <span aria-hidden="true" style={{
      flex: '0 0 52px', height: 52, borderRadius: 'var(--radius-cell)', background: `var(--cc-${t}-bg)`, color: `var(--cc-${t}-fg)`,
      display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 'var(--t-17)', fontWeight: 'var(--weight-semibold)',
    }}>{initials}</span>
  )
}

function Meta({ Icon, text, title }) {
  if (!text) return null
  return (
    <span title={title} style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0, fontSize: 'var(--t-13)', color: 'var(--text-2)' }}>
      <Icon size={15} strokeWidth={1.8} aria-hidden="true" style={{ flex: '0 0 auto', color: 'var(--muted)' }} />
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{text}</span>
    </span>
  )
}

const Reason = ({ ok, children }) => (
  <span title={children} style={{ display: 'flex', gap: 5, minWidth: 0, fontSize: 'var(--t-12)', lineHeight: '17px', color: 'var(--text-2)' }}>
    <span aria-hidden="true" style={{ flex: '0 0 auto', color: ok ? 'var(--good)' : 'var(--bad)' }}>{ok ? '✓' : '✗'}</span>
    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{children}</span>
    <span className="sr-only">{ok ? ' (evidenced)' : ' (not evidenced)'}</span>
  </span>
)

function MatchPanel({ rm, needsDetails, analyzing, onAnalyze, onRematch }) {
  const score = rm?.score ?? null
  const tone = scoreTone(score)
  const act = (fn) => (e) => { e?.stopPropagation?.(); fn() }
  return (
    <div style={{
      flex: '0 0 200px', minWidth: 0, padding: '16px 14px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6,
      background: 'var(--recessed)', borderLeft: '1px solid var(--line-soft)', borderRadius: '0 var(--radius-card) var(--radius-card) 0',
    }}>
      <Helper size="xs" title="Based on everything verified in your Profile. Not an ATS score.">Candidate Fit</Helper>
      {analyzing ? (
        <><ScoreRing busy size="md" /><Helper>Analyzing…</Helper></>
      ) : rm?.unavailable && !needsDetails ? (
        <>
          <ScoreRing value={null} label="—" size="md" ariaLabel="Match unavailable" />
          <Helper title={rm.unavailable}>Match unavailable</Helper>
          <Button size="xs" variant="secondary" onClick={act(onRematch)}>Re-match</Button>
        </>
      ) : rm?.stale && score != null ? (
        <>
          <ScoreRing value={null} label="stale" size="md" ariaLabel="Candidate Fit is stale" />
          <Helper>{rm.outdated ? 'Old scoring method' : rm.jd_stale ? 'Job details changed' : 'Profile changed'}</Helper>
          <Button size="xs" variant="secondary" onClick={act(rm.jd_stale ? onAnalyze : onRematch)}>{rm.jd_stale ? 'Re-analyze' : 'Re-match'}</Button>
        </>
      ) : score != null ? (
        <>
          <ScoreRing value={score} size="md" ariaLabel={`Candidate Fit ${score} of 100`} />
          <span style={{ fontSize: 'var(--t-12)', fontWeight: 'var(--weight-semibold)', color: `var(--${tone})` }}>{MATCH_LABEL[tone]}</span>
          <div style={{ alignSelf: 'stretch', display: 'flex', flexDirection: 'column', gap: 2, marginTop: 2 }}>
            {rm.matched.slice(0, 2).map((t) => <Reason key={`m${t}`} ok>{t}</Reason>)}
            {rm.missing.slice(0, 2).map((t) => <Reason key={`x${t}`}>{t}</Reason>)}
          </div>
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', justifyContent: 'center' }}>
            {rm.hard_blockers > 0 && <Tag tone="bad" title="Hard requirements your profile does not meet">{rm.hard_blockers} blocker{rm.hard_blockers === 1 ? '' : 's'}</Tag>}
          </div>
        </>
      ) : needsDetails ? (
        <>
          <ScoreRing value={null} label="—" size="md" />
          <Helper>Needs job details</Helper>
          <Button size="xs" variant="secondary" onClick={(e) => { e?.stopPropagation?.(); onAnalyze() }}>Get details</Button>
        </>
      ) : (
        <>
          <ScoreRing value={null} label="—" size="md" />
          {/* Analysis is automatic for anything that passed your preferences, so
              the honest state here is 'queued', not a chore for the user. The
              button stays as a way to jump the queue for one job. */}
          <Helper>{rm ? 'Match pending' : 'Waiting to be analyzed'}</Helper>
          {!rm && <Button size="xs" variant="secondary" onClick={(e) => { e?.stopPropagation?.(); onAnalyze() }}>Analyze now</Button>}
        </>
      )}
    </div>
  )
}

function JobCard({ job, analyzing, onSave, onHide, onAnalyze, onRematch }) {
  const navigate = useNavigate()
  const rm = job.role_match
  const saved = job.status === 'saved' || !!job.saved
  const arrangement = ARRANGEMENTS.filter(([k]) => job[`arr_${k}`]).map(([, l]) => l).join(' / ')
  // Seniority and years come from the DERIVED metadata every posting gets at
  // insert, falling back to the richer analysis once there is one. The card is
  // therefore never blank here just because an analysis has not run yet.
  const levels = (job.experience_levels || []).map(LEVEL_LABEL).filter(Boolean).join(' / ')
  const years = job.min_years_experience != null ? `${job.min_years_experience}+ yrs` : (rm?.years_required ? `${rm.years_required}+ yrs` : null)
  const level = [levels || cap(rm?.experience_level), years].filter(Boolean).join(' · ')
  const fresh = job.discovered_at && Date.now() - new Date(job.discovered_at).getTime() < 86400000
  const stop = (fn) => (e) => { e?.stopPropagation?.(); fn() }
  return (
    <article className="v2-jobcard" onClick={() => navigate(`/jobs/${job.id}`)} style={{
      display: 'flex', minWidth: 0, cursor: 'pointer',
      background: 'var(--card-bg)', border: '1px solid var(--card-border)', borderRadius: 'var(--radius-card)', boxShadow: 'var(--card-shadow)',
    }}>
      <div style={{ flex: 1, minWidth: 0, padding: '14px 18px 12px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', gap: 14, minWidth: 0 }}>
          <Logo company={job.company} />
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', minHeight: 19 }}>
              {job.discovered_at && <Tag title={`Found by JobNavigator ${new Date(job.discovered_at).toLocaleString()}`}>{ago(job.discovered_at)}</Tag>}
              {fresh && job.status === 'new' && <Tag tone="accent">New</Tag>}
              {job.status === 'applied' && <Tag tone="good">Applied</Tag>}
              {job.h1b_verdict === 'likely' && <Tag title="Company files H-1B petitions">H-1B likely</Tag>}
            </div>
            <RouterLink to={`/jobs/${job.id}`} onClick={(e) => e.stopPropagation()} style={{
              color: 'var(--text)', fontSize: 'var(--t-18)', lineHeight: '24px', fontWeight: 'var(--weight-semibold)',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{job.title || 'Untitled role'}</RouterLink>
            <span style={{ fontSize: 'var(--t-13)', color: 'var(--text-2)' }}>{job.company || 'Unknown company'}</span>
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: '6px 16px', paddingLeft: 66 }}>
          <Meta Icon={MapPin} text={job.location} />
          <Meta Icon={Building2} text={arrangement} />
          <Meta Icon={Clock} text={JOB_TYPE_LABEL[job.job_type] || cap(rm?.employment_type)} />
          <Meta Icon={GraduationCap} text={level} title="Seniority and years of experience the posting asks for" />
          <Meta Icon={DollarSign} text={fmtSalary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period, job.salary_source)} title={job.salary_source === 'lca_estimate' ? 'H-1B filing estimate' : job.salary_source === 'posting_ats' ? 'Employer-posted compensation · ATS' : job.salary_source === 'posting_description' ? 'Employer-posted compensation · job description' : job.salary_source?.startsWith('jobspy_') ? 'Compensation reported by JobSpy; pay period and currency are retained' : undefined} />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8, flexWrap: 'wrap' }}>
          <IconButton size={36} title="Hide this job" onClick={stop(onHide)}><EyeOff size={15} aria-hidden="true" /></IconButton>
          <IconButton size={36} on={saved} title={saved ? 'Saved — click to unsave' : 'Save'} onClick={stop(onSave)}>
            <Heart size={15} fill={saved ? 'currentColor' : 'none'} aria-hidden="true" />
          </IconButton>
          <Button size="sm" variant="secondary" onClick={stop(() => navigate(`/jobs/${job.id}?tab=resume`))}>Tailor resume</Button>
          <Button size="sm" href={job.url || undefined} target="_blank" disabled={!job.url} onClick={(e) => e.stopPropagation()}
            title={job.url ? 'Opens the posting; the JobNavigator extension fills the form from your accepted résumé and answers' : 'No posting URL'}>
            Apply with autofill
          </Button>
        </div>
      </div>
      <MatchPanel rm={rm} needsDetails={job.needs_job_details} analyzing={analyzing} onAnalyze={onAnalyze} onRematch={onRematch} />
    </article>
  )
}

// a pill that opens a menu under itself
function FilterChip({ id, open, setOpen, label, active, onClear, width = 240, children }) {
  const isOpen = open === id
  return (
    <div style={{ position: 'relative', flex: '0 0 auto' }}>
      <Pill on={active} onClick={() => setOpen(isOpen ? null : id)} ariaExpanded={isOpen} ariaHaspopup="menu">
        {label}<span aria-hidden="true" style={{ fontSize: 'var(--t-9)', opacity: 0.7 }}>▾</span>
      </Pill>
      {isOpen && (
        <Menu onDismiss={() => setOpen(null)} className="v2-scroll" ariaLabel={`${id} filter`}
          style={{ position: 'absolute', top: 'calc(100% + 6px)', left: 0, zIndex: 50, width, maxHeight: 380, overflowY: 'auto' }}>
          {children}
          {active && onClear && <MenuItem divider onClick={() => { onClear(); setOpen(null) }}>Clear</MenuItem>}
        </Menu>
      )}
    </div>
  )
}

export default function Jobs() {
  const [view] = useState(loadPrefs)
  const [tab, setTab] = useState(view.tab || 'recommended')
  const [sort, setSort] = useState(view.sort || 'match')
  // Local view state. These narrow what is ON SCREEN and are not criteria:
  // `places` is the facet picker's selection, `q` is the search box as typed.
  const [places, setPlaces] = useState(view.places || [])
  const [q, setQ] = useState('')
  const [dq, setDq] = useState('')
  const [open, setOpen] = useState(null)
  const [locQ, setLocQ] = useState('')
  const [jobs, setJobs] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(false)
  const [facets, setFacets] = useState({})
  const [pending, setPending] = useState({})   // job id -> { run_id, type }
  const [refreshing, setRefreshing] = useState(false)
  const { toasts, push: pushToast, dismiss } = useToasts()
  const jobsRef = useRef(jobs); jobsRef.current = jobs
  const pendingRef = useRef(pending); pendingRef.current = pending
  useEscape(() => setOpen(null), open !== null)

  // The criteria themselves live in the one canonical preferences document, so
  // editing them here is editing the same thing Settings edits — and changing
  // one re-plans discovery, not just this list's WHERE clause.
  const { prefs, taxonomy, patch, toggle, error: prefsErr } = useJobPreferences()

  useEffect(() => { try { localStorage.setItem(PREFS_KEY, JSON.stringify({ tab, sort, places })) } catch { /* ignore */ } }, [tab, sort, places])
  useEffect(() => { const t = setTimeout(() => setDq(q.trim()), 350); return () => clearTimeout(t) }, [q])
  useEffect(() => { if (open !== 'location') setLocQ('') }, [open])

  const params = useMemo(() => {
    if (!prefs) return null
    const p = { status: TABS.find(([k]) => k === tab)[2] }
    if (dq) p.title_search = dq
    if (tab === 'recommended') p.recommended = 1
    if (prefs.job_functions.length) p.job_function = prefs.job_functions.join(',')
    if (prefs.levels.length) p.level = prefs.levels.join(',')
    if (prefs.job_types.length) p.job_type = prefs.job_types.join(',')
    if (prefs.work_models.length && prefs.work_models.length < 3) p.arrangement = prefs.work_models.join(',')
    if (prefs.date_posted_days) p.since_days = prefs.date_posted_days
    if (prefs.max_years_experience != null) p.max_years = prefs.max_years_experience
    if (prefs.minimum_salary) p.min_salary = prefs.minimum_salary
    if (prefs.companies.length) p.company = prefs.companies.join(',')
    // A picked place is more specific than the country it sits in, so it wins.
    const loc = places.length ? places : prefs.countries
    if (loc.length) p.location = loc.join(',')
    return p
  }, [tab, dq, prefs, places])

  const load = useCallback(async (append = false) => {
    if (!params) return
    setLoading(true)
    try {
      const offset = append ? jobsRef.current.length : 0
      const { data } = await api.get('/jobs', { params: { ...params, sort_by: sort, limit: PAGE, offset, brief: 1 } })
      const rows = data.jobs || []
      setJobs((prev) => (append ? [...prev, ...rows.filter((r) => !prev.some((p) => p.id === r.id))] : rows))
      setTotal(data.total || 0)
      setErr(false)
    } catch (e) {
      setErr(e?.response?.status !== 401)
    } finally { setLoading(false) }
  }, [params, sort])
  useEffect(() => { load(false) }, [load])
  useEffect(() => {
    if (!params) return
    api.get('/jobs/facets', { params }).then(({ data }) => setFacets(data || {})).catch(() => { /* menus keep their last counts */ })
  }, [params])

  // Discovery and automatic analysis run in the background, so the feed grows on
  // its own. Poll slowly while the tab is open rather than making the user reload.
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) load(false) }, 90000)
    return () => clearInterval(t)
  }, [load])

  // analyses this screen started: poll until each settles, then refresh the page of cards
  useEffect(() => {
    if (!Object.keys(pending).length) return undefined
    const t = setInterval(async () => {
      const next = { ...pendingRef.current }
      let settled = false
      for (const [jid, run] of Object.entries(next)) {
        const out = await fetchRunOutcome(run.run_id, run.type)
        if (!out || out.status === 'running') continue
        delete next[jid]; settled = true
        if (runFailed(out)) pushToast({ kind: 'error', msg: `${run.type === 'copilot_match' ? 'Re-match' : 'Analysis'} failed — ${runFailureReason(out)}` })
      }
      if (settled) { setPending(next); load(false) }
    }, 3000)
    return () => clearInterval(t)
  }, [pending, load, pushToast])

  const patchJob = async (job, changes) => {
    // legacy_score=false: saving here never starts the classic LLM CV scorer
    try { await api.patch(`/jobs/${job.id}`, changes, { params: { legacy_score: false } }); return true } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not update this job') }); load(false); return false }
  }
  const toggleSave = async (job) => {
    const on = !(job.status === 'saved' || job.saved)
    // an applied job keeps its status; only the saved flag moves
    const changes = job.status === 'applied' ? { saved: on } : { saved: on, status: on ? 'saved' : 'new' }
    setJobs((prev) => (tab === 'saved' && !on ? prev.filter((j) => j.id !== job.id) : prev.map((j) => (j.id === job.id ? { ...j, ...changes } : j))))
    if (await patchJob(job, changes)) window.dispatchEvent(new CustomEvent('jn:counts-changed'))
  }
  const hide = async (job) => {
    setJobs((prev) => prev.filter((j) => j.id !== job.id)); setTotal((n) => Math.max(0, n - 1))
    if (await patchJob(job, { status: 'skip' })) {
      pushToast({ kind: 'undo', msg: `Hid "${job.title || 'job'}"`, action: 'Undo', onAction: async () => { await patchJob(job, { status: job.status }); load(false) } })
    }
  }
  const startRun = async (job, type) => {
    const [path, label] = type === 'copilot_match' ? ['match', 'Re-matching'] : ['analyze', 'Analyzing']
    try {
      const { data } = await api.post(`/copilot/jobs/${job.id}/${path}`)
      setPending((p) => ({ ...p, [job.id]: { run_id: data.run_id, type } }))
      pushToast({ kind: 'progress', msg: `${label} ${job.title || 'job'}…` })
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, `${label} could not start`) }) }
  }
  const analyze = (job) => startRun(job, 'copilot_analyze')

  // The one discovery action a normal user gets. It means "go and look again
  // using my current preferences" — never "run this scraper configuration".
  const refresh = async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      await refreshDiscovery()
      pushToast({ kind: 'progress', msg: 'Looking for new jobs — they appear here as they arrive' })
      setTimeout(() => load(false), 8000)
    } catch (e) {
      const busy = e?.response?.status === 409
      pushToast({ kind: busy ? 'progress' : 'error',
        msg: busy ? 'Already looking for new jobs' : errMsg(e, 'Could not start a refresh') })
    } finally { setRefreshing(false) }
  }

  // Committing the search box makes it a discovery intent as well as a filter:
  // until then it only narrows what is already here.
  const commitSearch = () => {
    if (!prefs) return
    patch({ title_query: q.trim() })
    pushToast({ kind: 'progress', msg: q.trim() ? `Now also searching boards for "${q.trim()}"` : 'Search term cleared from your preferences' })
  }

  const statusCount = Object.fromEntries((facets.statuses || []).map((x) => [x.name, x.count]))
  const tabCount = { recommended: (statusCount.new || 0) + (statusCount.saved || 0), saved: statusCount.saved || 0, applied: statusCount.applied || 0 }
  const countOf = (list, name) => (list || []).find((x) => x.name === name)?.count
  const labelsOf = (ids, options) => (options || []).filter((o) => (ids || []).includes(o.id)).map((o) => o.label)
  const multiLabel = (base, ids, options) => {
    const picked = labelsOf(ids, options)
    return picked.length === 1 ? picked[0] : picked.length ? `${base} (${picked.length})` : base
  }
  const placeRows = (facets.locations || []).filter((l) => !locQ || l.name.toLowerCase().includes(locQ.toLowerCase())).slice(0, 80)

  // Picking a place narrows the list AND tells discovery where to look: the
  // country code goes to `countries`, anything finer to `locations`.
  const pickPlace = (l) => {
    const next = places.includes(l.key) ? places.filter((k) => k !== l.key) : [...places, l.key]
    setPlaces(next)
    if (!prefs) return
    const countries = [...new Set(next.map((k) => k.split(':')[0]).filter(Boolean))]
    const finer = (facets.locations || []).filter((x) => next.includes(x.key) && x.level > 0).map((x) => x.name)
    patch({ countries: countries.length ? countries : prefs.countries, locations: finer })
  }

  const T = taxonomy
  const chips = T && prefs ? [
    ['function', multiLabel('Job function', prefs.job_functions, T.job_functions), prefs.job_functions.length,
      () => patch({ job_functions: [] }), T.job_functions, 'job_functions', facets.job_functions],
    ['level', multiLabel('Level', prefs.levels, T.levels), prefs.levels.length,
      () => patch({ levels: [] }), T.levels, 'levels', facets.levels],
    ['type', multiLabel('Job type', prefs.job_types, T.job_types), prefs.job_types.length,
      () => patch({ job_types: [] }), T.job_types, 'job_types', facets.job_types],
    ['model', multiLabel('Work model', prefs.work_models, T.work_models), prefs.work_models.length,
      () => patch({ work_models: [] }), T.work_models, 'work_models', facets.arrangements],
  ] : []

  const yearsLabel = !prefs || prefs.max_years_experience == null ? 'Experience'
    : (YEARS_OPTS.find(([v]) => v === String(prefs.max_years_experience)) || [])[1] || 'Experience'
  const dateLabel = (DATE_OPTS.find(([v]) => v === String(prefs?.date_posted_days)) || [])[1] || 'Date posted'
  const countryLabel = (prefs?.countries || []).map((c) => (COUNTRY_OPTS.find(([id]) => id === c) || [])[1] || c).join(', ')
  const locationLabel = places.length
    ? (places.length === 1 ? ((facets.locations || []).find((l) => l.key === places[0])?.name || places[0]) : `Location (${places.length})`)
    : (countryLabel || 'Location')
  const moreCount = prefs ? (prefs.companies.length ? 1 : 0) + (prefs.excluded_companies.length ? 1 : 0)
    + (prefs.minimum_salary ? 1 : 0) + (prefs.sponsorship !== 'any' ? 1 : 0) : 0

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <header style={{ flex: '0 0 auto', background: 'var(--surface)', borderBottom: '1px solid var(--line)' }}>
        <div style={{ display: 'flex', alignItems: 'stretch', gap: 24, height: 56, padding: '0 24px' }}>
          <h1 style={{ margin: 0, alignSelf: 'center', fontSize: 'var(--t-19)', fontWeight: 'var(--weight-semibold)' }}>Jobs</h1>
          <div role="tablist" aria-label="Job lists" style={{ display: 'flex', gap: 20 }}>
            {TABS.map(([k, label]) => (
              <button key={k} type="button" role="tab" aria-selected={tab === k} className="v2-tabbtn" onClick={() => setTab(k)}>
                {label}
                {facets.statuses && <span style={{ fontSize: 'var(--t-11)', padding: '1px 7px', borderRadius: 'var(--radius-round)', background: 'var(--surface-2)', color: 'var(--text-2)' }}>{tabCount[k]}</span>}
              </button>
            ))}
          </div>
          <span style={{ marginLeft: 'auto', alignSelf: 'center', display: 'flex', alignItems: 'center', gap: 8 }}>
            <SearchInput value={q} onChange={setQ} placeholder="Search job title or company" ariaLabel="Search jobs" width="240px" />
            {q.trim() !== (prefs?.title_query || '') && (
              <Button size="sm" variant="secondary" onClick={commitSearch}
                title="Also look for this on job boards, not only in the jobs already here">Search everywhere</Button>
            )}
            <Button size="sm" variant="secondary" onClick={refresh} disabled={refreshing}
              title="Look for new jobs using your current preferences">
              {refreshing ? 'Refreshing…' : 'Refresh jobs'}
            </Button>
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', padding: '10px 24px', borderTop: '1px solid var(--line-soft)' }}>
          <FilterChip id="location" open={open} setOpen={setOpen} width={300} label={locationLabel}
            active={places.length > 0} onClear={() => setPlaces([])}>
            <MenuHead>Where to search</MenuHead>
            {COUNTRY_OPTS.map(([id, label]) => (
              <MenuItem key={id} icon={<Check checked={(prefs?.countries || []).includes(id)} />}
                onClick={() => toggle('countries', id)}>{label}</MenuItem>
            ))}
            <MenuHead>Narrow to a place</MenuHead>
            <div style={{ padding: 6 }}><Input value={locQ} onChange={setLocQ} placeholder="Filter places…" ariaLabel="Filter places" /></div>
            {placeRows.length === 0 && <Helper style={{ padding: '6px 11px' }}>No places yet</Helper>}
            {placeRows.map((l) => (
              <MenuItem key={l.key} ellipsis icon={<Check checked={places.includes(l.key)} />} hint={l.count}
                onClick={() => pickPlace(l)} style={{ paddingLeft: 11 + l.level * 14 }}>{l.name}</MenuItem>
            ))}
          </FilterChip>

          {chips.map(([id, label, count, clear, options, key, facetRows]) => (
            <FilterChip key={id} id={id} open={open} setOpen={setOpen} label={label} active={count > 0} onClear={clear}>
              {options.map((o) => (
                <MenuItem key={o.id} icon={<Check checked={(prefs[key] || []).includes(o.id)} />}
                  hint={countOf(facetRows, o.id)} onClick={() => toggle(key, o.id)}>{o.label}</MenuItem>
              ))}
            </FilterChip>
          ))}

          <FilterChip id="date" open={open} setOpen={setOpen} width={200} label={dateLabel} active={false}>
            {DATE_OPTS.map(([v, label]) => (
              <MenuItem key={v} role="menuitemradio" selected={String(prefs?.date_posted_days) === v}
                onClick={() => { patch({ date_posted_days: Number(v) }); setOpen(null) }}>{label}</MenuItem>
            ))}
          </FilterChip>
          <FilterChip id="years" open={open} setOpen={setOpen} width={200} label={yearsLabel}
            active={prefs?.max_years_experience != null} onClear={() => patch({ max_years_experience: null })}>
            {YEARS_OPTS.map(([v, label]) => (
              <MenuItem key={v || 'any'} role="menuitemradio"
                selected={v === (!prefs || prefs.max_years_experience == null ? '' : String(prefs.max_years_experience))}
                onClick={() => { patch({ max_years_experience: v === '' ? null : Number(v) }); setOpen(null) }}>{label}</MenuItem>
            ))}
          </FilterChip>

          <FilterChip id="more" open={open} setOpen={setOpen} width={320}
            label={moreCount ? `More (${moreCount})` : 'More'} active={moreCount > 0}
            onClear={() => patch({ companies: [], excluded_companies: [], minimum_salary: null, sponsorship: 'any' })}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '8px 10px' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <Label>Only these companies</Label>
                <Input value={listToText(prefs?.companies)} ariaLabel="Only these companies" placeholder="any company"
                  onChange={(v) => patch({ companies: textToList(v) })} />
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <Label>Never these companies</Label>
                <Input value={listToText(prefs?.excluded_companies)} ariaLabel="Excluded companies"
                  onChange={(v) => patch({ excluded_companies: textToList(v) })} />
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <Label>Minimum salary</Label>
                <Input value={prefs?.minimum_salary == null ? '' : String(prefs.minimum_salary)} ariaLabel="Minimum salary" placeholder="any"
                  onChange={(v) => { const d = v.replace(/[^0-9]/g, ''); patch({ minimum_salary: d === '' ? null : Number(d) }) }} />
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <Label>Sponsorship</Label>
                <Select value={prefs?.sponsorship || 'any'} options={SPONSORSHIP_OPTS} ariaLabel="Sponsorship"
                  onPick={(v) => patch({ sponsorship: v })} />
              </div>
            </div>
          </FilterChip>

          <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
            <Helper>{loading ? <Spinner /> : `${total} job${total === 1 ? '' : 's'}`}</Helper>
            <Select value={sort} options={SORT_OPTS} onPick={setSort} width="180px" ariaLabel="Sort jobs" />
          </span>
        </div>
      </header>

      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto' }}>
        <div style={{ maxWidth: 1180, margin: '0 auto', padding: '16px 24px 40px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {prefsErr && <Notice tone="warn"><Helper>{prefsErr}</Helper></Notice>}
          {err ? (
            <Notice tone="bad" action={<Button size="sm" variant="secondary" onClick={() => load(false)}>Retry</Button>}>
              <Helper>Couldn’t load jobs. Check that the backend is running.</Helper>
            </Notice>
          ) : !loading && jobs.length === 0 ? (
            <Notice tone="quiet" glyph="○" action={<Button size="sm" variant="secondary" onClick={refresh}>Refresh jobs</Button>}>
              <Helper>
                {tab === 'recommended'
                  ? 'Nothing matches your preferences yet. JobNavigator is looking — widen the filters above to see more.'
                  : `No ${tab} jobs yet.`}
              </Helper>
            </Notice>
          ) : jobs.map((j) => (
            <JobCard key={j.id} job={j} analyzing={!!pending[j.id] || (j.in_flight || []).some((t) => t === 'copilot_analyze' || t === 'copilot_match')}
              onSave={() => toggleSave(j)} onHide={() => hide(j)} onAnalyze={() => analyze(j)} onRematch={() => startRun(j, 'copilot_match')} />
          ))}
          {!err && jobs.length < total && (
            <ShowMore n={Math.min(PAGE, total - jobs.length)} onClick={() => load(true)} />
          )}
        </div>
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
