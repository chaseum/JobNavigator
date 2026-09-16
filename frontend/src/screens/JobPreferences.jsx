import React from 'react'
import {
  COUNTRY_OPTS, DATE_OPTS, SPONSORSHIP_OPTS, YEARS_OPTS, listToText, textToList,
} from '../jobPrefs'
import { Chip, Helper, Input, Label, Select, Spinner } from '../ui'
import '../theme.css'

// The Job Preferences form: what you are looking for, and nothing about how it
// is found. Rendered in Settings and in first-run onboarding; the Jobs toolbar
// edits the same document through its filter chips.

function Group({ label, hint, children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <Label>{label}</Label>
      {hint && <Helper size="xs">{hint}</Helper>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>{children}</div>
    </div>
  )
}

function Chips({ options, selected, onToggle, ariaLabel }) {
  const on = new Set(selected || [])
  return (
    <div role="group" aria-label={ariaLabel} style={{ display: 'contents' }}>
      {options.map((o) => (
        <Chip key={o.id} on={on.has(o.id)} onClick={() => onToggle(o.id)}
          ariaLabel={`${o.label}${on.has(o.id) ? ' (selected)' : ''}`}>{o.label}</Chip>
      ))}
    </div>
  )
}

/**
 * @param {object} prefs      the canonical preferences document
 * @param {object} taxonomy   ids + labels from GET /job-preferences
 * @param {function} patch    partial update, e.g. patch({ date_posted_days: 7 })
 * @param {function} toggle   toggle one id inside a list field
 * @param {boolean} compact   hide the lower-frequency fields (onboarding)
 */
export default function JobPreferencesForm({ prefs, taxonomy, patch, toggle, compact = false }) {
  if (!prefs || !taxonomy) return <Spinner />
  const T = taxonomy

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
      <Group label="Job function" hint="What kind of work you want. JobNavigator turns this into the searches it runs.">
        <Chips options={T.job_functions} selected={prefs.job_functions}
          onToggle={(v) => toggle('job_functions', v)} ariaLabel="Job functions" />
      </Group>

      <Group label="Level">
        <Chips options={T.levels} selected={prefs.levels}
          onToggle={(v) => toggle('levels', v)} ariaLabel="Employment levels" />
      </Group>

      <Group label="Job type">
        <Chips options={T.job_types} selected={prefs.job_types}
          onToggle={(v) => toggle('job_types', v)} ariaLabel="Job types" />
      </Group>

      <Group label="Work model">
        <Chips options={T.work_models} selected={prefs.work_models}
          onToggle={(v) => toggle('work_models', v)} ariaLabel="Work models" />
      </Group>

      <Group label="Country">
        <Chips options={COUNTRY_OPTS.map(([id, label]) => ({ id, label }))} selected={prefs.countries}
          onToggle={(v) => toggle('countries', v)} ariaLabel="Countries" />
      </Group>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <Label>Date posted</Label>
          <Select value={String(prefs.date_posted_days ?? 7)} options={DATE_OPTS} width="180px"
            ariaLabel="Date posted" onPick={(v) => patch({ date_posted_days: Number(v) })} />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <Label>Years of experience</Label>
          <Select value={prefs.max_years_experience == null ? '' : String(prefs.max_years_experience)}
            options={YEARS_OPTS} width="180px" ariaLabel="Maximum years of experience"
            onPick={(v) => patch({ max_years_experience: v === '' ? null : Number(v) })} />
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <Label>Locations</Label>
        <Helper size="xs">Cities or states, comma separated. Leave empty for anywhere in the countries above.</Helper>
        <Input value={listToText(prefs.locations)} width="420px" ariaLabel="Locations"
          placeholder="Seattle, WA, New York"
          onChange={(v) => patch({ locations: textToList(v) })} />
      </div>

      {!compact && (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Label>Only these companies</Label>
            <Input value={listToText(prefs.companies)} width="420px" ariaLabel="Only these companies"
              placeholder="empty = any company"
              onChange={(v) => patch({ companies: textToList(v) })} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Label>Never these companies</Label>
            <Input value={listToText(prefs.excluded_companies)} width="420px" ariaLabel="Excluded companies"
              onChange={(v) => patch({ excluded_companies: textToList(v) })} />
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <Label>Minimum salary</Label>
              <Input value={prefs.minimum_salary == null ? '' : String(prefs.minimum_salary)}
                width="180px" ariaLabel="Minimum salary" placeholder="any"
                onChange={(v) => {
                  const digits = v.replace(/[^0-9]/g, '')
                  patch({ minimum_salary: digits === '' ? null : Number(digits) })
                }} />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <Label>Sponsorship</Label>
              <Select value={prefs.sponsorship || 'any'} options={SPONSORSHIP_OPTS} width="200px"
                ariaLabel="Sponsorship" onPick={(v) => patch({ sponsorship: v })} />
            </div>
          </div>
        </>
      )}
    </div>
  )
}
