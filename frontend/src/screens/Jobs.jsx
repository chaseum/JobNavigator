import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate, Link as RouterLink } from 'react-router-dom'
import { MapPin, Building2, Clock, GraduationCap, DollarSign, Heart, EyeOff } from 'lucide-react'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useEscape, fetchRunOutcome, runFailed, runFailureReason } from '../hooks'
import { ago } from '../time'
import { Button, Check, Helper, IconButton, Input, Menu, MenuItem, Notice, Pill, ScoreRing, SearchInput, Select, ShowMore, Spinner, Tag, scoreTone } from '../ui'

// Jobs: the feed as cards. The one score on screen is Candidate Fit — how much of
// the posting the whole verified profile satisfies. Not an ATS score, never mixed
// with legacy AI scores (those stay in /classic). A stale or failed match shows as
// such, never as a number.

const PAGE = 30
const PREFS_KEY = 'jn_jobs_prefs'
const TABS = [
  ['recommended', 'Recommended', 'new,saved'],
  ['saved', 'Saved', 'saved'],
  ['applied', 'Applied', 'applied'],
]
const NO_FILTERS = { location: [], level: [], employment_type: [], arrangement: [], since_days: '', max_years: '', min_match: '' }
const DATE_OPTS = [['1', 'Past 24 hours'], ['3', 'Past 3 days'], ['7', 'Past week'], ['30', 'Past month']]
const YEARS_OPTS = [['1', 'Up to 1 year'], ['3', 'Up to 3 years'], ['5', 'Up to 5 years'], ['10', 'Up to 10 years']]
const MATCH_OPTS = [['50', '50+'], ['60', '60+'], ['70', '70+'], ['80', '80+']]
const SORT_OPTS = [['match', 'Best Candidate Fit'], ['date', 'Newest'], ['salary', 'Highest salary']]
export const ARRANGEMENTS = [['remote', 'Remote'], ['hybrid', 'Hybrid'], ['onsite', 'On-site']]
export const MATCH_LABEL = { good: 'Strong match', warn: 'Partial match', bad: 'Low match' }
// generated logo tints: borrowed from the ATS badge hues, which exist in every theme
const LOGO_TONES = ['greenhouse', 'lever', 'ashby', 'phenom', 'smartrecruiters', 'workday', 'oraclehcm', 'tier2']

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
          <Helper>{rm ? 'Match pending' : 'Not analyzed'}</Helper>
          {!rm && <Button size="xs" variant="secondary" onClick={(e) => { e?.stopPropagation?.(); onAnalyze() }}>Analyze</Button>}
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
  const level = [cap(rm?.experience_level), rm?.years_required ? `${rm.years_required}+ yrs` : null].filter(Boolean).join(' · ')
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
          <Meta Icon={Clock} text={cap(rm?.employment_type)} />
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
  const [prefs] = useState(loadPrefs)
  const [tab, setTab] = useState(prefs.tab || 'recommended')
  const [filters, setFilters] = useState({ ...NO_FILTERS, ...(prefs.filters || {}) })
  const [sort, setSort] = useState(prefs.sort || 'match')
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
  const { toasts, push: pushToast, dismiss } = useToasts()
  const jobsRef = useRef(jobs); jobsRef.current = jobs
  const pendingRef = useRef(pending); pendingRef.current = pending
  useEscape(() => setOpen(null), open !== null)

  useEffect(() => { try { localStorage.setItem(PREFS_KEY, JSON.stringify({ tab, filters, sort })) } catch { /* ignore */ } }, [tab, filters, sort])
  useEffect(() => { const t = setTimeout(() => setDq(q.trim()), 350); return () => clearTimeout(t) }, [q])
  useEffect(() => { if (open !== 'location') setLocQ('') }, [open])

  const params = useMemo(() => {
    const p = { status: TABS.find(([k]) => k === tab)[2] }
    if (dq) p.title_search = dq
    for (const k of ['location', 'level', 'employment_type', 'arrangement']) if (filters[k].length) p[k] = filters[k].join(',')
    for (const k of ['since_days', 'max_years', 'min_match']) if (filters[k] !== '') p[k] = filters[k]
    return p
  }, [tab, dq, filters])

  const load = useCallback(async (append = false) => {
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
  useEffect(() => { api.get('/jobs/facets', { params }).then(({ data }) => setFacets(data || {})).catch(() => { /* menus keep their last counts */ }) }, [params])

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

  const setF = (k, v) => setFilters((f) => ({ ...f, [k]: v }))
  const toggleIn = (k, v) => setFilters((f) => ({ ...f, [k]: f[k].includes(v) ? f[k].filter((x) => x !== v) : [...f[k], v] }))
  const anyFilter = !!dq || Object.entries(filters).some(([, v]) => (Array.isArray(v) ? v.length : v !== ''))

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
      pushToast({ kind: 'undo', msg: `Hid “${job.title || 'job'}”`, action: 'Undo', onAction: async () => { await patchJob(job, { status: job.status }); load(false) } })
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

  const statusCount = Object.fromEntries((facets.statuses || []).map((x) => [x.name, x.count]))
  const tabCount = { recommended: (statusCount.new || 0) + (statusCount.saved || 0), saved: statusCount.saved || 0, applied: statusCount.applied || 0 }
  const countOf = (list, name) => (list || []).find((x) => x.name === name)?.count
  const multiLabel = (base, k, fmt = cap) => (filters[k].length === 1 ? fmt(filters[k][0]) : filters[k].length ? `${base} (${filters[k].length})` : base)
  const singleLabel = (base, k, opts) => (filters[k] !== '' ? (opts.find(([v]) => v === filters[k]) || [])[1] || base : base)
  const locName = (key) => (facets.locations || []).find((l) => l.key === key)?.name || key
  const places = (facets.locations || []).filter((l) => !locQ || l.name.toLowerCase().includes(locQ.toLowerCase())).slice(0, 80)

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
          <SearchInput value={q} onChange={setQ} placeholder="Search by job title" ariaLabel="Search jobs by title" width="280px" style={{ marginLeft: 'auto', alignSelf: 'center' }} />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', padding: '10px 24px', borderTop: '1px solid var(--line-soft)' }}>
          <FilterChip id="role" open={open} setOpen={setOpen} label={dq ? `Role: ${dq}` : 'Role'} active={!!dq} onClear={() => setQ('')}>
            <div style={{ padding: 6 }}><Input autoFocus value={q} onChange={setQ} placeholder="Job title contains…" ariaLabel="Role" /></div>
          </FilterChip>
          <FilterChip id="location" open={open} setOpen={setOpen} width={300} label={multiLabel('Location', 'location', locName)} active={filters.location.length > 0} onClear={() => setF('location', [])}>
            <div style={{ padding: 6 }}><Input autoFocus value={locQ} onChange={setLocQ} placeholder="Filter places…" ariaLabel="Filter places" /></div>
            {places.length === 0 && <Helper style={{ padding: '6px 11px' }}>No places</Helper>}
            {places.map((l) => (
              <MenuItem key={l.key} ellipsis icon={<Check checked={filters.location.includes(l.key)} />} hint={l.count}
                onClick={() => toggleIn('location', l.key)} style={{ paddingLeft: 11 + l.level * 14 }}>{l.name}</MenuItem>
            ))}
          </FilterChip>
          <FilterChip id="level" open={open} setOpen={setOpen} label={multiLabel('Level', 'level')} active={filters.level.length > 0} onClear={() => setF('level', [])}>
            {!(facets.levels || []).length && <Helper style={{ padding: '6px 11px', display: 'block' }}>Seniority comes from job analysis. Analyze jobs to fill this list.</Helper>}
            {(facets.levels || []).map((x) => (
              <MenuItem key={x.name} icon={<Check checked={filters.level.includes(x.name)} />} hint={x.count} onClick={() => toggleIn('level', x.name)}>{cap(x.name)}</MenuItem>
            ))}
          </FilterChip>
          <FilterChip id="employment" open={open} setOpen={setOpen} label={multiLabel('Employment type', 'employment_type')} active={filters.employment_type.length > 0} onClear={() => setF('employment_type', [])}>
            {!(facets.employment_types || []).length && <Helper style={{ padding: '6px 11px', display: 'block' }}>Employment type comes from job analysis. Analyze jobs to fill this list.</Helper>}
            {(facets.employment_types || []).map((x) => (
              <MenuItem key={x.name} icon={<Check checked={filters.employment_type.includes(x.name)} />} hint={x.count} onClick={() => toggleIn('employment_type', x.name)}>{cap(x.name)}</MenuItem>
            ))}
          </FilterChip>
          <FilterChip id="arrangement" open={open} setOpen={setOpen} width={200} label={multiLabel('Remote / on-site', 'arrangement', (v) => (ARRANGEMENTS.find(([k]) => k === v) || [])[1])} active={filters.arrangement.length > 0} onClear={() => setF('arrangement', [])}>
            {ARRANGEMENTS.map(([k, label]) => (
              <MenuItem key={k} icon={<Check checked={filters.arrangement.includes(k)} />} hint={countOf(facets.arrangements, k)} onClick={() => toggleIn('arrangement', k)}>{label}</MenuItem>
            ))}
          </FilterChip>
          {[['date', 'since_days', 'Date found', DATE_OPTS], ['experience', 'max_years', 'Experience', YEARS_OPTS], ['match', 'min_match', 'Min match', MATCH_OPTS]].map(([id, k, base, opts]) => (
            <FilterChip key={id} id={id} open={open} setOpen={setOpen} width={200} label={singleLabel(base, k, opts)} active={filters[k] !== ''} onClear={() => setF(k, '')}>
              {opts.map(([v, label]) => (
                <MenuItem key={v} role="menuitemradio" selected={filters[k] === v} onClick={() => { setF(k, filters[k] === v ? '' : v); setOpen(null) }}>{label}</MenuItem>
              ))}
            </FilterChip>
          ))}
          {anyFilter && <Button variant="ghost" size="xs" onClick={() => { setFilters(NO_FILTERS); setQ('') }}>Clear all</Button>}
          <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
            <Helper>{loading ? <Spinner /> : `${total} job${total === 1 ? '' : 's'}`}</Helper>
            <Select value={sort} options={SORT_OPTS} onPick={setSort} width="180px" ariaLabel="Sort jobs" />
          </span>
        </div>
      </header>

      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto' }}>
        <div style={{ maxWidth: 1180, margin: '0 auto', padding: '16px 24px 40px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {(filters.level.length > 0 || filters.employment_type.length > 0 || filters.max_years !== '' || filters.min_match !== '') && (
            <Helper>Level, employment type, experience and fit filters read job analysis, so jobs that have not been analyzed are hidden while they are set.</Helper>
          )}
          {err ? (
            <Notice tone="bad" action={<Button size="sm" variant="secondary" onClick={() => load(false)}>Retry</Button>}>
              <Helper>Couldn’t load jobs. Check that the backend is running.</Helper>
            </Notice>
          ) : !loading && jobs.length === 0 ? (
            <Notice tone="quiet" glyph="○" action={anyFilter ? <Button size="sm" variant="secondary" onClick={() => { setFilters(NO_FILTERS); setQ('') }}>Clear filters</Button> : null}>
              <Helper>{anyFilter ? 'No jobs match these filters.' : tab === 'recommended' ? 'No open jobs yet. Add companies or searches to start finding roles.' : `No ${tab} jobs yet.`}</Helper>
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
