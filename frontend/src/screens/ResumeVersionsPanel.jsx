import React, { useState, useEffect, useCallback } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import api from '../api'
import { Button, Card, Helper, Label, Notice, Tag } from '../ui'

// Fact-based résumé versions on the Résumés screen. The base version is your
// verified facts rendered verbatim through the LaTeX template; every tailored
// version diffs against it. Accepted versions are frozen and are what autofill uploads.
const errMsg = (e, fb) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fb)

export default function ResumeVersionsPanel({ pushToast }) {
  const [rows, setRows] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => api.get('/resume-versions').then(({ data }) => setRows(data)).catch(() => setRows([])), [])
  useEffect(() => { load() }, [load])

  const act = async (fn, ok) => {
    setBusy(true)
    try { await fn(); pushToast?.({ kind: 'success', msg: ok }); await load() } catch (e) { pushToast?.({ kind: 'error', msg: errMsg(e, 'Request failed') }) } finally { setBusy(false) }
  }
  const base = (rows || []).filter((r) => r.kind === 'base')
  const acceptedBase = base.find((r) => r.status === 'accepted')
  const tailored = (rows || []).filter((r) => r.kind === 'tailored' && r.status !== 'rejected').slice(0, 12)

  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>Fact-based résumés (LaTeX)</span>
        <Helper style={{ flex: 1 }}>Built only from verified <RouterLink to="/profile">Profile</RouterLink> facts, with every claim traceable.</Helper>
        <Button size="sm" busy={busy} onClick={() => act(() => api.post('/resume-versions/base'), 'Base résumé drafted — review and accept it')}>Generate base résumé</Button>
      </div>
      {!acceptedBase && <Notice tone="quiet"><Helper>No accepted base yet. Tailored résumés diff against the accepted base, so accept one first.</Helper></Notice>}
      {base.filter((r) => r.status !== 'rejected').slice(0, 4).map((r) => (
        <div key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
          <Tag tone={r.status === 'accepted' ? 'good' : 'accent'}>{r.status}</Tag>
          <span>Base · {new Date(r.created_at).toLocaleString()}</span>
          <Helper>{r.pages ? `${r.pages} page${r.pages === 1 ? '' : 's'}` : r.blocked ? 'blocked' : 'no PDF'}{r.parser_health != null ? ` · Parser Health ${r.parser_health}` : ''}</Helper>
          <span style={{ flex: 1 }} />
          {r.pages ? <Button size="sm" variant="secondary" href={`/api/resume-versions/${r.id}/pdf`} target="_blank">PDF ↗</Button> : null}
          <Button size="sm" variant="secondary" href={`/api/resume-versions/${r.id}/export.zip`}>Overleaf ZIP</Button>
          {r.status === 'draft' && <>
            <Button size="sm" variant="secondary" busy={busy} onClick={() => act(() => api.post(`/resume-versions/${r.id}/reject`), 'Draft discarded')}>Reject</Button>
            <Button size="sm" busy={busy} onClick={() => act(() => api.post(`/resume-versions/${r.id}/accept`, { all: true }), 'Base résumé accepted')}>Accept</Button>
          </>}
        </div>
      ))}
      {tailored.length > 0 && <Label style={{ marginTop: 4 }}>Tailored versions</Label>}
      {tailored.map((r) => (
        <div key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
          <Tag tone={r.status === 'accepted' ? 'good' : r.blocked ? 'bad' : 'accent'}>{r.blocked ? 'blocked' : r.status}</Tag>
          <RouterLink to={`/jobs/${r.job_id}`}>{r.title || 'Role'} — {r.company || '?'}</RouterLink>
          <Helper>{r.match_score != null ? `Role Match ${r.match_score}` : ''}{r.parser_health != null ? ` · Parser Health ${r.parser_health}` : ''}</Helper>
        </div>
      ))}
    </Card>
  )
}
