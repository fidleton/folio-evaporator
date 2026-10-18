import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

type HistoryRange = '1d' | '5d' | '1mo' | '3mo' | '6mo' | 'ytd' | '1y' | '2y' | '5y' | 'max'
type GraphMetric = 'value' | 'roi' | 'price'

type Props = {
  points: Array<{ date: string; value: number; roi: number | null; roi_amount: number | null }>
  range: HistoryRange
  currency: string
  metric: GraphMetric
}

function chartDate(value: string, range: HistoryRange) {
  const parsed = new Date(value.length === 10 ? `${value}T12:00:00` : value)
  const options: Intl.DateTimeFormatOptions = range === '1d'
    ? { hour: 'numeric', minute: '2-digit' }
    : range === 'max' ? { month: 'short', year: '2-digit' } : { month: 'short', day: 'numeric' }
  return new Intl.DateTimeFormat(undefined, options).format(parsed)
}

function PortfolioChart({ points, range, currency, metric }: Props) {
  const dataKey = metric === 'roi' ? 'roi_amount' : 'value'
  return <ResponsiveContainer width="100%" height="100%"><AreaChart data={points} margin={{ top: 12, right: 12, bottom: 0, left: 4 }}>
    <defs><linearGradient id="portfolio-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#377858" stopOpacity={0.22} /><stop offset="95%" stopColor="#377858" stopOpacity={0.015} /></linearGradient></defs>
    <CartesianGrid vertical={false} stroke="#e8ede8" strokeDasharray="3 5" />
    <XAxis dataKey="date" tickFormatter={(value: string) => chartDate(value, range)} tickLine={false} axisLine={false} minTickGap={30} tick={{ fill: '#89958c', fontSize: 10 }} />
    <YAxis domain={['dataMin', 'dataMax']} padding={{ top: 8, bottom: 8 }} tickFormatter={(value: number) => new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 2 }).format(value)} tickLine={false} axisLine={false} width={88} tick={{ fill: '#89958c', fontSize: 10 }} />
    <Tooltip labelFormatter={(value) => chartDate(String(value), range)} formatter={(value) => [new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 2 }).format(Number(value)), metric === 'roi' ? 'Return on investment' : metric === 'price' ? 'Security price' : 'Portfolio value']} contentStyle={{ border: '1px solid #dfe7df', borderRadius: 4, fontFamily: 'var(--sans)', fontSize: 11 }} />
    <Area type="monotone" dataKey={dataKey} stroke="#377858" strokeWidth={2.5} fill="url(#portfolio-fill)" activeDot={{ r: 4, strokeWidth: 2, stroke: '#fff' }} />
  </AreaChart></ResponsiveContainer>
}

export default PortfolioChart