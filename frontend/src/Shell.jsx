import React, { useState, useEffect } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import {
  Briefcase, FileText, Send, IdCard, Settings as SettingsIcon, LayoutDashboard, Search, Building2,
  Mail, MessageSquareText, Fingerprint, ChartLine,
} from 'lucide-react'
import api from './api'
import { ago } from './time'
import { useTheme, themeAttrs, appearanceTitle, MODE_ICON } from './theme'
import { Dot, Helper, IconButton, Label } from './ui'
import './theme.css'

// App shell: a light fixed sidebar. Five primary destinations, the rest of the app
// in a quieter group below, scrape health and the user at the foot.
const PRIMARY = [
  ['/feed', 'Jobs', Briefcase], ['/resumes', 'Resume', FileText], ['/applications', 'Applications', Send],
  ['/profile', 'Profile', IdCard], ['/settings', 'Settings', SettingsIcon],
]
const MORE = [
  ['/dashboard', 'Dashboard', LayoutDashboard], ['/searches', 'Searches', Search], ['/companies', 'Companies', Building2],
  ['/cover-letters', 'Cover letters', Mail], ['/answer-bank', 'Answer bank', MessageSquareText],
  ['/persona', 'Persona', Fingerprint], ['/stats', 'Stats', ChartLine],
]
// a job's workspace belongs to Jobs
const isActive = (path, to) => path === to || path.startsWith(to + '/') || (to === '/feed' && path.startsWith('/jobs/'))

function useHealth() {
  const [h, setH] = useState(null)
  useEffect(() => {
    Promise.allSettled([
      api.get('/health/entities'),
      api.get('/monitor/history', { params: { limit: 1, job_type: 'scrape_all' } }),
    ]).then(([w, r]) => {
      // a rejection with no response at all is "backend unreachable"; a 500 is the screen's to report
      const netErr = (x) => x.status === 'rejected' && !x.reason?.response
      if (netErr(w) && netErr(r)) return setH({ tone: 'bad', text: 'Backend unreachable' })
      const d = w.status === 'fulfilled' ? w.value.data : null
      const failing = (d?.companies || []).length + (d?.searches || []).length
      const run = r.status === 'fulfilled' ? (r.value.data || [])[0] : null
      if (failing) return setH({ tone: 'warn', text: `${failing} source${failing === 1 ? ' needs' : 's need'} attention` })
      if (run?.status === 'failed') return setH({ tone: 'warn', text: `Last scrape failed · ${ago(run.finished_at || run.started_at)}` })
      setH({ tone: 'good', text: run ? `Scraper healthy · ${ago(run.finished_at || run.started_at)}` : 'No scrape recorded yet' })
    })
  }, [])
  return h
}

function NavItem({ to, label, Icon, path, small }) {
  return (
    <Link to={to} className={small ? 'v2-sidenav v2-sidenav-sm' : 'v2-sidenav'} aria-current={isActive(path, to) ? 'page' : undefined}>
      <Icon size={small ? 15 : 17} strokeWidth={1.8} aria-hidden="true" />
      <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
    </Link>
  )
}

export default function Shell() {
  const { pathname } = useLocation()
  const look = useTheme()
  const health = useHealth()
  const [name, setName] = useState('')
  useEffect(() => {
    api.get('/profile').then(({ data }) => {
      const c = data?.identity?.contact || {}
      setName([c.preferred_name || c.first_name, c.last_name].filter(Boolean).join(' '))
    }).catch(() => { /* the name is decoration; Profile reports its own errors */ })
  }, [])

  return (
    <div className="jn-v2" {...themeAttrs(look)} style={{ display: 'flex', height: '100vh', overflow: 'hidden', background: 'var(--bg)' }}>
      <aside style={{ flex: '0 0 212px', display: 'flex', flexDirection: 'column', background: 'var(--surface)', borderRight: '1px solid var(--line)' }}>
        <div style={{ height: 60, display: 'flex', alignItems: 'center', gap: 9, padding: '0 18px', flex: '0 0 auto' }}>
          <img src="/favicon-48.png" alt="" width={24} height={24} />
          <span style={{ fontSize: 'var(--t-17)', fontWeight: 'var(--weight-semibold)', letterSpacing: '-.01em' }}>JobNavigator</span>
        </div>
        <nav aria-label="Main" className="v2-scroll" style={{ flex: 1, overflowY: 'auto', padding: '4px 10px', display: 'flex', flexDirection: 'column', gap: 2 }}>
          {PRIMARY.map(([to, label, Icon]) => <NavItem key={to} to={to} label={label} Icon={Icon} path={pathname} />)}
          <Label style={{ padding: '18px 10px 6px' }}>More</Label>
          {MORE.map(([to, label, Icon]) => <NavItem key={to} to={to} label={label} Icon={Icon} path={pathname} small />)}
        </nav>
        <div style={{ flex: '0 0 auto', borderTop: '1px solid var(--line-soft)', padding: '10px 14px 12px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {health && (
            <Link to="/stats#runs" title="Open run history" style={{ display: 'flex', alignItems: 'center', gap: 8, textDecoration: 'none' }}>
              <Dot tone={health.tone} />
              <Helper style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{health.text}</Helper>
            </Link>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
            <span aria-hidden="true" style={{ flex: '0 0 30px', height: 30, borderRadius: 'var(--radius-round)', background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 'var(--weight-semibold)', fontSize: 'var(--t-13)' }}>
              {(name || '?').charAt(0).toUpperCase()}
            </span>
            <Link to="/profile" style={{ flex: 1, minWidth: 0, color: 'var(--text)', fontSize: 'var(--t-13)', fontWeight: 'var(--weight-medium)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {name || 'Your profile'}
            </Link>
            <IconButton onClick={look.cycle} title={appearanceTitle(look.mode)}>{MODE_ICON[look.mode]}</IconButton>
          </div>
          <a href="/classic" style={{ fontSize: 'var(--t-12)', color: 'var(--muted)' }}>Classic interface ↗</a>
        </div>
      </aside>
      <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <Outlet />
      </main>
    </div>
  )
}
