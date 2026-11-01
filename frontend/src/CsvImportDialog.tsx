import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { ArrowDownLeft, FileUp, Pencil, Plus, Search, Trash2, X } from 'lucide-react'

type Account = { id: number; name: string }
type TemplateDefinition = {
  schema_version: 1
  delimiter: string
  quotechar: string
  encoding: 'utf-8' | 'utf-8-sig'
  header_row: boolean
  skip_rows: number
  field_map: Record<string, string>
  date_format: string | null
  time_format: string | null
  decimal_separator: '.' | ','
  thousands_separator: string | null
  sign_handling: Record<string, 'preserve' | 'absolute' | 'reject_negative'>
  sign_handling_by_type: Record<string, Partial<Record<'cash_amount' | 'fees' | 'taxes', 'preserve' | 'absolute' | 'reject_negative'>>>
  type_map: Record<string, string>
  trade_value: null | { mode: 'principal' | 'buy_debit' | 'sell_proceeds'; includes_fees: boolean; includes_taxes: boolean }
}
type ImportTemplate = { id: number; name: string; definition: TemplateDefinition; schema_version: number; created_at: string; updated_at: string }
type CsvPreview = {
  id: string; filename: string; total_rows: number; valid_rows: number; invalid_rows: number
  rows: Array<{
    line: number; valid: boolean; errors: string[]; account_name?: string; transaction_date?: string
    type?: string; symbol?: string | null; isin?: string | null; security_name?: string; quantity?: string; price?: string
    currency?: string; fees?: string; taxes?: string; cash_amount?: string | null
  }>
}
type SecuritySuggestion = { symbol: string; name: string; exchange: string; quote_type: string; isin: string | null; currency: string | null }
type Props = {
  accounts: Account[]
  onClose: () => void
  onImported: (message: string) => Promise<void>
}

const pageBase = window.location.pathname.endsWith('/') ? window.location.pathname : `${window.location.pathname}/`
const apiBase = new URL('api/', `${window.location.origin}${pageBase}`)
const transactionTypes = ['buy', 'sell', 'dividend', 'interest', 'fee', 'split', 'deposit', 'withdrawal', 'transfer_in', 'transfer_out']
const fieldOptions = [
  ['date', 'Date / combined date-time'], ['time', 'Separate time'], ['account', 'Account'], ['type', 'Transaction type'], ['symbol', 'Symbol'],
  ['isin', 'ISIN'], ['quantity', 'Quantity / split ratio'], ['price', 'Unit price'], ['gross_value', 'Gross trade value'],
  ['currency', 'Currency'], ['fees', 'Fees'], ['taxes', 'Taxes'], ['cash_amount', 'Cash amount'], ['notes', 'Notes'], ['security_name', 'Security name'],
] as const
const baseDefinition: TemplateDefinition = {
  schema_version: 1, delimiter: ',', quotechar: '"', encoding: 'utf-8-sig', header_row: true, skip_rows: 0,
  field_map: {
    date: 'date', account: 'account', type: 'type', symbol: 'symbol', isin: 'isin', quantity: 'quantity',
    price: 'price', currency: 'currency', fees: 'fees', taxes: 'taxes', cash_amount: 'cash_amount', notes: 'notes', security_name: 'security_name',
  },
  date_format: null, time_format: null, decimal_separator: '.', thousands_separator: null, sign_handling: {}, sign_handling_by_type: {}, type_map: {}, trade_value: null,
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(new URL(path, apiBase), init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((item: { msg: string }) => item.msg).join(', ') : body.detail
    throw new Error(detail || response.statusText || 'Request failed')
  }
  return body as T
}

function csvFirstRow(value: string, delimiter: string): string[] {
  const result: string[] = []
  let field = ''
  let quoted = false
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index]
    if (char === '"') {
      if (quoted && value[index + 1] === '"') { field += '"'; index += 1 }
      else quoted = !quoted
    } else if (char === delimiter && !quoted) {
      result.push(field.trim()); field = ''
    } else if ((char === '\n' || char === '\r') && !quoted) {
      break
    } else field += char
  }
  result.push(field.trim())
  if (result[0]?.startsWith('\uFEFF')) result[0] = result[0].slice(1)
  return result
}

function csvColumnLabels(sample: string, delimiter: string, headerRow: boolean, skipRows: number): string[] {
  const firstContent = sample.split(/\r?\n/).slice(skipRows).find((line) => line.trim()) ?? ''
  const columns = csvFirstRow(firstContent, delimiter)
  return headerRow ? columns : columns.map((_, index) => `Column ${index + 1}`)
}

function serializeTypeMap(value: string): Record<string, string> {
  return Object.fromEntries(value.split('\n').map((line) => line.split('=')).filter((parts) => parts.length >= 2)
    .map(([source, canonical]) => [source.trim(), canonical.trim().toLowerCase()]).filter(([source, canonical]) => source && transactionTypes.includes(canonical)))
}

function typeMapText(typeMap: Record<string, string>) {
  return Object.entries(typeMap).map(([source, canonical]) => `${source} = ${canonical}`).join('\n')
}

function displayDate(value: string | undefined) {
  if (!value) return '—'
  const parsed = value.length === 10
    ? new Date(`${value}T12:00:00`)
    : new Date(value)
  return parsed.toLocaleString(undefined, value.length === 10
    ? { dateStyle: 'short' }
    : { dateStyle: 'short', timeStyle: 'short' })
}

function ImportSymbolPicker({ row, onSelect, disabled }: {
  row: CsvPreview['rows'][number]
  onSelect: (suggestion: SecuritySuggestion) => Promise<void>
  disabled: boolean
}) {
  const [query, setQuery] = useState(row.isin || row.security_name || '')
  const [results, setResults] = useState<SecuritySuggestion[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const canResolve = ['buy', 'sell', 'split', 'transfer_in', 'transfer_out'].includes(row.type ?? '')
    || Boolean(row.isin)
    || (row.type === 'dividend' && Boolean(row.security_name))

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (query.trim().length < 2) return
    setSearching(true)
    setError('')
    setOpen(true)
    try {
      setResults(await api<SecuritySuggestion[]>(`securities/search?q=${encodeURIComponent(query.trim())}`))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Security search failed')
      setResults([])
    } finally {
      setSearching(false)
    }
  }

  if (!canResolve && !row.symbol) return <span>—</span>
  if (row.symbol && !open) return <div className="import-symbol-selected"><strong>{row.symbol}</strong><button type="button" onClick={() => setOpen(true)} disabled={disabled}>Change</button></div>
  return <div className="import-symbol-picker">
    {row.security_name && <small>{row.security_name}</small>}
    <form className="import-symbol-search" onSubmit={(event) => { void search(event) }}>
      <input aria-label={`Search symbol for row ${row.line}`} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search ISIN or name" />
      <button className="button button-subtle" type="submit" disabled={disabled || searching || query.trim().length < 2}><Search size={13} /> Search</button>
    </form>
    {searching && <span className="security-search-status">Searching securities…</span>}
    {error && <span className="error-label">{error}</span>}
    {open && !searching && results.length === 0 && !error && <span className="security-search-status">No matching symbols found.</span>}
    {results.length > 0 && <div className="import-symbol-results" role="listbox" aria-label={`Symbol results for row ${row.line}`}>
      {results.map((suggestion) => {
        const isinMismatch = Boolean(row.isin && suggestion.isin && suggestion.isin.toUpperCase() !== row.isin.toUpperCase())
        return <button key={suggestion.symbol} type="button" role="option" aria-disabled={isinMismatch} disabled={disabled || isinMismatch} onClick={() => { void onSelect(suggestion).then(() => setOpen(false)).catch(() => {}) }}>
          <strong>{suggestion.symbol}</strong><span>{suggestion.name}{suggestion.exchange ? ` · ${suggestion.exchange}` : ''}</span>
          {isinMismatch && <small>Different ISIN</small>}
        </button>
      })}
    </div>}
    {row.symbol && <button type="button" className="import-symbol-cancel" onClick={() => setOpen(false)}>Keep {row.symbol}</button>}
  </div>
}

export default function CsvImportDialog({ accounts, onClose, onImported }: Props) {
  const [templates, setTemplates] = useState<ImportTemplate[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState('')
  const [mode, setMode] = useState<'select' | 'configure'>('select')
  const [definition, setDefinition] = useState<TemplateDefinition>(baseDefinition)
  const [typeMapInput, setTypeMapInput] = useState('')
  const [templateName, setTemplateName] = useState('')
  const [editingId, setEditingId] = useState<number | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [headers, setHeaders] = useState<string[]>([])
  const [accountOverride, setAccountOverride] = useState('')
  const [preview, setPreview] = useState<CsvPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!file) {
      setHeaders([])
      return
    }
    let active = true
    file.slice(0, 16384).text().then((sample) => {
      if (active) setHeaders(csvColumnLabels(sample, definition.delimiter, definition.header_row, definition.skip_rows))
    }).catch(() => {
      if (active) setHeaders([])
    })
    return () => { active = false }
  }, [file, definition.delimiter, definition.header_row, definition.skip_rows])

  useEffect(() => {
    api<ImportTemplate[]>('imports/templates').then(setTemplates).catch((requestError) => {
      setError(requestError instanceof Error ? requestError.message : 'Could not load import templates')
    })
  }, [])

  const selectedTemplate = useMemo(() => templates.find((template) => String(template.id) === selectedTemplateId), [templates, selectedTemplateId])

  function startNewTemplate() {
    setDefinition({ ...baseDefinition, field_map: { ...baseDefinition.field_map }, sign_handling: {}, sign_handling_by_type: {}, type_map: {} })
    setTypeMapInput('')
    setTemplateName('')
    setEditingId(null)
    setSelectedTemplateId('')
    setMode('configure')
    setPreview(null)
    setError('')
  }

  function editTemplate(template: ImportTemplate) {
    setDefinition(structuredClone(template.definition))
    setTypeMapInput(typeMapText(template.definition.type_map))
    setTemplateName(template.name)
    setEditingId(template.id)
    setSelectedTemplateId(String(template.id))
    setMode('configure')
    setPreview(null)
    setError('')
  }

  function setFieldMap(field: string, source: string) {
    setDefinition((current) => {
      const next = { ...current, field_map: { ...current.field_map } }
      if (!source) delete next.field_map[field]
      else next.field_map[field] = source
      if (field === 'time' && !source) next.time_format = null
      return next
    })
  }

  function setTypeSignHandling(type: string, field: 'cash_amount' | 'fees' | 'taxes', value: '' | 'preserve' | 'absolute' | 'reject_negative') {
    setDefinition((current) => {
      const fieldRules = { ...(current.sign_handling_by_type[type] ?? {}) }
      if (value) fieldRules[field] = value
      else delete fieldRules[field]
      const byType = { ...current.sign_handling_by_type }
      if (Object.keys(fieldRules).length) byType[type] = fieldRules
      else delete byType[type]
      return { ...current, sign_handling_by_type: byType }
    })
  }

  async function chooseFile(value: File | null) {
    setFile(value)
    setPreview(null)
  }

  function makeDefinition(): TemplateDefinition {
    return { ...definition, type_map: serializeTypeMap(typeMapInput) }
  }

  async function saveTemplate() {
    setBusy(true)
    setError('')
    try {
      const payload = JSON.stringify({ name: templateName, definition: makeDefinition() })
      const saved = await api<ImportTemplate>(editingId ? `imports/templates/${editingId}` : 'imports/templates', {
        method: editingId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: payload,
      })
      setTemplates((current) => [...current.filter((template) => template.id !== saved.id), saved].sort((a, b) => a.name.localeCompare(b.name)))
      setSelectedTemplateId(String(saved.id))
      setDefinition(saved.definition)
      setTypeMapInput(typeMapText(saved.definition.type_map))
      setEditingId(null)
      setMode('select')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not save template')
    } finally {
      setBusy(false)
    }
  }

  async function duplicateTemplate(template: ImportTemplate) {
    const name = window.prompt('Name for the copied template', `Copy of ${template.name}`)
    if (!name?.trim()) return
    setBusy(true)
    setError('')
    try {
      const saved = await api<ImportTemplate>('imports/templates', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), definition: template.definition }),
      })
      setTemplates((current) => [...current, saved].sort((a, b) => a.name.localeCompare(b.name)))
      setSelectedTemplateId(String(saved.id))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not duplicate template')
    } finally {
      setBusy(false)
    }
  }

  async function deleteTemplate(template: ImportTemplate) {
    if (!window.confirm(`Delete the “${template.name}” import template? Existing imports will remain unchanged.`)) return
    setBusy(true)
    setError('')
    try {
      await api<void>(`imports/templates/${template.id}`, { method: 'DELETE' })
      setTemplates((current) => current.filter((item) => item.id !== template.id))
      if (selectedTemplateId === String(template.id)) setSelectedTemplateId('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not delete template')
    } finally {
      setBusy(false)
    }
  }

  async function previewCsv(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file) { setError('Choose a CSV file first.'); return }
    setBusy(true)
    setError('')
    try {
      const form = new FormData()
      form.set('file', file)
      if (accountOverride) form.set('account_id', accountOverride)
      if (mode === 'configure') form.set('template_definition', JSON.stringify(makeDefinition()))
      else if (selectedTemplateId) form.set('template_id', selectedTemplateId)
      setPreview(await api<CsvPreview>('imports/csv/preview', { method: 'POST', body: form }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'CSV preview failed')
    } finally {
      setBusy(false)
    }
  }

  async function commitImport() {
    if (!preview) return
    setBusy(true)
    setError('')
    try {
      const result = await api<{ imported_count: number; skipped_count: number }>(`imports/${preview.id}/commit`, { method: 'POST' })
      await onImported(`Imported ${result.imported_count} transactions; skipped ${result.skipped_count}.`)
      onClose()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'CSV import failed')
    } finally {
      setBusy(false)
    }
  }

  async function selectSymbol(row: CsvPreview['rows'][number], suggestion: SecuritySuggestion) {
    if (!preview) return
    setBusy(true)
    setError('')
    try {
      const updated = await api<CsvPreview>(`imports/${preview.id}/rows/${row.line}/symbol`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: suggestion.symbol, security_name: suggestion.name }),
      })
      setPreview(updated)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not set security symbol')
      throw requestError
    } finally {
      setBusy(false)
    }
  }

  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section className={`modal import-modal${preview ? ' modal-wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="import-title">
      <div className="modal-heading">
        <div><span className="modal-kicker">LEDGER IMPORT</span><h2 id="import-title">Import transactions</h2></div>
        <button className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button>
      </div>
      {error && <div className="inline-warning import-error" role="alert">{error}</div>}
      <div className="import-file-control"><label>CSV file<input type="file" accept=".csv,text/csv" required onChange={(event) => { void chooseFile(event.currentTarget.files?.[0] ?? null) }} /></label></div>
      {!preview ? <form className="modal-form import-form" onSubmit={previewCsv}>
        {accounts.length === 0 && <div className="inline-warning">Create an account before importing transactions.</div>}
        <label>Account mapping override <select value={accountOverride} onChange={(event) => setAccountOverride(event.target.value)}><option value="">Use account column</option>{accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label>
        <div className="import-format-row">
          <label>Format <select value={mode === 'configure' ? '__configure__' : selectedTemplateId} onChange={(event) => {
            if (event.target.value === '__configure__') startNewTemplate()
            else { setMode('select'); setSelectedTemplateId(event.target.value); setPreview(null) }
          }}><option value="">Canonical CSV</option>{templates.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}<option value="__configure__">Configure a format…</option></select></label>
          {selectedTemplate && <div className="template-actions"><button type="button" className="button button-subtle" onClick={() => editTemplate(selectedTemplate)} disabled={busy}><Pencil size={13} /> Edit</button><button type="button" className="button button-subtle" onClick={() => { void duplicateTemplate(selectedTemplate) }} disabled={busy}>Duplicate</button><button type="button" className="icon-button" aria-label={`Delete ${selectedTemplate.name}`} onClick={() => { void deleteTemplate(selectedTemplate) }} disabled={busy}><Trash2 size={15} /></button></div>}
        </div>

        {mode === 'configure' && <div className="template-editor">
          <h3>{editingId ? 'Edit format' : 'Configure format'}</h3>
          <label>Template name<input value={templateName} maxLength={80} onChange={(event) => setTemplateName(event.target.value)} placeholder="e.g. Example Broker Activity" /></label>
          <div className="import-settings-grid">
            <label>Delimiter<input maxLength={1} value={definition.delimiter} onChange={(event) => setDefinition({ ...definition, delimiter: event.target.value })} /></label>
            <label>Quote character<input maxLength={1} value={definition.quotechar} onChange={(event) => setDefinition({ ...definition, quotechar: event.target.value })} /></label>
            <label>Header row<select value={String(definition.header_row)} onChange={(event) => setDefinition({ ...definition, header_row: event.target.value === 'true' })}><option value="true">First row contains headers</option><option value="false">No header row</option></select></label>
            <label>Skip leading rows<input type="number" min={0} max={100} value={definition.skip_rows} onChange={(event) => setDefinition({ ...definition, skip_rows: Number(event.target.value) })} /></label>
            <label>Date or combined date-time format<input value={definition.date_format ?? ''} onChange={(event) => setDefinition({ ...definition, date_format: event.target.value || null })} placeholder="%m/%d/%Y or %m/%d/%Y %H:%M" /></label>
            <label>Separate time format<input value={definition.time_format ?? ''} onChange={(event) => setDefinition({ ...definition, time_format: event.target.value || null })} placeholder="%I:%M:%S %p" /></label>
            <label>Decimal separator<select value={definition.decimal_separator} onChange={(event) => setDefinition({ ...definition, decimal_separator: event.target.value as '.' | ',' })}><option value=".">Period (1,234.56)</option><option value=",">Comma (1.234,56)</option></select></label>
            <label>Thousands separator<select value={definition.thousands_separator ?? ''} onChange={(event) => setDefinition({ ...definition, thousands_separator: event.target.value || null })}><option value="">None</option><option value=",">Comma</option><option value=".">Period</option><option value=" ">Space</option><option value="'">Apostrophe</option></select></label>
          </div>
          <div className="field-map-list"><strong>Map CSV columns</strong>{fieldOptions.map(([field, label]) => <label key={field}>{label}<select value={definition.field_map[field] ?? ''} onChange={(event) => setFieldMap(field, event.target.value)}><option value="">Not mapped</option>{headers.map((header, index) => <option key={`${index}-${header}`} value={header}>{header || `Column ${index + 1}`}</option>)}</select></label>)}</div>
          <p className="form-help">Map a combined date-time column to Date, or map date and time separately. Set a time format only when using a separate time column. Formats use Python directives, such as %d/%m/%Y and %H:%M:%S.</p>
          <label>Transaction type labels <textarea rows={3} value={typeMapInput} onChange={(event) => setTypeMapInput(event.target.value)} placeholder={'Optional; one mapping per line\nPurchase = buy\nSale = sell'} /><span className="form-help">Use one source label = canonical type per line. Leave blank when the CSV already uses canonical type names.</span></label>
          <div className="trade-value-options">
            <label>Trade value field<select value={definition.trade_value ? 'gross_value' : 'price'} onChange={(event) => {
              const nextMap = { ...definition.field_map }
              if (event.target.value === 'gross_value') { delete nextMap.price; nextMap.gross_value = 'gross_value' }
              else { delete nextMap.gross_value; nextMap.price = 'price' }
              setDefinition({ ...definition, field_map: nextMap, trade_value: event.target.value === 'gross_value' ? { mode: 'principal', includes_fees: false, includes_taxes: false } : null })
            }}><option value="price">Source has unit price</option><option value="gross_value">Source has gross trade value</option></select></label>
            {definition.trade_value && <>
              <label>Gross value represents<select value={definition.trade_value.mode} onChange={(event) => setDefinition({ ...definition, trade_value: { ...definition.trade_value!, mode: event.target.value as NonNullable<TemplateDefinition['trade_value']>['mode'] } })}><option value="principal">Trade principal before fees/taxes</option><option value="buy_debit">Buy cash debit</option><option value="sell_proceeds">Net sell proceeds</option></select></label>
              <label className="check-option"><input type="checkbox" disabled={definition.trade_value.mode === 'principal'} checked={definition.trade_value.includes_fees} onChange={(event) => setDefinition({ ...definition, trade_value: { ...definition.trade_value!, includes_fees: event.target.checked } })} /> Gross value includes fees</label>
              <label className="check-option"><input type="checkbox" disabled={definition.trade_value.mode === 'principal'} checked={definition.trade_value.includes_taxes} onChange={(event) => setDefinition({ ...definition, trade_value: { ...definition.trade_value!, includes_taxes: event.target.checked } })} /> Gross value includes taxes</label>
            </>}
          </div>
          <div className="sign-options"><strong>Default negative number handling</strong>{(['quantity', 'price', 'gross_value'] as const).map((field) => <label key={field}>{field.replace('_', ' ')}<select value={definition.sign_handling[field] ?? 'reject_negative'} onChange={(event) => setDefinition({ ...definition, sign_handling: { ...definition.sign_handling, [field]: event.target.value as 'preserve' | 'absolute' | 'reject_negative' } })}><option value="reject_negative">Reject negative values</option><option value="absolute">Use absolute value</option><option value="preserve">Preserve sign</option></select></label>)}</div>
          <details className="type-sign-rules"><summary>Cash amount, fee, and tax sign rules by transaction type</summary><p className="form-help">Defaults convert negative withdrawals and standalone fees to positive magnitudes, preserve signed distributions, and preserve negative fees or taxes as refunds. Set an override only when the source uses a different convention.</p><div className="type-sign-grid"><div className="type-sign-header"><strong>Type</strong><strong>Cash amount</strong><strong>Fees</strong><strong>Taxes</strong></div>{transactionTypes.map((type) => <div className="type-sign-row" key={type}><strong>{type}</strong>{(['cash_amount', 'fees', 'taxes'] as const).map((field) => <label key={field} aria-label={`${type} ${field} sign rule`}><select value={definition.sign_handling_by_type[type]?.[field] ?? ''} onChange={(event) => setTypeSignHandling(type, field, event.target.value as '' | 'preserve' | 'absolute' | 'reject_negative')}><option value="">Use type default</option><option value="reject_negative">Reject negative</option><option value="absolute">Absolute value</option><option value="preserve">Preserve sign</option></select></label>)}</div>)}</div></details>
          <div className="template-save-row"><button type="button" className="button button-subtle" onClick={() => { setMode('select'); setEditingId(null) }}>Cancel format setup</button><button type="button" className="button button-primary" onClick={() => { void saveTemplate() }} disabled={busy || !templateName.trim()}><Plus size={14} /> {busy ? 'Saving…' : editingId ? 'Save changes' : 'Save template'}</button></div>
        </div>}

        <div className="modal-actions"><button type="button" className="button button-subtle" onClick={onClose}>Cancel</button><button className="button button-primary" type="submit" disabled={busy || accounts.length === 0 || !file}><FileUp size={16} /> {busy ? 'Checking file…' : 'Preview import'}</button></div>
      </form> : <div className="preview-content">
        <div className="preview-summary"><div><strong>{preview.filename}</strong><span>{preview.total_rows} rows</span></div><div className="preview-counts"><span className="count-valid">{preview.valid_rows} ready</span><span className="count-invalid">{preview.invalid_rows} skipped</span></div></div>
        <div className="preview-table-wrap"><table><thead><tr><th>LINE</th><th>DATE</th><th>ACCOUNT</th><th>TYPE</th><th>ISIN / DESCRIPTION</th><th>SYMBOL</th><th>QUANTITY</th><th>UNIT PRICE</th><th>FEES</th><th>TAXES</th><th>CASH</th><th>CHECK</th></tr></thead><tbody>{preview.rows.map((row) => <tr key={row.line} className={row.valid ? '' : 'invalid-row'}><td>{row.line}</td><td>{displayDate(row.transaction_date)}</td><td>{row.account_name ?? '—'}</td><td>{row.type ?? '—'}</td><td>{row.isin ?? row.security_name ?? '—'}</td><td><ImportSymbolPicker row={row} onSelect={(suggestion) => selectSymbol(row, suggestion)} disabled={busy} /></td><td>{row.quantity ?? '—'}</td><td>{row.price ?? '—'}</td><td>{row.fees ?? '—'}</td><td>{row.taxes ?? '—'}</td><td>{row.cash_amount ?? '—'}</td><td>{row.valid ? <span className="valid-label">Ready</span> : <span className="error-label">{row.errors.join('; ')}</span>}</td></tr>)}</tbody></table></div>
        <div className="modal-actions"><button className="button button-subtle" onClick={() => setPreview(null)}>Back to settings</button><button className="button button-primary" onClick={commitImport} disabled={busy || preview.valid_rows === 0}><ArrowDownLeft size={16} /> {busy ? 'Importing…' : `Import ${preview.valid_rows} rows`}</button></div>
      </div>}
    </section>
  </div>
}
