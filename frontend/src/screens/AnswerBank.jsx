import React, { useState, useEffect, useCallback } from 'react'
import api from '../api'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import { useTitle } from '../useTitle'
import { Button, Card, DashedAdd, HeaderRow, Helper, Input, Label, PageTitle, Pill, Switch, Tag, Textarea } from '../ui'

// Saved application answers. Equivalent wordings of one question are stored once
// (the other wordings become aliases), matched by intent. Protected intents —
// work authorization, sponsorship, EEO, salary, legal attestations — are never
// drafted by AI and are only reused when you have verified the answer.
const errMsg = (e, fb) => (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : fb)

function EntryCard({ e, onSave, onDelete }) {
  const [q, setQ] = useState(e.question)
  const [a, setA] = useState(e.answer)
  const dirty = q !== e.question || a !== e.answer
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        {e.intent ? <Tag tone="accent" title="Normalized intent used to match other wordings">{e.intent.replace(/_/g, ' ')}</Tag> : <Tag>free text</Tag>}
        {e.protected && <Tag tone="warn" title="Never drafted by AI; used only exactly as you saved and verified it">protected</Tag>}
        <Helper size="xs">{e.answer_type}{e.last_used ? ` · last used ${new Date(e.last_used).toLocaleDateString()}` : ' · never used'}</Helper>
        <span style={{ flex: 1 }} />
        <Switch size="sm" on={e.user_verified} label="verified" onChange={(v) => onSave(e.id, { user_verified: v })} />
        <Button size="sm" variant="secondary" onClick={() => onDelete(e)}>Delete</Button>
      </div>
      <Label>Question (original wording)</Label>
      <Input value={q} onChange={setQ} />
      {e.aliases.length > 0 && (
        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', alignItems: 'center' }}>
          <Label>Also matches</Label>
          {e.aliases.map((al) => (
            <Pill key={al} size="sm" title="Remove this wording" onClick={() => onSave(e.id, { aliases: e.aliases.filter((x) => x !== al) })}>{al} ✕</Pill>
          ))}
        </div>
      )}
      <Label>Answer</Label>
      <Textarea rows={2} value={a} onChange={setA} />
      {dirty && <div style={{ display: 'flex', justifyContent: 'flex-end' }}><Button size="sm" onClick={() => onSave(e.id, { question: q, answer: a })}>Save</Button></div>}
    </Card>
  )
}

export default function AnswerBank() {
  useTitle('Answer Bank')
  const [rows, setRows] = useState(null)
  const [adding, setAdding] = useState(false)
  const [nq, setNq] = useState('')
  const [na, setNa] = useState('')
  const [filter, setFilter] = useState('')
  const { toasts, push: pushToast, dismiss } = useToasts()

  const load = useCallback(() => api.get('/persona/answer-bank').then(({ data }) => setRows(data.entries))
    .catch((e) => pushToast({ kind: 'error', msg: errMsg(e, 'Could not load answers') })), [pushToast])
  useEffect(() => { load() }, [load])

  const onSave = async (id, patch) => {
    try { const { data } = await api.put(`/persona/answer-bank/${id}`, patch); setRows(data.entries) } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not save') }) }
  }
  const onDelete = async (e) => {
    if (!window.confirm(`Delete the saved answer to “${e.question}”?`)) return
    try { const { data } = await api.delete(`/persona/answer-bank/${e.id}`); setRows(data.entries) } catch (err) { pushToast({ kind: 'error', msg: errMsg(err, 'Could not delete') }) }
  }
  const add = async () => {
    try {
      const before = rows?.length || 0
      const { data } = await api.post('/persona/answer-bank', { question: nq, answer: na })
      setRows(data.entries)
      pushToast({ kind: 'success', msg: data.entries.length === before ? 'Merged into an existing answer for the same question' : 'Answer saved' })
      setNq(''); setNa(''); setAdding(false)
    } catch (e) { pushToast({ kind: 'error', msg: errMsg(e, 'Could not save') }) }
  }
  const shown = (rows || []).filter((e) => !filter || `${e.question} ${e.aliases.join(' ')} ${e.answer} ${e.intent || ''}`.toLowerCase().includes(filter.toLowerCase()))

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 12 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <PageTitle>Answer Bank</PageTitle>
          <Helper>{rows ? `${rows.length} saved answers · ${rows.filter((e) => e.protected).length} protected` : ' '}</Helper>
        </div>
        <span style={{ flex: 1 }} />
        <Input value={filter} onChange={setFilter} placeholder="Filter…" style={{ width: 220 }} />
      </HeaderRow>
      <div className="v2-scroll" style={{ flex: 1, overflow: 'auto', padding: '16px 26px 30px', display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 900 }}>
        <Helper>Autofill reuses these answers on application forms and highlights every reused answer for your review. It never submits a form.</Helper>
        {adding ? (
          <Card style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 12 }}>
            <Label>Question</Label><Input value={nq} onChange={setNq} placeholder="As the form asks it" />
            <Label>Answer</Label><Textarea rows={2} value={na} onChange={setNa} />
            <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
              <Button size="sm" variant="secondary" onClick={() => setAdding(false)}>Cancel</Button>
              <Button size="sm" disabled={!nq.trim() || !na.trim()} onClick={add}>Save answer</Button>
            </div>
          </Card>
        ) : <DashedAdd big onClick={() => setAdding(true)}>+ Add answer</DashedAdd>}
        {shown.map((e) => <EntryCard key={`${e.id}:${e.question}:${e.answer}`} e={e} onSave={onSave} onDelete={onDelete} />)}
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
