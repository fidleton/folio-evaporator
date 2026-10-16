from datetime import datetime, timezone
from decimal import Decimal

import yfinance as yf


class YahooFinanceProvider:
    name = "Yahoo Finance (yfinance)"

    def search(self, query: str) -> list[dict[str, str | None]]:
        results = yf.Search(query, max_results=10, news_count=0)
        securities = []
        seen_symbols = set()
        for quote in results.quotes:
            symbol = quote.get("symbol")
            quote_type = quote.get("quoteType")
            if not symbol or symbol in seen_symbols or quote_type not in ("EQUITY", "ETF", "MUTUALFUND"):
                continue
            seen_symbols.add(symbol)
            securities.append({
                "symbol": symbol,
                "name": quote.get("longname") or quote.get("shortname") or symbol,
                "exchange": quote.get("exchDisp") or quote.get("exchange") or "",
                "quote_type": quote_type,
                "isin": quote.get("isin"),
                "currency": quote.get("currency"),
            })
        return securities

    def quote(self, symbol: str) -> tuple[Decimal, str, str]:
        ticker = yf.Ticker(symbol)
        currency = str(ticker.fast_info.get("currency") or "USD").upper()
        price = ticker.fast_info.get("last_price")
        if price is None:
            history = ticker.history(period="5d")
            if history.empty:
                raise ValueError(f"No quote available for {symbol}")
            price = history["Close"].dropna().iloc[-1]
            observed_at = history.index[-1].to_pydatetime().astimezone(timezone.utc).isoformat()
        else:
            observed_at = datetime.now(timezone.utc).isoformat()
        return Decimal(str(price)), currency, observed_at

    def history(self, symbol: str, period: str) -> tuple[str, list[dict[str, str]]]:
        ticker = yf.Ticker(symbol)
        interval = "5m" if period == "1d" else "1d"
        frame = ticker.history(period=period, interval=interval, auto_adjust=False)
        if frame.empty:
            raise ValueError(f"No price history available for {symbol}")
        currency = str(ticker.fast_info.get("currency") or "USD").upper()
        points = [
            {
                "date": index.isoformat() if interval != "1d" else index.date().isoformat(),
                "close": str(Decimal(str(row["Close"]))),
            }
            for index, row in frame.iterrows()
            if row["Close"] == row["Close"]
        ]
        return currency, points

    def fx_rate(self, base_currency: str, quote_currency: str) -> tuple[Decimal, str]:
        if base_currency == quote_currency:
            return Decimal("1"), datetime.now(timezone.utc).isoformat()
        price, _, observed_at = self.quote(f"{base_currency}{quote_currency}=X")
        return price, observed_at