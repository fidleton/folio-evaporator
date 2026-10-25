import { lazy, Suspense, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  Activity, ArrowDown, ArrowDownLeft, ArrowUp, ArrowUpRight, BriefcaseBusiness,
  ArrowLeft, ChartNoAxesCombined, Clock3, FileUp, Pencil, Plus, Settings2,
  RefreshCw, Trash2, WalletCards, X,
} from 'lucide-react'
import './folio.css'

const PortfolioChart = lazy(() => import('./PortfolioChart'))

type Account = { id: number; name: string; broker: string; base_currency: string }
type TimeFormat = '24-hour' | '12-hour'
type Summary = { reporting_currency: string; time_format: TimeFormat; positions_value: string; market_value: string; unrealized_pl: string | null; realized_pl: string | null; total_return: string | null; cash_balance: string; total_value: string; unpriced_positions: number }
type HistoryRange = '1d' | '5d' | '1mo' | 'ytd' | '1y' | 'max'
type SecurityHistoryRange = '1d' | '5d' | '1mo' | '3mo' | '6mo' | '1y' | '2y' | '5y' | 'max'
type PositionStatus = 'open' | 'closed'
type PositionSortKey = 'symbol' | 'quantity' | 'average_cost' | 'price' | 'market_value' | 'unrealized_pl' | 'realized_pl' | 'total_return' | 'price_observed_at'
type SortDirection = 'asc' | 'desc'
type GraphMetric = 'value' | 'roi'
type WorkspaceView = 'portfolio' | 'activity' | 'settings'
type InterfacePreferences = {
  selectedAccount: string
  positionStatus: PositionStatus
  positionSortKey: PositionSortKey
  positionSortDirection: SortDirection
  historyRange: HistoryRange
  graphMetric: GraphMetric
  securityHistoryRange: SecurityHistoryRange
  view: WorkspaceView
}
type HistoryPoint = { date: string; value: string; roi: string | null; roi_amount: string | null }
type PortfolioHistory = { period: HistoryRange; reporting_currency: string; points: HistoryPoint[] }
type SecurityHistory = { symbol: string; currency: string; points: Array<{ date: string; close: string }> }
type Holding = { symbol: string; name: string; currency: string; quantity: string; average_cost: string | null; price: string | null; price_currency: string | null; price_observed_at: string | null; market_value: string | null; unrealized_pl: string | null; realized_pl: string; total_return: string | null }
type Transaction = { id: number; account_id: number; account_name: string; symbol: string | null; isin: string | null; type: string; transaction_date: string; quantity: string; price: string; currency: string; fees: string; taxes: string; cash_amount: string | null; notes: string }
type SecuritySuggestion = { symbol: string; name: string; exchange: string; quote_type: string; isin: string | null; currency: string | null }
type CsvPreview = { id: string; filename: string; total_rows: number; valid_rows: number; invalid_rows: number; rows: Array<{ line: number; valid: boolean; errors: string[]; account_name?: string; transaction_date?: string; type?: string; symbol?: string | null; isin?: string | null; quantity?: string; price?: string; currency?: string }> }

const pageBase = window.location.pathname.endsWith('/') ? window.location.pathname : `${window.location.pathname}/`
const apiBase = new URL('api/', `${window.location.origin}${pageBase}`)
function localDateTimeValue(value = new Date(), timeFormat: TimeFormat = '24-hour') {
  const date = `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
  const minute = String(value.getMinutes()).padStart(2, '0')
  const second = String(value.getSeconds()).padStart(2, '0')
  if (timeFormat === '24-hour') return `${date} ${String(value.getHours()).padStart(2, '0')}:${minute}:${second}`
  const hours = value.getHours()
  return `${date} ${String(hours % 12 || 12).padStart(2, '0')}:${minute}:${second} ${hours >= 12 ? 'PM' : 'AM'}`
}

function transactionDateTimeValue(value: string, timeFormat: TimeFormat) {
  const dateTime = value.length === 10 ? `${value}T00:00:00` : value
  return localDateTimeValue(new Date(dateTime), timeFormat)
}

function parseInputDateTime(value: string, timeFormat: TimeFormat) {
  const match = timeFormat === '24-hour'
    ? /^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/.exec(value.trim())
    : /^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)$/i.exec(value.trim())
  if (!match) return null
  let hour = Number(match[4])
  const minute = Number(match[5])
  const second = Number(match[6])
  if (minute > 59 || second > 59) return null
  if (timeFormat === '12-hour') {
    if (hour < 1 || hour > 12) return null
    hour = hour % 12 + (match[7].toUpperCase() === 'PM' ? 12 : 0)
  } else if (hour > 23) return null
  return `${match[1]}-${match[2]}-${match[3]}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}`
}
const historyRanges: Array<{ value: HistoryRange; label: string }> = [
  { value: '1d', label: '1D' }, { value: '5d', label: '1W' },
  { value: '1mo', label: '1M' }, { value: 'ytd', label: 'YTD' },
  { value: '1y', label: '1Y' }, { value: 'max', label: 'MAX' },
]
const securityHistoryRanges: Array<{ value: SecurityHistoryRange; label: string }> = [
  { value: '1d', label: '1D' }, { value: '5d', label: '1W' },
  { value: '1mo', label: '1M' }, { value: '3mo', label: '3M' },
  { value: '6mo', label: '6M' }, { value: '1y', label: '1Y' },
  { value: '2y', label: '2Y' }, { value: '5y', label: '5Y' },
  { value: 'max', label: 'MAX' },
]
const positionSortOptions: Array<{ value: PositionSortKey; label: string }> = [
  { value: 'symbol', label: 'Security' }, { value: 'quantity', label: 'Shares' },
  { value: 'average_cost', label: 'Average cost' }, { value: 'price', label: 'Last price' },
  { value: 'market_value', label: 'Market value' }, { value: 'unrealized_pl', label: 'Unrealized P/L' },
  { value: 'realized_pl', label: 'Realized P/L' }, { value: 'total_return', label: 'Total return' },
  { value: 'price_observed_at', label: 'Price time' },
]
const transactionCurrencies = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF']
const preferenceStorageKey = 'folio.interface-preferences.v1'
const defaultPreferences: InterfacePreferences = {
  selectedAccount: 'all',
  positionStatus: 'open',
  positionSortKey: 'symbol',
  positionSortDirection: 'asc',
  historyRange: '1mo',
  graphMetric: 'value',
  securityHistoryRange: '1y',
  view: 'portfolio',
}

function readInterfacePreferences(): InterfacePreferences {
  try {
    const stored = JSON.parse(window.localStorage.getItem(preferenceStorageKey) ?? 'null') as Partial<InterfacePreferences> | null
    if (!stored || typeof stored !== 'object') return defaultPreferences
    return {
      selectedAccount: typeof stored.selectedAccount === 'string' && (stored.selectedAccount === 'all' || /^\d+$/.test(stored.selectedAccount)) ? stored.selectedAccount : defaultPreferences.selectedAccount,
      positionStatus: stored.positionStatus === 'open' || stored.positionStatus === 'closed' ? stored.positionStatus : defaultPreferences.positionStatus,
      positionSortKey: ['symbol', 'quantity', 'average_cost', 'price', 'market_value', 'unrealized_pl', 'realized_pl', 'total_return', 'price_observed_at'].includes(stored.positionSortKey ?? '') ? stored.positionSortKey as PositionSortKey : defaultPreferences.positionSortKey,
      positionSortDirection: stored.positionSortDirection === 'asc' || stored.positionSortDirection === 'desc' ? stored.positionSortDirection : defaultPreferences.positionSortDirection,
      historyRange: historyRanges.some((range) => range.value === stored.historyRange) ? stored.historyRange as HistoryRange : defaultPreferences.historyRange,
      graphMetric: stored.graphMetric === 'value' || stored.graphMetric === 'roi' ? stored.graphMetric : defaultPreferences.graphMetric,
      securityHistoryRange: securityHistoryRanges.some((range) => range.value === stored.securityHistoryRange) ? stored.securityHistoryRange as SecurityHistoryRange : defaultPreferences.securityHistoryRange,
      view: stored.view === 'portfolio' || stored.view === 'activity' || stored.view === 'settings' ? stored.view : defaultPreferences.view,
    }
  } catch {
    return defaultPreferences
  }
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

function SecurityFields({ initialSymbol, initialIsin, initialCurrency }: { initialSymbol: string | null; initialIsin: string | null; initialCurrency: string | null }) {
  const [symbol, setSymbol] = useState(initialSymbol ?? '')
  const [isin, setIsin] = useState(initialIsin ?? '')
  const [companyName, setCompanyName] = useState('')
  const [currency, setCurrency] = useState(initialCurrency ?? 'USD')
  const [activeField, setActiveField] = useState<'symbol' | 'isin' | null>(null)
  const [suggestions, setSuggestions] = useState<SecuritySuggestion[]>([])
  const [searching, setSearching] = useState(false)
  const currencyLookupId = useRef(0)
  const query = activeField === 'symbol' ? symbol : activeField === 'isin' ? isin : ''
  const normalizedQuery = query.trim()
  const canSearch = activeField === 'symbol'
    ? normalizedQuery.length >= 2
    : activeField === 'isin' && normalizedQuery.length === 12

  useEffect(() => {
    if (!canSearch) return

    let active = true
    const timeout = window.setTimeout(() => {
      setSearching(true)
      api<SecuritySuggestion[]>(`securities/search?q=${encodeURIComponent(normalizedQuery)}`)
        .then((results) => { if (active) setSuggestions(results) })
        .catch(() => { if (active) setSuggestions([]) })
        .finally(() => { if (active) setSearching(false) })
    }, 250)
    return () => {
      active = false
      window.clearTimeout(timeout)
    }
  }, [activeField, canSearch, normalizedQuery])

  function chooseSuggestion(suggestion: SecuritySuggestion) {
    setSymbol(suggestion.symbol)
    setCompanyName(suggestion.name)
    if (suggestion.isin) setIsin(suggestion.isin)
    setActiveField(null)
    setSuggestions([])
    const lookupId = ++currencyLookupId.current
    const suggestionCurrency = suggestion.currency?.trim().toUpperCase()
    if (suggestionCurrency && /^[A-Z]{3}$/.test(suggestionCurrency)) {
      setCurrency(suggestionCurrency)
    } else {
      api<{ currency: string }>(`prices/${encodeURIComponent(suggestion.symbol)}`)
        .then(({ currency: quoteCurrency }) => {
          const normalizedCurrency = quoteCurrency.trim().toUpperCase()
          if (currencyLookupId.current === lookupId && /^[A-Z]{3}$/.test(normalizedCurrency)) {
            setCurrency(normalizedCurrency)
          }
        })
        .catch(() => {})
    }
  }

  const suggestionList = canSearch && (searching || suggestions.length > 0) && (
    <div className="security-suggestions" role="listbox" aria-label="Security suggestions">
      {searching && <div className="security-search-status">Searching securities…</div>}
      {suggestions.map((suggestion) => (
        <button key={suggestion.symbol} type="button" role="option" aria-selected="false" onMouseDown={(event) => event.preventDefault()} onClick={() => chooseSuggestion(suggestion)}>
          <strong>{suggestion.symbol}</strong>
          <span>{suggestion.name}{suggestion.exchange ? ` · ${suggestion.exchange}` : ''}</span>
        </button>
      ))}
    </div>
  )

  return <>
    <div className="security-field">
      <label htmlFor="transaction-symbol">Symbol</label>
      <input id="transaction-symbol" name="symbol" value={symbol} maxLength={32} autoComplete="off" aria-autocomplete="list" aria-expanded={activeField === 'symbol' && (searching || suggestions.length > 0)} onFocus={() => { setActiveField('symbol'); setSuggestions([]); setSearching(false) }} onChange={(event) => { setSymbol(event.target.value); setCompanyName(''); setActiveField('symbol'); setSuggestions([]); setSearching(false) }} />
      {activeField === 'symbol' && suggestionList}
    </div>
    <div className="security-field">
      <label htmlFor="transaction-isin">ISIN</label>
      <input id="transaction-isin" name="isin" value={isin} maxLength={12} autoComplete="off" aria-autocomplete="list" aria-expanded={activeField === 'isin' && (searching || suggestions.length > 0)} onFocus={() => { setActiveField('isin'); setSuggestions([]); setSearching(false) }} onChange={(event) => { setIsin(event.target.value); setActiveField('isin'); setSuggestions([]); setSearching(false) }} />
      {activeField === 'isin' && suggestionList}
    </div>
    <label>Currency<select name="currency" value={currency} onChange={(event) => { currencyLookupId.current += 1; setCurrency(event.target.value) }}>
      {!transactionCurrencies.includes(currency) && <option value={currency}>{currency}</option>}
      {transactionCurrencies.map((option) => <option key={option}>{option}</option>)}
    </select></label>
    <input type="hidden" name="security_name" value={companyName} />
  </>
}

function money(value: string | null | undefined, currency: string) {
  if (value == null || !Number.isFinite(Number(value))) return 'Not priced'
  return new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 2 }).format(Number(value))
}

function normalizedTransactionDate(value: string) {
  return value.length === 10 ? `${value}T00:00:00` : value
}

function formatTransactionDate(value: string, timeFormat: TimeFormat) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium', hourCycle: timeFormat === '24-hour' ? 'h23' : 'h12' }).format(new Date(normalizedTransactionDate(value)))
}

function formatLocalDateTime(value: string, timeFormat: TimeFormat) {
  return new Date(value).toLocaleString(undefined, { year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', hourCycle: timeFormat === '24-hour' ? 'h23' : 'h12' })
}

function formatShareCount(value: string) {
  const quantity = Number(value)
  return Number.isFinite(quantity)
    ? quantity.toLocaleString(undefined, { maximumSignificantDigits: 15 })
    : value
}

function sortPositions(positions: Holding[], sortKey: PositionSortKey, direction: SortDirection) {
  return [...positions].sort((left, right) => {
    if (sortKey === 'symbol' || sortKey === 'price_observed_at') {
      const leftValue = left[sortKey]
      const rightValue = right[sortKey]
      if (leftValue == null) return rightValue == null ? 0 : 1
      if (rightValue == null) return -1
      const stringOrder = leftValue.localeCompare(rightValue)
      return stringOrder === 0 || direction === 'asc' ? stringOrder : -stringOrder
    }

    const leftValue = left[sortKey] == null ? null : Number(left[sortKey])
    const rightValue = right[sortKey] == null ? null : Number(right[sortKey])
    if (leftValue == null || !Number.isFinite(leftValue)) return rightValue == null ? 0 : 1
    if (rightValue == null || !Number.isFinite(rightValue)) return -1
    const valueOrder = (leftValue - rightValue) * (direction === 'asc' ? 1 : -1)
    return valueOrder || left.symbol.localeCompare(right.symbol)
  })
}

function SortableHeader({ label, sortKey, activeSortKey, direction, onSort }: {
  label: string
  sortKey: PositionSortKey
  activeSortKey: PositionSortKey
  direction: SortDirection
  onSort: (sortKey: PositionSortKey) => void
}) {
  const active = sortKey === activeSortKey
  return <th aria-sort={active ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}>
    <button className={active ? 'sort-header active' : 'sort-header'} type="button" onClick={() => onSort(sortKey)}>
      {label}{active && (direction === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
    </button>
  </th>
}

function portfolioRangePerformance(points: HistoryPoint[]) {
  if (points.length < 2) return null
  const startingValue = Number(points[0].value)
  const endingValue = Number(points[points.length - 1].value)
  if (!Number.isFinite(startingValue) || !Number.isFinite(endingValue)) return null
  const amount = endingValue - startingValue
  const startingValueMagnitude = Math.abs(startingValue)
  return { amount, percent: startingValueMagnitude > 0 ? amount / startingValueMagnitude * 100 : null }
}

function securityRangePerformance(points: SecurityHistory['points'], quantity: string) {
  if (points.length < 2) return null
  const startPrice = Number(points[0].close)
  const endPrice = Number(points[points.length - 1].close)
  const shareCount = Number(quantity)
  if (!Number.isFinite(startPrice) || !Number.isFinite(endPrice) || !Number.isFinite(shareCount) || startPrice === 0 || shareCount === 0) return null
  const amount = (endPrice - startPrice) * shareCount
  const startingValue = Math.abs(startPrice * shareCount)
  return { amount, percent: amount / startingValue * 100 }
}

async function fetchPortfolioData(selectedAccount: string, positionStatus: PositionStatus) {
  const params = new URLSearchParams()
  if (selectedAccount !== 'all') params.set('account_id', selectedAccount)
  const query = params.toString()
  const holdingsParams = new URLSearchParams(params)
  holdingsParams.set('status', positionStatus)
  const [accounts, summary, holdings, transactions] = await Promise.all([
    api<Account[]>('accounts'), api<Summary>(`portfolio/summary${query ? `?${query}` : ''}`),
    api<Holding[]>(`portfolio/holdings?${holdingsParams}`), api<Transaction[]>(`transactions${query ? `?${query}` : ''}`),
  ])
  return { accounts, summary, holdings, transactions }
}

function App() {
  const [initialPreferences] = useState(readInterfacePreferences)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [selectedAccount, setSelectedAccount] = useState(initialPreferences.selectedAccount)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [positionStatus, setPositionStatus] = useState(initialPreferences.positionStatus)
  const [positionSortKey, setPositionSortKey] = useState(initialPreferences.positionSortKey)
  const [positionSortDirection, setPositionSortDirection] = useState(initialPreferences.positionSortDirection)
  const [holdings, setHoldings] = useState<Holding[]>([])
  const [transactions, setTransactions] = useState<Transaction[]>([])
  const [historyRange, setHistoryRange] = useState(initialPreferences.historyRange)
  const [graphMetric, setGraphMetric] = useState(initialPreferences.graphMetric)
  const [portfolioHistory, setPortfolioHistory] = useState<PortfolioHistory | null>(null)
  const [selectedSecurity, setSelectedSecurity] = useState<Holding | null>(null)
  const [securityHistoryRange, setSecurityHistoryRange] = useState(initialPreferences.securityHistoryRange)
  const [securityHistory, setSecurityHistory] = useState<SecurityHistory | null>(null)
  const [securityHistoryLoading, setSecurityHistoryLoading] = useState(false)
  const [securityHistoryError, setSecurityHistoryError] = useState('')
  const [loadedHistoryKey, setLoadedHistoryKey] = useState('')
  const [historyFailure, setHistoryFailure] = useState<{ key: string; message: string } | null>(null)
  const [historyRevision, setHistoryRevision] = useState(0)
  const [view, setView] = useState<WorkspaceView>(initialPreferences.view)
  const [reportingCurrency, setReportingCurrency] = useState('USD')
  const [timeFormat, setTimeFormat] = useState<TimeFormat>('24-hour')
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
  const selectedSecuritySymbol = selectedSecurity?.symbol
  const historyLoading = loadedHistoryKey !== historyRequestKey
  const historyError = historyFailure?.key === historyRequestKey ? historyFailure.message : ''
  const portfolioPerformance = historyLoading || !portfolioHistory ? null : portfolioRangePerformance(portfolioHistory.points)
  const sortedHoldings = sortPositions(holdings, positionSortKey, positionSortDirection)
  const selectedSecurityPerformance = selectedSecurity && !securityHistoryLoading && securityHistory?.symbol === selectedSecurity.symbol
    ? securityRangePerformance(securityHistory.points, selectedSecurity.quantity)
    : null
  const hasSelectedPortfolioMetric = graphMetric === 'value' || portfolioHistory?.points.some((point) => point.roi_amount !== null) === true

  useEffect(() => {
    try {
      window.localStorage.setItem(preferenceStorageKey, JSON.stringify({
        selectedAccount,
        positionStatus,
        positionSortKey,
        positionSortDirection,
        historyRange,
        graphMetric,
        securityHistoryRange,
        view,
      } satisfies InterfacePreferences))
    } catch {
      // Preferences remain usable for this visit when browser storage is unavailable.
    }
  }, [selectedAccount, positionStatus, positionSortKey, positionSortDirection, historyRange, graphMetric, securityHistoryRange, view])

  function selectSecurity(item: Holding) {
    setSelectedSecurity(item)
    if (selectedSecuritySymbol !== item.symbol) {
      setSecurityHistoryLoading(true)
      setSecurityHistoryError('')
    }
  }

  function selectPositionStatus(status: PositionStatus) {
    if (status !== positionStatus) setLoading(true)
    setPositionStatus(status)
    setSelectedSecurity(null)
    setSecurityHistoryLoading(false)
    setSecurityHistoryError('')
  }

  function sortByHeader(sortKey: PositionSortKey) {
    if (positionSortKey === sortKey) {
      setPositionSortDirection((direction) => direction === 'asc' ? 'desc' : 'asc')
      return
    }
    setPositionSortKey(sortKey)
    setPositionSortDirection(sortKey === 'symbol' || sortKey === 'price_observed_at' ? 'asc' : 'desc')
  }

  async function loadData() {
    try {
      const data = await fetchPortfolioData(selectedAccount, positionStatus)
      setAccounts(data.accounts)
      setSummary(data.summary)
      setReportingCurrency(data.summary.reporting_currency)
      setTimeFormat(data.summary.time_format)
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
    fetchPortfolioData(selectedAccount, positionStatus)
      .then((data) => {
        if (!active) return
        setAccounts(data.accounts)
        setSummary(data.summary)
        setReportingCurrency(data.summary.reporting_currency)
        setTimeFormat(data.summary.time_format)
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
  }, [selectedAccount, positionStatus])

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

  useEffect(() => {
    if (!selectedSecuritySymbol) return

    let active = true
    api<SecurityHistory>(`prices/${encodeURIComponent(selectedSecuritySymbol)}/history?period=${securityHistoryRange}`)
      .then((data) => { if (active) setSecurityHistory(data) })
      .catch((requestError: unknown) => {
        if (active) setSecurityHistoryError(requestError instanceof Error ? requestError.message : 'Could not load security price history')
      })
      .finally(() => { if (active) setSecurityHistoryLoading(false) })
    return () => { active = false }
  }, [selectedSecuritySymbol, securityHistoryRange])

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
    const transactionDate = parseInputDateTime(String(form.get('transaction_date') ?? ''), timeFormat)
    if (!transactionDate) {
      setError(`Enter date and time as ${timeFormat === '24-hour' ? 'YYYY-MM-DD HH:MM:SS' : 'YYYY-MM-DD HH:MM:SS AM/PM'}.`)
      return
    }
    const payload = {
      account_id: Number(form.get('account_id')),
      type: form.get('type'),
      transaction_date: transactionDate,
      symbol: String(form.get('symbol') ?? '').trim() || null,
      security_name: String(form.get('security_name') ?? '').trim(),
      isin: String(form.get('isin') ?? '').trim() || null,
      quantity: form.get('quantity') || '0',
      price: form.get('price') || '0',
      currency: form.get('currency'),
      fees: form.get('fees') || '0',
      taxes: form.get('taxes') || '0',
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
      const params = new URLSearchParams({ reporting_currency: reportingCurrency, time_format: timeFormat })
      const result = await api<{ reporting_currency: string; time_format: TimeFormat }>(`settings?${params}`, { method: 'PUT' })
      setReportingCurrency(result.reporting_currency)
      setTimeFormat(result.time_format)
      setHistoryRevision((revision) => revision + 1)
      await loadData()
      setNotice('Display settings saved.')
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
          <label className="account-select-wrap"><WalletCards size={16} /><select aria-label="Filter by broker account" value={selectedAccount} onChange={(event) => { setLoading(true); setSelectedAccount(event.target.value); setSelectedSecurity(null); setSecurityHistoryLoading(false); setSecurityHistoryError('') }}><option value="all">All accounts</option>{accounts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <button className="icon-button" title="Refresh prices" aria-label="Refresh prices" onClick={refreshPrices} disabled={refreshing}><RefreshCw size={17} className={refreshing ? 'spin' : ''} /></button>
          <button className="button button-primary" onClick={() => setShowAccountForm(true)}><Plus size={16} /> Add account</button>
        </div>}
      </header>

      <div className="page-wrap">
        <section className="page-heading"><div><div className="eyebrow">{view === 'settings' ? 'WORKSPACE / PREFERENCES' : <>{accountLabel}</>}</div><h1>{view === 'portfolio' ? selectedSecurity ? selectedSecurity.name || selectedSecurity.symbol : 'Your portfolio' : view === 'activity' ? 'Transaction activity' : 'Settings'}</h1>{view !== 'portfolio' && <p>{view === 'activity' ? 'A single ledger for activity across your accounts.' : 'Manage how portfolio values are displayed.'}</p>}</div>{view === 'activity' && <div className="heading-actions"><button className="button button-primary" onClick={() => openTransactionForm()} disabled={accounts.length === 0}><Plus size={16} /> Add transaction</button><button className="button button-subtle" onClick={() => { setPreview(null); setShowImport(true) }}><FileUp size={16} /> Import CSV</button></div>}</section>
        {error && <div className="alert alert-error" role="alert"><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError('')}><X size={16} /></button></div>}
        {notice && <div className="alert alert-success" role="status"><span>{notice}</span><button aria-label="Dismiss message" onClick={() => setNotice('')}><X size={16} /></button></div>}

        {view === 'portfolio' ? <>
          <section className="portfolio-chart-panel" aria-label={selectedSecurity ? `${selectedSecurity.name} price history` : `Portfolio ${graphMetric === 'value' ? 'value' : 'return on investment'} history`}>
            <div className="chart-header">
              <div className="chart-value-block">
                <div className="panel-kicker">{selectedSecurity ? `SECURITY PRICE · ${selectedSecurity.symbol}` : graphMetric === 'value' ? 'TOTAL PORTFOLIO VALUE' : 'RETURN ON INVESTMENT'}</div>
                <div className="chart-price-line">
                  <div className="chart-total-value">{selectedSecurity ? money(selectedSecurity.price, selectedSecurity.price_currency ?? selectedSecurity.currency) : graphMetric === 'value' ? (summary ? money(summary.total_value, summary.reporting_currency) : '—') : portfolioPerformance == null ? '—' : money(String(portfolioPerformance.amount), portfolioHistory?.reporting_currency ?? 'USD')}</div>
                  {selectedSecurity ? <span className={`chart-pl-readout ${selectedSecurityPerformance == null ? '' : selectedSecurityPerformance.amount >= 0 ? 'positive' : 'negative'}`}>{selectedSecurityPerformance == null ? securityHistoryLoading ? 'P/L loading' : 'P/L unavailable' : `P/L ${money(String(selectedSecurityPerformance.amount), selectedSecurity.currency)}${selectedSecurityPerformance.percent == null ? '' : ` (${selectedSecurityPerformance.percent >= 0 ? '+' : ''}${selectedSecurityPerformance.percent.toFixed(2)}%)`}`}</span> : portfolioPerformance == null ? null : graphMetric === 'roi' ? <span className={`chart-pl-readout ${portfolioPerformance.percent == null ? '' : portfolioPerformance.percent >= 0 ? 'positive' : 'negative'}`}>{portfolioPerformance.percent == null ? 'Percentage unavailable' : `Period return ${portfolioPerformance.percent >= 0 ? '+' : ''}${portfolioPerformance.percent.toFixed(2)}%`}</span> : <span className={`chart-pl-readout ${portfolioPerformance.amount >= 0 ? 'positive' : 'negative'}`}>P/L {money(String(portfolioPerformance.amount), portfolioHistory?.reporting_currency ?? 'USD')}{portfolioPerformance.percent == null ? '' : ` (${portfolioPerformance.percent >= 0 ? '+' : ''}${portfolioPerformance.percent.toFixed(2)}%)`}</span>}
                </div>
                {selectedSecurity ? <div className="chart-value-meta">{`${selectedSecurity.name} · ${selectedSecurity.currency} listing`}</div> : graphMetric === 'value' ? summary?.unpriced_positions ? <div className="chart-value-meta">{summary.unpriced_positions} position{summary.unpriced_positions === 1 ? '' : 's'} awaiting prices</div> : null : <div className="chart-value-meta">{portfolioPerformance == null ? 'Not enough history to calculate this range' : portfolioPerformance.percent == null ? 'Return percentage unavailable without starting investment' : 'Return on starting investment for this range'}</div>}
              </div>
              <div className="chart-controls">{selectedSecurity ? <><div className="range-switch" role="group" aria-label="Security price history range">{securityHistoryRanges.map((range) => <button key={range.value} className={securityHistoryRange === range.value ? 'range-button active' : 'range-button'} aria-pressed={securityHistoryRange === range.value} onClick={() => { if (securityHistoryRange !== range.value) { setSecurityHistoryLoading(true); setSecurityHistoryError(''); setSecurityHistoryRange(range.value) } }}>{range.label}</button>)}</div><button className="text-button" onClick={() => { setSelectedSecurity(null); setSecurityHistoryLoading(false); setSecurityHistoryError('') }}><ArrowLeft size={14} /> Portfolio overview</button></> : <><div className="metric-switch" role="group" aria-label="Graph metric"><button className={graphMetric === 'value' ? 'metric-button active' : 'metric-button'} aria-pressed={graphMetric === 'value'} onClick={() => setGraphMetric('value')}>Value</button><button className={graphMetric === 'roi' ? 'metric-button active' : 'metric-button'} aria-pressed={graphMetric === 'roi'} onClick={() => setGraphMetric('roi')}>ROI</button></div><div className="range-switch" role="group" aria-label="History range">{historyRanges.map((range) => <button key={range.value} className={historyRange === range.value ? 'range-button active' : 'range-button'} aria-pressed={historyRange === range.value} onClick={() => setHistoryRange(range.value)}>{range.label}</button>)}</div></>}</div>
            </div>
            <div className="chart-area">
              {selectedSecurity ? <>
                {securityHistoryLoading && <div className="chart-state">Loading price history…</div>}
                {!securityHistoryLoading && securityHistoryError && <div className="chart-state chart-error" role="alert">{securityHistoryError}</div>}
                {!securityHistoryLoading && !securityHistoryError && securityHistory?.points.length ? <Suspense fallback={<div className="chart-state">Loading graph…</div>}><PortfolioChart points={securityHistory.points.map((point) => ({ date: point.date, value: Number(point.close), roi: null, roi_amount: null }))} range={securityHistoryRange} currency={securityHistory.currency} metric="price" timeFormat={timeFormat} /></Suspense> : null}
                {!securityHistoryLoading && !securityHistoryError && !securityHistory?.points.length && <div className="chart-state"><span className="empty-icon"><ChartNoAxesCombined size={18} /></span><strong>No price history available</strong><span>There are no historical prices for {selectedSecurity.symbol} in this range.</span></div>}
              </> : <>
                {historyLoading && <div className="chart-state">Loading portfolio history…</div>}
                {!historyLoading && historyError && <div className="chart-state chart-error" role="alert">{historyError}</div>}
                {!historyLoading && !historyError && portfolioHistory?.points.length && hasSelectedPortfolioMetric ? <Suspense fallback={<div className="chart-state">Loading graph…</div>}><PortfolioChart points={portfolioHistory.points.map((point) => ({ ...point, value: Number(point.value), roi: point.roi === null ? null : Number(point.roi), roi_amount: point.roi_amount === null ? null : Number(point.roi_amount) }))} range={historyRange} currency={portfolioHistory.reporting_currency} metric={graphMetric} timeFormat={timeFormat} /></Suspense> : null}
                {!historyLoading && !historyError && (!portfolioHistory?.points.length || !hasSelectedPortfolioMetric) && <div className="chart-state"><span className="empty-icon"><ChartNoAxesCombined size={18} /></span><strong>{graphMetric === 'roi' ? 'ROI is unavailable' : 'No historical values for this range'}</strong><span>{graphMetric === 'roi' ? 'Record net deposits to calculate investment return.' : 'Add transactions and market prices to build the portfolio graph.'}</span></div>}
              </>}
            </div>
          </section>

          <section className="data-section holdings-section">
            <div className="section-heading"><div><h2>Positions</h2><p>{positionStatus === 'open' ? 'Open positions · Market value in each security’s trading currency' : 'Closed positions · Realized P/L from fully sold securities'}</p></div><div className="heading-actions"><div className="metric-switch" role="group" aria-label="Position status"><button className={positionStatus === 'open' ? 'metric-button active' : 'metric-button'} aria-pressed={positionStatus === 'open'} onClick={() => selectPositionStatus('open')}>Open</button><button className={positionStatus === 'closed' ? 'metric-button active' : 'metric-button'} aria-pressed={positionStatus === 'closed'} onClick={() => selectPositionStatus('closed')}>Closed</button></div><button className="text-button" onClick={refreshPrices} disabled={refreshing}>{refreshing ? 'Updating…' : 'Update prices'} <ArrowUpRight size={14} /></button></div></div>
            <div className="position-totals" aria-label="Portfolio totals across open and closed positions"><div><span>Market value</span><strong>{summary ? money(summary.market_value, summary.reporting_currency) : '—'}</strong></div><div><span>Cash balance</span><strong>{summary ? money(summary.cash_balance, summary.reporting_currency) : '—'}</strong></div><div><span>Invested value</span><strong>{summary ? money(summary.positions_value, summary.reporting_currency) : '—'}</strong></div><div><span>{positionStatus === 'open' ? 'Open positions' : 'Closed positions'}</span><strong>{holdings.length}</strong></div><div><span>Unrealized P/L</span><strong className={summary?.unrealized_pl == null ? '' : Number(summary.unrealized_pl) >= 0 ? 'positive' : 'negative'}>{summary?.unrealized_pl == null ? '—' : money(summary.unrealized_pl, summary.reporting_currency)}</strong></div><div><span>Realized P/L</span><strong className={summary?.realized_pl == null ? '' : Number(summary.realized_pl) >= 0 ? 'positive' : 'negative'}>{summary?.realized_pl == null ? '—' : money(summary.realized_pl, summary.reporting_currency)}</strong></div><div><span>Total return · open + closed</span><strong className={summary?.total_return == null ? '' : Number(summary.total_return) >= 0 ? 'positive' : 'negative'}>{summary?.total_return == null ? '—' : money(summary.total_return, summary.reporting_currency)}</strong></div></div>
            <div className="mobile-position-view">
              <div className="mobile-position-controls">
                <label htmlFor="mobile-position-sort">Sort by</label>
                <select id="mobile-position-sort" value={positionSortKey} onChange={(event) => sortByHeader(event.target.value as PositionSortKey)}>
                  {positionSortOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
                <button type="button" aria-label={`Switch to ${positionSortDirection === 'asc' ? 'descending' : 'ascending'} order`} title={`Switch to ${positionSortDirection === 'asc' ? 'descending' : 'ascending'} order`} onClick={() => setPositionSortDirection((direction) => direction === 'asc' ? 'desc' : 'asc')}>
                  {positionSortDirection === 'asc' ? <ArrowUp size={16} /> : <ArrowDown size={16} />}
                </button>
              </div>
              {sortedHoldings.map((item) => <button key={`${item.symbol}-${item.currency}`} type="button" className="mobile-position" aria-pressed={selectedSecuritySymbol === item.symbol && selectedSecurity?.currency === item.currency} onClick={() => selectSecurity(item)}>
                <span className="mobile-position-heading">
                  <span className="mobile-position-security"><span className="ticker-mark">{item.symbol.slice(0, 1)}</span><span><strong>{item.name || item.symbol}</strong><small>{item.symbol} · {item.currency} listing</small></span></span>
                  <span className="mobile-position-value"><small>Market value</small><strong>{money(item.market_value, item.currency)}</strong></span>
                </span>
                <span className="mobile-position-stats">
                  <span><small>Shares</small><strong>{formatShareCount(item.quantity)}</strong></span>
                  <span><small>Average cost</small><strong>{item.average_cost == null ? '—' : money(item.average_cost, item.currency)}</strong></span>
                  <span><small>Last price</small><strong>{money(item.price, item.price_currency ?? item.currency)}</strong></span>
                </span>
                <span className="mobile-position-returns">
                  <span className="mobile-position-return-line"><span>Unrealized P/L</span><strong className={item.unrealized_pl == null ? '' : Number(item.unrealized_pl) >= 0 ? 'positive' : 'negative'}>{item.unrealized_pl == null ? '—' : money(item.unrealized_pl, item.currency)}</strong></span>
                  <span className="mobile-position-return-line"><span>Realized P/L</span><strong className={Number(item.realized_pl) >= 0 ? 'positive' : 'negative'}>{money(item.realized_pl, item.currency)}</strong></span>
                  <span className="mobile-position-return-line mobile-position-total-return"><span>Total return</span><strong className={item.total_return == null ? '' : Number(item.total_return) >= 0 ? 'positive' : 'negative'}>{item.total_return == null ? '—' : money(item.total_return, item.currency)}</strong></span>
                </span>
              <span className="mobile-position-time">Price time: {item.price_observed_at ? formatLocalDateTime(item.price_observed_at, timeFormat) : 'Not priced'}</span>
              </button>)}
              {!loading && holdings.length === 0 && <div className="empty-state"><span className="empty-icon"><ChartNoAxesCombined size={20} /></span><strong>{positionStatus === 'open' ? 'No open positions' : 'No closed positions'}</strong><p>{positionStatus === 'open' ? 'Add a broker account, then import a transaction CSV to get started.' : 'Fully sold securities will appear here with their realized P/L.'}</p>{positionStatus === 'open' && <button className="button button-primary" onClick={() => setShowImport(true)}><FileUp size={15} /> Import transactions</button>}</div>}
              {loading && holdings.length === 0 && <div className="loading-cell">Loading portfolio…</div>}
            </div>
            <div className="table-wrap"><table><thead><tr><SortableHeader label="SECURITY" sortKey="symbol" activeSortKey={positionSortKey} direction={positionSortDirection} onSort={sortByHeader} /><SortableHeader label="SHARES" sortKey="quantity" activeSortKey={positionSortKey} direction={positionSortDirection} onSort={sortByHeader} /><SortableHeader label="WEIGHTED AVG COST" sortKey="average_cost" activeSortKey={positionSortKey} direction={positionSortDirection} onSort={sortByHeader} /><SortableHeader label="LAST PRICE" sortKey="price" activeSortKey={positionSortKey} direction={positionSortDirection} onSort={sortByHeader} /><SortableHeader label="MARKET VALUE" sortKey="market_value" activeSortKey={positionSortKey} direction={positionSortDirection} onSort={sortByHeader} /><SortableHeader label="PERFORMANCE" sortKey="total_return" activeSortKey={positionSortKey} direction={positionSortDirection} onSort={sortByHeader} /><SortableHeader label="PRICE TIME" sortKey="price_observed_at" activeSortKey={positionSortKey} direction={positionSortDirection} onSort={sortByHeader} /></tr></thead><tbody>
              {sortedHoldings.map((item) => <tr key={`${item.symbol}-${item.currency}`} className="security-row" tabIndex={0} aria-selected={selectedSecuritySymbol === item.symbol && selectedSecurity?.currency === item.currency} onClick={() => selectSecurity(item)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectSecurity(item) } }}><td><div className="security-cell"><span className="ticker-mark">{item.symbol.slice(0, 1)}</span><span><strong>{item.name || item.symbol}</strong><small>{item.name ? `${item.symbol} · ${item.currency} listing` : `${item.currency} listing`}</small></span></div></td><td className="numeric">{formatShareCount(item.quantity)}</td><td className="numeric">{item.average_cost == null ? '—' : money(item.average_cost, item.currency)}</td><td className="numeric">{money(item.price, item.price_currency ?? item.currency)}</td><td className="numeric market-value">{money(item.market_value, item.currency)}</td><td className="return-cell"><div className="return-stack"><div className="return-line"><span>Unrealized P/L</span><strong className={item.unrealized_pl == null ? '' : Number(item.unrealized_pl) >= 0 ? 'positive' : 'negative'}>{item.unrealized_pl == null ? '—' : money(item.unrealized_pl, item.currency)}</strong></div><div className="return-line"><span>Realized P/L</span><strong className={Number(item.realized_pl) >= 0 ? 'positive' : 'negative'}>{money(item.realized_pl, item.currency)}</strong></div><div className="return-line"><span>Total return</span><strong className={item.total_return == null ? '' : Number(item.total_return) >= 0 ? 'positive' : 'negative'}>{item.total_return == null ? '—' : money(item.total_return, item.currency)}</strong></div></div></td><td className="time-cell">{item.price_observed_at ? formatLocalDateTime(item.price_observed_at, timeFormat) : <span className="not-priced">Not priced</span>}</td></tr>)}
              {!loading && holdings.length === 0 && <tr><td colSpan={7}><div className="empty-state"><span className="empty-icon"><ChartNoAxesCombined size={20} /></span><strong>{positionStatus === 'open' ? 'No open positions' : 'No closed positions'}</strong><p>{positionStatus === 'open' ? 'Add a broker account, then import a transaction CSV to get started.' : 'Fully sold securities will appear here with their realized P/L.'}</p>{positionStatus === 'open' && <button className="button button-primary" onClick={() => setShowImport(true)}><FileUp size={15} /> Import transactions</button>}</div></td></tr>}
              {loading && holdings.length === 0 && <tr><td colSpan={7} className="loading-cell">Loading portfolio…</td></tr>}
            </tbody></table></div>
          </section>
          <section className="data-section activity-section"><div className="section-heading"><div><h2>Recent activity</h2><p>Latest entries from your transaction ledger</p></div><button className="text-button" onClick={() => setView('activity')}>View all <ArrowUpRight size={14} /></button></div><TransactionTable transactions={transactions.slice(0, 5)} loading={loading} timeFormat={timeFormat} onEdit={openTransactionForm} onDelete={removeTransaction} /></section>
        </> : view === 'activity' ? <section className="data-section full-activity-section"><div className="section-heading"><div><h2>All transactions</h2><p>Up to 500 most recent ledger entries</p></div><span className="table-count">{transactions.length} records</span></div><TransactionTable transactions={transactions} loading={loading} timeFormat={timeFormat} onEdit={openTransactionForm} onDelete={removeTransaction} /></section> : <section className="settings-panel" aria-labelledby="settings-title"><div className="settings-section-heading"><div><h2 id="settings-title">Portfolio display</h2><p>Choose the currency and time format used throughout the app.</p></div></div><form className="settings-form" onSubmit={saveSettings}><label htmlFor="reporting-currency">Reporting currency</label><div className="settings-control-row"><select id="reporting-currency" value={reportingCurrency} disabled={loading || savingSettings || summary === null} onChange={(event) => setReportingCurrency(event.target.value)}><option value="USD">USD - US Dollar</option><option value="EUR">EUR - Euro</option><option value="GBP">GBP - British Pound</option><option value="CAD">CAD - Canadian Dollar</option><option value="AUD">AUD - Australian Dollar</option><option value="JPY">JPY - Japanese Yen</option><option value="CHF">CHF - Swiss Franc</option></select></div><label htmlFor="time-format">Time format</label><div className="settings-control-row"><select id="time-format" value={timeFormat} disabled={loading || savingSettings || summary === null} onChange={(event) => setTimeFormat(event.target.value as TimeFormat)}><option value="24-hour">24-hour (14:30)</option><option value="12-hour">12-hour (2:30 PM)</option></select><button className="button button-primary" type="submit" disabled={loading || savingSettings || summary === null || (reportingCurrency === summary.reporting_currency && timeFormat === summary.time_format)}>{savingSettings ? 'Saving…' : 'Save changes'}</button></div></form></section>}
        <footer className="page-footer"><span>Folio Evaporator</span></footer>
      </div>
    </main>

    {showAccountForm && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowAccountForm(false) }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="account-title"><div className="modal-heading"><div><span className="modal-kicker">BROKER SETUP</span><h2 id="account-title">Add an account</h2></div><button className="icon-button" aria-label="Close" onClick={() => setShowAccountForm(false)}><X size={18} /></button></div><form className="modal-form" onSubmit={createAccount}><label>Account name<input name="name" placeholder="Long-term investments" required maxLength={100} /></label><label>Broker or institution<input name="broker" placeholder="Broker name" maxLength={100} /></label><label>Account currency<select name="currency" defaultValue="USD"><option>USD</option><option>EUR</option><option>GBP</option><option>CAD</option><option>AUD</option><option>JPY</option><option>CHF</option></select></label><div className="modal-actions"><button type="button" className="button button-subtle" onClick={() => setShowAccountForm(false)}>Cancel</button><button className="button button-primary" type="submit"><Plus size={16} /> Create account</button></div></form></section></div>}

    {showTransactionForm && (
      <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowTransactionForm(false) }}>
        <section className="modal transaction-modal" role="dialog" aria-modal="true" aria-labelledby="transaction-title">
          <div className="modal-heading">
            <div><span className="modal-kicker">LEDGER ENTRY</span><h2 id="transaction-title">{editingTransaction ? 'Edit transaction' : 'Add transaction'}</h2></div>
            <button className="icon-button" aria-label="Close" onClick={() => setShowTransactionForm(false)}><X size={18} /></button>
          </div>
          <form className="modal-form transaction-form" onSubmit={saveTransaction}>
            <label>Broker account<select name="account_id" defaultValue={editingTransaction?.account_id ?? (selectedAccount === 'all' ? accounts[0]?.id : Number(selectedAccount))} required>{accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label>
            <div className="transaction-form-grid">
              <label>Type<select name="type" defaultValue={editingTransaction?.type ?? 'buy'}><option value="buy">Buy</option><option value="sell">Sell</option><option value="dividend">Dividend</option><option value="fee">Fee</option><option value="split">Split</option><option value="deposit">Deposit</option><option value="withdrawal">Withdrawal</option></select></label>
              <label>Date and time<input name="transaction_date" type="text" inputMode="text" placeholder={timeFormat === '24-hour' ? 'YYYY-MM-DD HH:MM:SS' : 'YYYY-MM-DD HH:MM:SS AM'} pattern={timeFormat === '24-hour' ? '\\d{4}-\\d{2}-\\d{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' : '\\d{4}-\\d{2}-\\d{2} [0-9]{1,2}:[0-9]{2}:[0-9]{2} (AM|PM|am|pm)'} defaultValue={editingTransaction ? transactionDateTimeValue(editingTransaction.transaction_date, timeFormat) : localDateTimeValue(new Date(), timeFormat)} required /></label>
              <SecurityFields key={editingTransaction?.id ?? 'new'} initialSymbol={editingTransaction?.symbol ?? null} initialIsin={editingTransaction?.isin ?? null} initialCurrency={editingTransaction?.currency ?? null} />
              <label>Quantity<input name="quantity" type="number" step="any" defaultValue={editingTransaction?.quantity ?? '0'} /></label>
              <label>Price<input name="price" type="number" step="any" defaultValue={editingTransaction?.price ?? '0'} /></label>
              <label>Fees<input name="fees" type="number" step="any" defaultValue={editingTransaction?.fees ?? '0'} /></label>
              <label>Taxes<input name="taxes" type="number" step="any" defaultValue={editingTransaction?.taxes ?? '0'} /></label>
              <label>Cash amount<input name="cash_amount" type="number" step="any" defaultValue={editingTransaction?.cash_amount ?? ''} /></label>
            </div>
            <label>Notes<input name="notes" defaultValue={editingTransaction?.notes ?? ''} maxLength={500} /></label>
            <div className="modal-actions">
              <button type="button" className="button button-subtle" onClick={() => setShowTransactionForm(false)}>Cancel</button>
              <button className="button button-primary" type="submit">{editingTransaction ? 'Save changes' : 'Add transaction'}</button>
            </div>
          </form>
        </section>
      </div>
    )}

    {showImport && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) { setShowImport(false); setPreview(null) } }}><section className={`modal import-modal${preview ? ' modal-wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="import-title"><div className="modal-heading"><div><span className="modal-kicker">LEDGER IMPORT</span><h2 id="import-title">Import transactions</h2></div><button className="icon-button" aria-label="Close" onClick={() => { setShowImport(false); setPreview(null) }}><X size={18} /></button></div>
      {!preview ? <form className="modal-form" onSubmit={previewCsv}>{accounts.length === 0 && <div className="inline-warning">Create a broker account before importing transactions.</div>}<label>CSV file<input name="file" type="file" accept=".csv,text/csv" required /></label><p className="form-help">Columns: date, account, symbol, type, quantity, price, currency, fees, cash_amount, notes. Use YYYY-MM-DD or an ISO date and time.</p><div className="modal-actions"><button type="button" className="button button-subtle" onClick={() => setShowImport(false)}>Cancel</button><button className="button button-primary" type="submit" disabled={importBusy || accounts.length === 0}><FileUp size={16} /> {importBusy ? 'Checking file…' : 'Preview import'}</button></div></form> : <div className="preview-content"><div className="preview-summary"><div><strong>{preview.filename}</strong><span>{preview.total_rows} rows</span></div><div className="preview-counts"><span className="count-valid">{preview.valid_rows} ready</span><span className="count-invalid">{preview.invalid_rows} skipped</span></div></div><div className="preview-table-wrap"><table><thead><tr><th>LINE</th><th>DATE</th><th>ACCOUNT</th><th>TYPE</th><th>SYMBOL</th><th>QUANTITY</th><th>PRICE</th><th>CHECK</th></tr></thead><tbody>{preview.rows.map((row) => <tr key={row.line} className={row.valid ? '' : 'invalid-row'}><td>{row.line}</td><td>{row.transaction_date ?? '—'}</td><td>{row.account_name ?? '—'}</td><td>{row.type ?? '—'}</td><td>{row.symbol ?? '—'}</td><td>{row.quantity ?? '—'}</td><td>{row.price ? money(row.price, row.currency ?? 'USD') : '—'}</td><td>{row.valid ? <span className="valid-label">Ready</span> : <span className="error-label">{row.errors.join('; ')}</span>}</td></tr>)}</tbody></table></div><div className="modal-actions"><button className="button button-subtle" onClick={() => setPreview(null)}>Choose another file</button><button className="button button-primary" onClick={commitImport} disabled={importBusy || preview.valid_rows === 0}><ArrowDownLeft size={16} /> {importBusy ? 'Importing…' : `Import ${preview.valid_rows} rows`}</button></div></div>}
    </section></div>}
  </div>
}

function TransactionTable({ transactions, loading, timeFormat, onEdit, onDelete }: { transactions: Transaction[]; loading: boolean; timeFormat: TimeFormat; onEdit: (transaction: Transaction) => void; onDelete: (transaction: Transaction) => void }) {
  return <>
    <div className="mobile-transactions">
      {transactions.map((item) => {
        const positive = item.type === 'sell' || item.type === 'dividend'
        const value = Number(item.quantity) * Number(item.price)
        return <article key={item.id} className="mobile-transaction">
          <div className="mobile-transaction-heading">
            <div className="transaction-type"><span className={`type-icon ${positive ? 'type-positive' : ''}`}>{positive ? <ArrowDownLeft size={14} /> : <ArrowUpRight size={14} />}</span><span><strong>{item.symbol ?? item.type}</strong><small>{item.type}</small></span></div>
            <time dateTime={normalizedTransactionDate(item.transaction_date)}>{formatTransactionDate(item.transaction_date, timeFormat)}</time>
          </div>
          <div className="mobile-transaction-details">
            <div><span>Account</span><strong>{item.account_name}</strong></div>
            <div><span>Quantity</span><strong>{Number(item.quantity) ? Number(item.quantity).toLocaleString(undefined, { maximumFractionDigits: 6 }) : '—'}</strong></div>
            <div><span>Price / cash</span><strong>{money(item.cash_amount ?? String(value), item.currency)}</strong></div>
            <div><span>Fees</span><strong>{money(item.fees, item.currency)}</strong></div>
            <div><span>Taxes</span><strong>{money(item.taxes, item.currency)}</strong></div>
          </div>
          <div className="mobile-transaction-actions">
            <button className="icon-button" title="Edit transaction" aria-label={`Edit ${item.type} transaction`} onClick={() => onEdit(item)}><Pencil size={14} /></button>
            <button className="icon-button delete-button" title="Remove transaction" aria-label={`Remove ${item.type} transaction`} onClick={() => onDelete(item)}><Trash2 size={14} /></button>
          </div>
        </article>
      })}
      {!loading && transactions.length === 0 && <div className="empty-activity"><span className="empty-icon"><Clock3 size={18} /></span><span><strong>No transactions recorded</strong><small>Add a transaction or import a CSV to build your portfolio ledger.</small></span></div>}
      {loading && transactions.length === 0 && <div className="loading-cell">Loading transactions…</div>}
    </div>
    <div className="table-wrap transaction-table-wrap"><table><thead><tr><th>DATE</th><th>ACTIVITY</th><th>ACCOUNT</th><th>QUANTITY</th><th>PRICE / CASH</th><th>FEES</th><th>TAXES</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>
      {transactions.map((item) => { const value = Number(item.quantity) * Number(item.price); const positive = item.type === 'sell' || item.type === 'dividend'; return <tr key={item.id}><td className="time-cell">{formatTransactionDate(item.transaction_date, timeFormat)}</td><td><div className="transaction-type"><span className={`type-icon ${positive ? 'type-positive' : ''}`}>{positive ? <ArrowDownLeft size={14} /> : <ArrowUpRight size={14} />}</span><span><strong>{item.symbol ?? item.type}</strong><small>{item.type}</small></span></div></td><td>{item.account_name}</td><td className="numeric">{Number(item.quantity) ? Number(item.quantity).toLocaleString(undefined, { maximumFractionDigits: 6 }) : '—'}</td><td className="numeric">{money(item.cash_amount ?? String(value), item.currency)}</td><td className="numeric">{money(item.fees, item.currency)}</td><td className="numeric">{money(item.taxes, item.currency)}</td><td className="transaction-actions"><button className="icon-button" title="Edit transaction" aria-label={`Edit ${item.type} transaction`} onClick={() => onEdit(item)}><Pencil size={14} /></button><button className="icon-button delete-button" title="Remove transaction" aria-label={`Remove ${item.type} transaction`} onClick={() => onDelete(item)}><Trash2 size={14} /></button></td></tr> })}
      {!loading && transactions.length === 0 && <tr><td colSpan={8}><div className="empty-activity"><span className="empty-icon"><Clock3 size={18} /></span><span><strong>No transactions recorded</strong><small>Add a transaction or import a CSV to build your portfolio ledger.</small></span></div></td></tr>}
      {loading && transactions.length === 0 && <tr><td colSpan={8} className="loading-cell">Loading transactions…</td></tr>}
    </tbody></table></div>
  </>
}

export default App
