import React, { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Settings as SettingsIcon, FileUser, IdCard, Search } from 'lucide-react'
import { Button, FooterRow, GlyphBadge, Helper, ModalPanel } from './ui'
import { useTheme, themeAttrs } from './theme'
import JobPreferencesForm from './screens/JobPreferences'
import { refreshDiscovery, useJobPreferences } from './jobPrefs'
import './theme.css'

// First-run overlay. Each step is a link to the screen it names, so they
// stay clickable and hover.
const STEPS = [
  [SettingsIcon, 'Pick a model', 'Ollama runs locally and is the default; choose a model you have pulled. Other providers send your data to them.', 'settings'],
  [IdCard, 'Build your profile', 'Import a résumé, verify each fact, then add everything it leaves out. Résumés are written only from verified facts.', 'profile'],
  [FileUser, 'Accept a base résumé', 'Generate a LaTeX résumé from your facts and accept it; tailored versions are reviewed against it.', 'resumes'],
  // Not a screen to visit: the last step is stating what you want, right here.
  // From then on finding, collecting and analysing jobs is the app's problem.
  [Search, 'Say what you are looking for', 'Job function, level, location. JobNavigator searches for it, keeps the feed fresh and analyses the matches on its own.', ''],
]

// The last step, inline: the same Job Preferences document every other screen
// edits. "Start finding jobs" saves it, kicks off a discovery run and leaves
// you on the feed — no search to create, no company to activate, no Run button.
function PreferencesStep({ onDone }) {
  const { prefs, taxonomy, patch, toggle, flush } = useJobPreferences()
  const [busy, setBusy] = useState(false)
  const start = async () => {
    setBusy(true)
    try {
      await flush()
      await refreshDiscovery().catch(() => { /* the feed still fills on the next scheduled pass */ })
    } finally { onDone() }
  }
  return (
    <>
      <div style={{ padding: '22px 24px 6px', display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span className="v2-dialogtitle" style={{ fontFamily: 'var(--serif)', fontSize: 21, fontWeight: 400, letterSpacing: '-.02em', lineHeight: '26px' }}>What are you looking for?</span>
        <span style={{ fontSize: 12.5, lineHeight: '18px', color: 'var(--muted)' }}>You can change any of this later from the Jobs screen.</span>
      </div>
      <div className="v2-scroll" style={{ padding: '14px 24px 18px', maxHeight: '60vh', overflowY: 'auto' }}>
        <JobPreferencesForm prefs={prefs} taxonomy={taxonomy} patch={patch} toggle={toggle} compact />
      </div>
      <FooterRow pad="12px 24px" soft bg="page" gap="normal">
        <Button size="xs" onClick={start} disabled={busy || !prefs} style={{ marginLeft: 'auto' }}>
          {busy ? 'Starting…' : 'Start finding jobs →'}
        </Button>
      </FooterRow>
    </>
  )
}

export default function WelcomeModal({ onClose }) {
  const navigate = useNavigate()
  // land in whichever shell you're already in — this overlay is global
  const base = useLocation().pathname.startsWith('/classic') ? '/classic/' : '/'
  // mounts outside the v2 shell like the sign-in overlay, so it brings the
  // theme with it from the shared store
  const look = useTheme()
  const [phase, setPhase] = useState('steps')
  // An empty slug is the inline preferences step, not a route.
  const go = (slug) => { if (!slug) return setPhase('prefs'); onClose?.(); navigate(base + slug) }
  const finish = () => { onClose?.(); navigate(base === '/' ? '/feed' : base) }

  return (
    // Scrim carries the theme root and its own z-index (below sign-in, above
    // every in-shell modal). Scrim click and Escape both close (ModalPanel contract).
    // `escapeCapture`: this overlay mounts outside the shell, so the screen behind
    // it registers its Escape listener first — capture phase is how the topmost
    // overlay keeps the key (R4-E2E-01).
    <ModalPanel width={phase === "prefs" ? 560 : 420} title="Welcome to JobNavigator" onClose={onClose} escapeCapture zIndex={9998}
      scrimProps={{ className: 'jn-v2', ...themeAttrs(look) }}
      scrimStyle={{ padding: 16 }}
      style={{ maxWidth: '100%', overflow: 'hidden' }}>
      {phase === 'prefs' ? <PreferencesStep onDone={finish} /> : (<>
      <div style={{ padding: '22px 24px 6px', display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div style={{ display: 'flex', alignItems: 'baseline' }}>
          {/* ui: keep — the welcome title is serif 21/26px; the Heading scale is 18/19/22 */}
          <span className="v2-dialogtitle" style={{ fontFamily: 'var(--serif)', fontSize: 21, fontWeight: 400, letterSpacing: '-.02em', lineHeight: '26px' }}>Welcome to JobNavigator</span>
          {/* ui: keep — the modal's own ✕: muted 13 sitting on the title's 26px line box; IconButton is a 26px round box */}
          <span onClick={onClose} className="v2-hover-accent-text" role="button" aria-label="Close"
            style={{ marginLeft: 'auto', fontSize: 13, lineHeight: '26px', color: 'var(--muted)', cursor: 'pointer' }}>✕</span>
        </div>
        <span style={{ fontSize: 12.5, lineHeight: '18px', color: 'var(--muted)' }}>Four steps. After that, finding jobs is automatic.</span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '14px 24px 18px' }}>
        {STEPS.map(([Icon, title, desc, to], i) => (
          <div key={to} onClick={() => go(to)} className="v2-welcomestep"
            style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '9px 2px', borderRadius: 'var(--radius-cell)', cursor: 'pointer' }}>
            <GlyphBadge size={22} tone="neutral" mono line={1} style={{ flex: '0 0 auto' }}>{i + 1}</GlyphBadge>
            <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 13, lineHeight: '18px', fontWeight: 600 }}>
                {title}<Icon size={15} strokeWidth={1.8} style={{ color: 'var(--muted)', flex: '0 0 auto' }} />
              </span>
              <Helper style={{ textWrap: 'pretty' }}>{desc}</Helper>
            </div>
          </div>
        ))}
      </div>

      {/* `gap="normal"` is the initial value, not a zero: this bar holds one
          button and has never reserved a gap. */}
      <FooterRow pad="12px 24px" soft bg="page" gap="normal">
        <Button size="xs" onClick={() => go('settings')} style={{ marginLeft: 'auto' }}>Start with Settings →</Button>
      </FooterRow>
      </>)}
    </ModalPanel>
  )
}
