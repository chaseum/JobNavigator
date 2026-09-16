import { useCallback, useEffect, useRef, useState } from 'react'
import api from './api'

// Job Preferences: one document, two editors.
//
// The Jobs toolbar and the Settings page both write here, so they cannot drift
// apart — there is no second copy to keep in sync. Everything below is criteria
// the user states. Nothing in this file knows what a job board is.

export const PREFS_EVENT = 'jn:prefs-changed'

export const EMPTY_PREFS = {
  countries: ['US'],
  locations: [],
  job_functions: [],
  levels: [],
  job_types: [],
  work_models: [],
  date_posted_days: 7,
  max_years_experience: null,
  companies: [],
  excluded_companies: [],
  minimum_salary: null,
  sponsorship: 'any',
  industries: [],
  title_query: '',
}

export const DATE_OPTS = [['1', 'Past 24 hours'], ['3', 'Past 3 days'], ['7', 'Past 7 days'], ['30', 'Past 30 days']]
export const YEARS_OPTS = [['0', '0 years'], ['1', '0–1 years'], ['2', '0–2 years'], ['3', '0–3 years'], ['5', '3–5 years'], ['', '5+ / any']]
export const SPONSORSHIP_OPTS = [['any', 'Any employer'], ['required', 'Sponsors visas']]
// Countries the planner knows a board-facing name for; anything else still works
// as a raw code, but these are the ones worth offering in a menu.
export const COUNTRY_OPTS = [
  ['US', 'United States'], ['CA', 'Canada'], ['GB', 'United Kingdom'], ['DE', 'Germany'],
  ['IN', 'India'], ['AU', 'Australia'], ['IE', 'Ireland'], ['NL', 'Netherlands'], ['SG', 'Singapore'],
]

const listToText = (v) => (Array.isArray(v) ? v.join(', ') : '')
const textToList = (v) => String(v || '').split(',').map((x) => x.trim()).filter(Boolean)
export { listToText, textToList }

/**
 * Load the preferences document and write partial changes back to it.
 *
 * Writes are optimistic and debounced: clicking four levels in a menu is one
 * request, and the UI never waits on the network to show what was picked. A
 * failed PATCH rolls the local copy back to what the server last confirmed, so
 * the screen can't quietly disagree with what is actually stored.
 */
export function useJobPreferences({ onSaved } = {}) {
  const [prefs, setPrefs] = useState(null)
  const [taxonomy, setTaxonomy] = useState(null)
  const [filters, setFilters] = useState([])
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const confirmed = useRef(null)      // last document the server acknowledged
  const queued = useRef({})
  const timer = useRef(null)
  const savedCb = useRef(onSaved)
  savedCb.current = onSaved

  const load = useCallback(async () => {
    try {
      const { data } = await api.get('/job-preferences')
      confirmed.current = data.preferences
      setPrefs(data.preferences)
      setTaxonomy(data.taxonomy)
      setFilters(data.saved_filters || [])
      setError(null)
    } catch (e) {
      setError(e?.response?.status === 401 ? null : 'Could not load your job preferences')
    }
  }, [])
  useEffect(() => { load() }, [load])

  const flush = useCallback(async () => {
    const changes = queued.current
    queued.current = {}
    if (!Object.keys(changes).length) return
    setSaving(true)
    try {
      const { data } = await api.patch('/job-preferences', changes)
      confirmed.current = data.preferences
      setPrefs(data.preferences)
      setError(null)
      window.dispatchEvent(new CustomEvent(PREFS_EVENT))
      if (savedCb.current) savedCb.current(data)
    } catch (e) {
      setPrefs(confirmed.current)
      setError(e?.response?.data?.detail || 'Could not save your job preferences')
    } finally { setSaving(false) }
  }, [])

  // A pending change must survive the editor closing — otherwise the last click
  // before navigating away is silently lost.
  useEffect(() => () => { clearTimeout(timer.current); flush() }, [flush])

  const patch = useCallback((changes) => {
    setPrefs((p) => ({ ...(p || EMPTY_PREFS), ...changes }))
    queued.current = { ...queued.current, ...changes }
    clearTimeout(timer.current)
    timer.current = setTimeout(flush, 500)
  }, [flush])

  const toggle = useCallback((key, value) => {
    setPrefs((p) => {
      const cur = (p || EMPTY_PREFS)[key] || []
      const next = cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value]
      queued.current = { ...queued.current, [key]: next }
      clearTimeout(timer.current)
      timer.current = setTimeout(flush, 500)
      return { ...p, [key]: next }
    })
  }, [flush])

  const saveFilters = useCallback(async (next) => {
    const { data } = await api.put('/job-preferences/filters', { filters: next })
    setFilters(data.saved_filters || [])
    window.dispatchEvent(new CustomEvent(PREFS_EVENT))
    return data.saved_filters
  }, [])

  return { prefs, taxonomy, filters, error, saving, patch, toggle, reload: load, saveFilters, flush }
}

/** Ask the backend to refresh discovery from the current preferences. */
export async function refreshDiscovery() {
  const { data } = await api.post('/discovery/refresh')
  return data
}
