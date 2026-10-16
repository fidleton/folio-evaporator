import { lazy, Suspense, useEffect, useState, type FormEvent } from 'react'
import {
  Activity, ArrowDownLeft, ArrowUpRight, BriefcaseBusiness,
  ChartNoAxesCombined, Clock3, FileUp, Pencil, Plus, Settings2,
  RefreshCw, Trash2, WalletCards, X,
} from 'lucide-react'
import './folio.css'

const PortfolioChart = lazy(() => import('./PortfolioChart'))

type Account = { id: number; name: string; broker: string; base_currency: string }
type Summary = { reporting_currency: string; positions_value: string; cash_balance: string; total_value: string; unpriced_positions: number }
type HistoryRange = '1d' | '5d' | '1mo' | 'ytd' | '1y' | 'max'
type GraphMetric = 'value' | 'roi'
type HistoryPoint = { date: string; value: string; roi: string | null; roi_amount: string | null }
type PortfolioHistory = { period: HistoryRange; reporting_currency: string; points: HistoryPoint[] }
type Holding = { symbol: string; currency: string; quantity: string; price: string | null; price_currency: string | null; price_observed_at: string | null; market_value: string | null }
type Transaction = { id: number; account_id: number; account_name: string; symbol: string | null; type: string; transaction_date: string; quantity: string; price: string; currency: string; fees: string; cash_amount: string | null; notes: string }
type CsvPreview = { id: string; filename: string; total_rows: number; valid_rows: number; invalid_rows: number; rows: Array<{ line: number; valid: boolean; errors: string[]; account_name?: string; transaction_date?: string; type?: string; symbol?: string | null; quantity?: string; price?: string; currency?: string }> }

const pageBase = window.location.pathname.endsWith('/') ? window.location.pathname : `${window.location.pathname}/`
const apiBase = new URL('api/', `${window.location.origin}${pageBase}`)
const today = new Date().toISOString().slice(0, 10)
const historyRanges: Array<{ value: HistoryRange; label: string }> = [
  { value: '1d', label: '1D' }, { value: '5d', label: '1W' },
  { value: '1mo', label: '1M' }, { value: 'ytd', label: 'YTD' },
  { value: '1y', label: '1Y' }, { value: 'max', label: 'MAX' },
]

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(new URL(path, apiBase), init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((item: { msg: string }) => item.msg).join(', ') : body.detail
    throw new Error(detail || response.statusText || 'Request failed')
  }
  return body as T
}

function money(value: string | null | undefined, currency: string) {
  if (value == null || !Number.isFinite(Number(value))) return 'Not priced'
  return new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 2 }).format(Number(value))
}

async function fetchPortfolioData(selectedAccount: string) {
  const query = selectedAccount === 'all' ? '' : `?account_id=${selectedAccount}`
  const [accounts, summary, holdings, transactions] = await Promise.all([
    api<Account[]>('accounts'), api<Summary>(`portfolio/summary${query}`),
    api<Holding[]>(`portfolio/holdings${query}`), api<Transaction[]>(`transactions${query}`),
  ])
  return { accounts, summary, holdings, transactions }
}

function App() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [selectedAccount, setSelectedAccount] = useState('all')
  const [summary, setSummary] = useState<Summary | null>(null)
  const [holdings, setHoldings] = useState<Holding[]>([])
  const [transactions, setTransactions] = useState<Transaction[]>([])
  const [historyRange, setHistoryRange] = useState<HistoryRange>('1mo')
  const [graphMetric, setGraphMetric] = useState<GraphMetric>('value')
  const [portfolioHistory, setPortfolioHistory] = useState<PortfolioHistory | null>(null)
  const [loadedHistoryKey, setLoadedHistoryKey] = useState('')
  const [historyFailure, setHistoryFailure] = useState<{ key: string; message: string } | null>(null)
  const [historyRevision, setHistoryRevision] = useState(0)
  const [view, setView] = useState<'portfolio' | 'activity' | 'settings'>('portfolio')
  const [reportingCurrency, setReportingCurrency] = useState('USD')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [showAccountForm, setShowAccountForm] = useState(false)
  const [showTransactionForm, setShowTransactionForm] = useState(false)
  const [editingTransaction, setEditingTransaction] = useState<Transaction | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [preview, setPreview] = useState<CsvPreview | null>(null)
  const [importBusy, setImportBusy] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)
  const historyRequestKey = `${selectedAccount}:${historyRange}:${historyRevision}`
  const historyLoading = loadedHistoryKey !== historyRequestKey
  const historyError = historyFailure?.key === historyRequestKey ? historyFailure.message : ''
  const currentRoi = portfolioHistory?.points.at(-1)?.roi
  const currentRoiAmount = portfolioHistory?.points.at(-1)?.roi_amount

  async function loadData() {
    try {
      const data = await fetchPortfolioData(selectedAccount)
      setAccounts(data.accounts)
      setSummary(data.summary)
      setReportingCurrency(data.summary.reporting_currency)
      setHoldings(data.holdings)
      setTransactions(data.transactions)
      setError('')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not reach the portfolio service')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let active = true
    fetchPortfolioData(selectedAccount)
      .then((data) => {
        if (!active) return
        setAccounts(data.accounts)
        setSummary(data.summary)
        setReportingCurrency(data.summary.reporting_currency)
        setHoldings(data.holdings)
        setTransactions(data.transactions)
        setError('')
      })
      .catch((requestError: unknown) => {
        if (active) setError(requestError instanceof Error ? requestError.message : 'Could not reach the portfolio service')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [selectedAccount])

  useEffect(() => {
    let active = true
    const params = new URLSearchParams({ period: historyRange })
    if (selectedAccount !== 'all') params.set('account_id', selectedAccount)
    api<PortfolioHistory>(`portfolio/history?${params}`)
      .then((data) => { if (active) setPortfolioHistory(data) })
      .catch((requestError: unknown) => {
        if (active) setHistoryFailure({ key: historyRequestKey, message: requestError instanceof Error ? requestError.message : 'Could not load portfolio history' })
      })
      .finally(() => { if (active) setLoadedHistoryKey(historyRequestKey) })
    return () => { active = false }
  }, [selectedAccount, historyRange, historyRevision, historyRequestKey])

  async function refreshPrices() {
    setRefreshing(true)
    setNotice('')
    try {
      const result = await api<{ refreshed_quotes: number; errors: unknown[] }>('portfolio/refresh', { method: 'POST' })
      await loadData()
      setNotice(result.errors.length
        ? `Updated ${result.refreshed_quotes} quotes; ${result.errors.length} price or FX requests unavailable.`
        : `Updated ${result.refreshed_quotes} quotes and currency rates.`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Price refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  async function createAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    try {
      await api<Account>('accounts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: form.get('name'), broker: form.get('broker'), base_currency: form.get('currency') }),
      })
      setShowAccountForm(false)
      setNotice('Broker account added.')
      await loadData()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not add account')
    }
  }

  function openTransactionForm(transaction?: Transaction) {
    setEditingTransaction(transaction ?? null)
    setShowTransactionForm(true)
  }

  async function saveTransaction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const payload = {
      account_id: Number(form.get('account_id')),
      type: form.get('type'),
      transaction_date: form.get('transaction_date'),
      symbol: String(form.get('symbol') ?? '').trim() || null,
      quantity: form.get('quantity') || '0',
      price: form.get('price') || '0',
      currency: form.get('currency'),
      fees: form.get('fees') || '0',
      cash_amount: form.get('cash_amount') || null,
      notes: form.get('notes') || '',
    }
    try {
      await api<Transaction>(editingTransaction ? `transactions/${editingTransaction.id}` : 'transactions', {
        method: editingTransaction ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      setShowTransactionForm(false)
      setNotice(editingTransaction ? 'Transaction updated.' : 'Transaction added.')
      setHistoryRevision((revision) => revision + 1)
      await loadData()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not save transaction')
    }
  }

  async function removeTransaction(transaction: Transaction) {
    if (!window.confirm(`Remove this ${transaction.type} transaction${transaction.symbol ? ` for ${transaction.symbol}` : ''}?`)) return
    try {
      await api<void>(`transactions/${transaction.id}`, { method: 'DELETE' })
      setNotice('Transaction removed.')
      setHistoryRevision((revision) => revision + 1)
      await loadData()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not remove transaction')
    }
  }

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSavingSettings(true)
    setNotice('')
    try {
      const params = new URLSearchParams({ reporting_currency: reportingCurrency })
      const result = await api<{ reporting_currency: string }>(`settings?${params}`, { method: 'PUT' })
      setReportingCurrency(result.reporting_currency)
      setHistoryRevision((revision) => revision + 1)
      await loadData()
      setNotice(`Reporting currency saved as ${result.reporting_currency}.`)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not save settings')
    } finally {
      setSavingSettings(false)
    }
  }

  async function previewCsv(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setImportBusy(true)
    setError('')
    try {
      setPreview(await api<CsvPreview>('imports/csv/preview', { method: 'POST', body: new FormData(event.currentTarget) }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'CSV preview failed')
    } finally {
      setImportBusy(false)
    }
  }

  async function commitImport() {
    if (!preview) return
    setImportBusy(true)
    try {
      const result = await api<{ imported_count: number; skipped_count: number }>(`imports/${preview.id}/commit`, { method: 'POST' })
      setNotice(`Imported ${result.imported_count} transactions; skipped ${result.skipped_count}.`)
      setPreview(null)
      setShowImport(false)
      setHistoryRevision((revision) => revision + 1)
      await loadData()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'CSV import failed')
    } finally {
      setImportBusy(false)
    }
  }

  const accountLabel = selectedAccount === 'all' ? 'All accounts' : accounts.find((item) => String(item.id) === selectedAccount)?.name ?? 'Selected account'

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#portfolio" onClick={() => setView('portfolio')}>
        <span className="brand-mark"><ChartNoAxesCombined size={19} strokeWidth={2.5} /></span>
        <span><strong>folio</strong><small>PORTFOLIO DESK</small></span>
      </a>
      <div className="side-label">WORKSPACE</div>
      <nav className="side-nav" aria-label="Main navigation">
        <button className={view === 'portfolio' ? 'nav-item active' : 'nav-item'} onClick={() => setView('portfolio')}><BriefcaseBusiness size={17} /><span>Portfolio</span></button>
        <button className={view === 'activity' ? 'nav-item active' : 'nav-item'} onClick={() => setView('activity')}><Activity size={17} /><span>Transactions</span>{transactions.length > 0 && <span className="nav-count">{transactions.length}</span>}</button>
        <button className={view === 'settings' ? 'nav-item active' : 'nav-item'} onClick={() => setView('settings')}><Settings2 size={17} /><span>Settings</span></button>
      </nav>
      <div className="sidebar-bottom">
        <div className="provider-note"><span className="status-dot" /><span><strong>Market data</strong><small>Yahoo Finance adapter</small></span></div>
      </div>
    </aside>

    <main className="main-content">
      <header className="topbar">
        <div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><strong>{view === 'portfolio' ? 'Portfolio' : view === 'activity' ? 'Transactions' : 'Settings'}</strong></div>
        {view !== 'settings' && <div className="top-actions">
          <label className="account-select-wrap"><WalletCards size={16} /><select aria-label="Filter by broker account" value={selectedAccount} onChange={(event) => setSelectedAccount(event.target.value)}><option value="all">All accounts</option>{accounts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <button className="icon-button" title="Refresh prices" aria-label="Refresh prices" onClick={refreshPrices} disabled={refreshing}><RefreshCw size={17} className={refreshing ? 'spin' : ''} /></button>
          <button className="button button-primary" onClick={() => setShowAccountForm(true)}><Plus size={16} /> Add account</button>
        </div>}
      </header>

      <div className="page-wrap">
        <section className="page-heading"><div><div className="eyebrow">{view === 'settings' ? 'WORKSPACE / PREFERENCES' : <>{accountLabel} <span className="eyebrow-sep">/</span> OVERVIEW</>}</div><h1>{view === 'portfolio' ? 'Your portfolio' : view === 'activity' ? 'Transaction activity' : 'Settings'}</h1><p>{view === 'portfolio' ? 'A clear view of every position across your brokers.' : view === 'activity' ? 'A single ledger for activity across your accounts.' : 'Manage how portfolio values are displayed.'}</p></div>{view === 'activity' && <div className="heading-actions"><button className="button button-primary" onClick={() => openTransactionForm()} disabled={accounts.length === 0}><Plus size={16} /> Add transaction</button><button className="button button-subtle" onClick={() => { setPreview(null); setShowImport(true) }}><FileUp size={16} /> Import CSV</button></div>}</section>
        {error && <div className="alert alert-error" role="alert"><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError('')}><X size={16} /></button></div>}
        {notice && <div className="alert alert-success" role="status"><span>{notice}</span><button aria-label="Dismiss message" onClick={() => setNotice('')}><X size={16} /></button></div>}

        {view === 'portfolio' ? <>
          <section className="portfolio-chart-panel" aria-label="Portfolio value history">
            <div className="chart-header">
              <div className="chart-value-block"><div className="panel-kicker">{graphMetric === 'value' ? 'TOTAL PORTFOLIO VALUE' : 'RETURN ON INVESTMENT'}</div><div className="chart-total-value">{graphMetric === 'value' ? (summary ? money(summary.total_value, summary.reporting_currency) : '—') : (currentRoi == null ? '—' : `${Number(currentRoi).toFixed(2)}%`)}</div><div className="chart-value-meta">{graphMetric === 'value' ? (summary?.unpriced_positions ? `${summary.unpriced_positions} position${summary.unpriced_positions === 1 ? '' : 's'} awaiting prices` : `Reported in ${summary?.reporting_currency ?? 'USD'}`) : currentRoiAmount == null ? 'Record net deposits to calculate investment return' : `${money(currentRoiAmount, portfolioHistory?.reporting_currency ?? 'USD')} return against net deposits`}</div></div>
              <div className="chart-controls"><div className="metric-switch" role="group" aria-label="Graph metric"><button className={graphMetric === 'value' ? 'metric-button active' : 'metric-button'} aria-pressed={graphMetric === 'value'} onClick={() => setGraphMetric('value')}>Value</button><button className={graphMetric === 'roi' ? 'metric-button active' : 'metric-button'} aria-pressed={graphMetric === 'roi'} onClick={() => setGraphMetric('roi')}>ROI</button></div><div className="range-switch" role="group" aria-label="History range">{historyRanges.map((range) => <button key={range.value} className={historyRange === range.value ? 'range-button active' : 'range-button'} aria-pressed={historyRange === range.value} onClick={() => setHistoryRange(range.value)}>{range.label}</button>)}</div></div>
            </div>
            <div className="chart-area">
              {historyLoading && <div className="chart-state">Loading portfolio history…</div>}
              {!historyLoading && historyError && <div className="chart-state chart-error" role="alert">{historyError}</div>}
              {!historyLoading && !historyError && portfolioHistory?.points.length && (graphMetric === 'value' || portfolioHistory.points.some((point) => point.roi !== null)) ? <Suspense fallback={<div className="chart-state">Loading graph…</div>}><PortfolioChart points={portfolioHistory.points.map((point) => ({ ...point, value: Number(point.value), roi: point.roi === null ? null : Number(point.roi), roi_amount: point.roi_amount === null ? null : Number(point.roi_amount) }))} range={historyRange} currency={portfolioHistory.reporting_currency} metric={graphMetric} /></Suspense> : null}
              {!historyLoading && !historyError && (!portfolioHistory?.points.length || (graphMetric === 'roi' && !portfolioHistory.points.some((point) => point.roi !== null))) && <div className="chart-state"><span className="empty-icon"><ChartNoAxesCombined size={18} /></span><strong>{graphMetric === 'roi' ? 'ROI is unavailable' : 'No historical values for this range'}</strong><span>{graphMetric === 'roi' ? 'Record net deposits to calculate investment return.' : 'Add transactions and market prices to build the portfolio graph.'}</span></div>}
            </div>
            <div className="chart-summary-strip"><div><span>Cash balance</span><strong>{summary ? money(summary.cash_balance, summary.reporting_currency) : '—'}</strong></div><div><span>Invested value</span><strong>{summary ? money(summary.positions_value, summary.reporting_currency) : '—'}</strong></div><div><span>Open positions</span><strong>{holdings.filter((item) => Number(item.quantity) !== 0).length}</strong></div></div>
          </section>

          <section className="data-section holdings-section">
            <div className="section-heading"><div><h2>Positions</h2><p>Market value in each security's trading currency</p></div><button className="text-button" onClick={refreshPrices} disabled={refreshing}>{refreshing ? 'Updating…' : 'Update prices'} <ArrowUpRight size={14} /></button></div>
            <div className="table-wrap"><table><thead><tr><th>SECURITY</th><th>SHARES</th><th>LAST PRICE</th><th>MARKET VALUE</th><th>PRICE TIME</th></tr></thead><tbody>
              {holdings.map((item) => <tr key={`${item.symbol}-${item.currency}`}><td><div className="security-cell"><span className="ticker-mark">{item.symbol.slice(0, 1)}</span><span><strong>{item.symbol}</strong><small>{item.currency} listing</small></span></div></td><td className="numeric">{Number(item.quantity).toLocaleString(undefined, { maximumFractionDigits: 6 })}</td><td className="numeric">{money(item.price, item.price_currency ?? item.currency)}</td><td className="numeric market-value">{money(item.market_value, item.currency)}</td><td className="time-cell">{item.price_observed_at ? new Date(item.price_observed_at).toLocaleString() : <span className="not-priced">Not priced</span>}</td></tr>)}
              {!loading && holdings.length === 0 && <tr><td colSpan={5}><div className="empty-state"><span className="empty-icon"><ChartNoAxesCombined size={20} /></span><strong>No positions yet</strong><p>Add a broker account, then import a transaction CSV to get started.</p><button className="button button-primary" onClick={() => setShowImport(true)}><FileUp size={15} /> Import transactions</button></div></td></tr>}
              {loading && holdings.length === 0 && <tr><td colSpan={5} className="loading-cell">Loading portfolio…</td></tr>}
            </tbody></table></div>
          </section>
          <section className="data-section activity-section"><div className="section-heading"><div><h2>Recent activity</h2><p>Latest entries from your transaction ledger</p></div><button className="text-button" onClick={() => setView('activity')}>View all <ArrowUpRight size={14} /></button></div><TransactionTable transactions={transactions.slice(0, 5)} loading={loading} onEdit={openTransactionForm} onDelete={removeTransaction} /></section>
        </> : view === 'activity' ? <section className="data-section full-activity-section"><div className="section-heading"><div><h2>All transactions</h2><p>Up to 500 most recent ledger entries</p></div><span className="table-count">{transactions.length} records</span></div><TransactionTable transactions={transactions} loading={loading} onEdit={openTransactionForm} onDelete={removeTransaction} /></section> : <section className="settings-panel" aria-labelledby="settings-title"><div className="settings-section-heading"><div><h2 id="settings-title">Portfolio display</h2><p>Choose the currency used for portfolio totals.</p></div></div><form className="settings-form" onSubmit={saveSettings}><label htmlFor="reporting-currency">Reporting currency</label><div className="settings-control-row"><select id="reporting-currency" value={reportingCurrency} disabled={loading || savingSettings || summary === null} onChange={(event) => setReportingCurrency(event.target.value)}><option value="USD">USD - US Dollar</option><option value="EUR">EUR - Euro</option><option value="GBP">GBP - British Pound</option><option value="CAD">CAD - Canadian Dollar</option><option value="AUD">AUD - Australian Dollar</option><option value="JPY">JPY - Japanese Yen</option><option value="CHF">CHF - Swiss Franc</option></select><button className="button button-primary" type="submit" disabled={loading || savingSettings || summary === null || reportingCurrency === summary.reporting_currency}>{savingSettings ? 'Saving…' : 'Save changes'}</button></div></form></section>}
        <footer className="page-footer"><span>Folio Portfolio Tracker</span></footer>
      </div>
    </main>

    {showAccountForm && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowAccountForm(false) }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="account-title"><div className="modal-heading"><div><span className="modal-kicker">BROKER SETUP</span><h2 id="account-title">Add an account</h2></div><button className="icon-button" aria-label="Close" onClick={() => setShowAccountForm(false)}><X size={18} /></button></div><form className="modal-form" onSubmit={createAccount}><label>Account name<input name="name" placeholder="Long-term investments" required maxLength={100} /></label><label>Broker or institution<input name="broker" placeholder="Broker name" maxLength={100} /></label><label>Account currency<select name="currency" defaultValue="USD"><option>USD</option><option>EUR</option><option>GBP</option><option>CAD</option><option>AUD</option><option>JPY</option><option>CHF</option></select></label><div className="modal-actions"><button type="button" className="button button-subtle" onClick={() => setShowAccountForm(false)}>Cancel</button><button className="button button-primary" type="submit"><Plus size={16} /> Create account</button></div></form></section></div>}

    {showTransactionForm && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowTransactionForm(false) }}><section className="modal transaction-modal" role="dialog" aria-modal="true" aria-labelledby="transaction-title"><div className="modal-heading"><div><span className="modal-kicker">LEDGER ENTRY</span><h2 id="transaction-title">{editingTransaction ? 'Edit transaction' : 'Add transaction'}</h2></div><button className="icon-button" aria-label="Close" onClick={() => setShowTransactionForm(false)}><X size={18} /></button></div><form className="modal-form transaction-form" onSubmit={saveTransaction}><label>Broker account<select name="account_id" defaultValue={editingTransaction?.account_id ?? (selectedAccount === 'all' ? accounts[0]?.id : Number(selectedAccount))} required>{accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label><div className="transaction-form-grid"><label>Type<select name="type" defaultValue={editingTransaction?.type ?? 'buy'}><option value="buy">Buy</option><option value="sell">Sell</option><option value="dividend">Dividend</option><option value="fee">Fee</option><option value="split">Split</option><option value="deposit">Deposit</option><option value="withdrawal">Withdrawal</option></select></label><label>Date<input name="transaction_date" type="date" defaultValue={editingTransaction?.transaction_date ?? today} required /></label><label>Symbol<input name="symbol" defaultValue={editingTransaction?.symbol ?? ''} maxLength={32} /></label><label>Currency<select name="currency" defaultValue={editingTransaction?.currency ?? 'USD'}><option>USD</option><option>EUR</option><option>GBP</option><option>CAD</option><option>AUD</option><option>JPY</option><option>CHF</option></select></label><label>Quantity<input name="quantity" type="number" step="any" defaultValue={editingTransaction?.quantity ?? '0'} /></label><label>Price<input name="price" type="number" step="any" defaultValue={editingTransaction?.price ?? '0'} /></label><label>Fees<input name="fees" type="number" step="any" defaultValue={editingTransaction?.fees ?? '0'} /></label><label>Cash amount<input name="cash_amount" type="number" step="any" defaultValue={editingTransaction?.cash_amount ?? ''} /></label></div><label>Notes<input name="notes" defaultValue={editingTransaction?.notes ?? ''} maxLength={500} /></label><div className="modal-actions"><button type="button" className="button button-subtle" onClick={() => setShowTransactionForm(false)}>Cancel</button><button className="button button-primary" type="submit">{editingTransaction ? 'Save changes' : 'Add transaction'}</button></div></form></section></div>}

    {showImport && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) { setShowImport(false); setPreview(null) } }}><section className={`modal import-modal${preview ? ' modal-wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="import-title"><div className="modal-heading"><div><span className="modal-kicker">LEDGER IMPORT</span><h2 id="import-title">Import transactions</h2></div><button className="icon-button" aria-label="Close" onClick={() => { setShowImport(false); setPreview(null) }}><X size={18} /></button></div>
      {!preview ? <form className="modal-form" onSubmit={previewCsv}>{accounts.length === 0 && <div className="inline-warning">Create a broker account before importing transactions.</div>}<label>CSV file<input name="file" type="file" accept=".csv,text/csv" required /></label><p className="form-help">Columns: date, account, symbol, type, quantity, price, currency, fees, cash_amount, notes. Date format: YYYY-MM-DD.</p><div className="modal-actions"><button type="button" className="button button-subtle" onClick={() => setShowImport(false)}>Cancel</button><button className="button button-primary" type="submit" disabled={importBusy || accounts.length === 0}><FileUp size={16} /> {importBusy ? 'Checking file…' : 'Preview import'}</button></div></form> : <div className="preview-content"><div className="preview-summary"><div><strong>{preview.filename}</strong><span>{preview.total_rows} rows</span></div><div className="preview-counts"><span className="count-valid">{preview.valid_rows} ready</span><span className="count-invalid">{preview.invalid_rows} skipped</span></div></div><div className="preview-table-wrap"><table><thead><tr><th>LINE</th><th>DATE</th><th>ACCOUNT</th><th>TYPE</th><th>SYMBOL</th><th>QUANTITY</th><th>PRICE</th><th>CHECK</th></tr></thead><tbody>{preview.rows.map((row) => <tr key={row.line} className={row.valid ? '' : 'invalid-row'}><td>{row.line}</td><td>{row.transaction_date ?? '—'}</td><td>{row.account_name ?? '—'}</td><td>{row.type ?? '—'}</td><td>{row.symbol ?? '—'}</td><td>{row.quantity ?? '—'}</td><td>{row.price ? money(row.price, row.currency ?? 'USD') : '—'}</td><td>{row.valid ? <span className="valid-label">Ready</span> : <span className="error-label">{row.errors.join('; ')}</span>}</td></tr>)}</tbody></table></div><div className="modal-actions"><button className="button button-subtle" onClick={() => setPreview(null)}>Choose another file</button><button className="button button-primary" onClick={commitImport} disabled={importBusy || preview.valid_rows === 0}><ArrowDownLeft size={16} /> {importBusy ? 'Importing…' : `Import ${preview.valid_rows} rows`}</button></div></div>}
    </section></div>}
  </div>
}

function TransactionTable({ transactions, loading, onEdit, onDelete }: { transactions: Transaction[]; loading: boolean; onEdit: (transaction: Transaction) => void; onDelete: (transaction: Transaction) => void }) {
  return <div className="table-wrap"><table><thead><tr><th>DATE</th><th>ACTIVITY</th><th>ACCOUNT</th><th>QUANTITY</th><th>PRICE / CASH</th><th>FEES</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>
    {transactions.map((item) => { const value = Number(item.quantity) * Number(item.price); const positive = item.type === 'sell' || item.type === 'dividend'; return <tr key={item.id}><td className="time-cell">{new Date(`${item.transaction_date}T00:00:00`).toLocaleDateString()}</td><td><div className="transaction-type"><span className={`type-icon ${positive ? 'type-positive' : ''}`}>{positive ? <ArrowDownLeft size={14} /> : <ArrowUpRight size={14} />}</span><span><strong>{item.symbol ?? item.type}</strong><small>{item.type}</small></span></div></td><td>{item.account_name}</td><td className="numeric">{Number(item.quantity) ? Number(item.quantity).toLocaleString(undefined, { maximumFractionDigits: 6 }) : '—'}</td><td className="numeric">{money(item.cash_amount ?? String(value), item.currency)}</td><td className="numeric">{money(item.fees, item.currency)}</td><td className="transaction-actions"><button className="icon-button" title="Edit transaction" aria-label={`Edit ${item.type} transaction`} onClick={() => onEdit(item)}><Pencil size={14} /></button><button className="icon-button delete-button" title="Remove transaction" aria-label={`Remove ${item.type} transaction`} onClick={() => onDelete(item)}><Trash2 size={14} /></button></td></tr> })}
    {!loading && transactions.length === 0 && <tr><td colSpan={7}><div className="empty-activity"><span className="empty-icon"><Clock3 size={18} /></span><span><strong>No transactions recorded</strong><small>Add a transaction or import a CSV to build your portfolio ledger.</small></span></div></td></tr>}
    {loading && transactions.length === 0 && <tr><td colSpan={7} className="loading-cell">Loading transactions…</td></tr>}
  </tbody></table></div>
}

export default App