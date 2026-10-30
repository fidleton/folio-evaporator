import logging
import time
from functools import wraps
from datetime import datetime, timezone
from decimal import Decimal

import yfinance as yf


external_logger = logging.getLogger("uvicorn.error")


def log_external_call(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        started_at = time.perf_counter()
        try:
            result = method(self, *args, **kwargs)
        except Exception as error:
            external_logger.warning(
                "External call failed provider=%s operation=%s duration_ms=%.2f error_type=%s",
                self.name,
                method.__name__,
                (time.perf_counter() - started_at) * 1000,
                type(error).__name__,
            )
            raise
        external_logger.info(
            "External call completed provider=%s operation=%s duration_ms=%.2f",
            self.name,
            method.__name__,
            (time.perf_counter() - started_at) * 1000,
        )
        return result

    return wrapped


class YahooFinanceProvider:
    name = "Yahoo Finance (yfinance)"

    @log_external_call
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

    @log_external_call
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

    @log_external_call
    def history(self, symbol: str, period: str) -> tuple[str, list[dict[str, str]]]:
        ticker = yf.Ticker(symbol)
        interval = "5m" if period == "1d" else "1h" if period == "5d" else "1d"
        frame = ticker.history(period=period, interval=interval, auto_adjust=False)
        if frame.empty:
            raise ValueError(f"No price history available for {symbol}")
        currency = str(ticker.fast_info.get("currency") or "USD").upper()
        points = [
            {
                "date": (
                    index.to_pydatetime().astimezone(timezone.utc).isoformat()
                    if interval != "1d"
                    else index.date().isoformat()
                ),
                "close": str(Decimal(str(row["Close"]))),
            }
            for index, row in frame.iterrows()
            if row["Close"] == row["Close"]
        ]
        return currency, points

    @log_external_call
    def fx_rate(self, base_currency: str, quote_currency: str) -> tuple[Decimal, str]:
        if base_currency == quote_currency:
            return Decimal("1"), datetime.now(timezone.utc).isoformat()
        price, _, observed_at = self.quote(f"{base_currency}{quote_currency}=X")
        return price, observed_at
