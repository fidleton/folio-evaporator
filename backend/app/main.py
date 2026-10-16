import csv
import hashlib
import io
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import database
from .market_data import YahooFinanceProvider


PROVIDER = YahooFinanceProvider()
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    yield


app = FastAPI(title="Folio Portfolio API", version="0.1.0", lifespan=lifespan)


def get_connection():
    connection = database.connect()
    try:
        yield connection
    finally:
        connection.close()


class AccountInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    broker: str = Field(default="", max_length=100)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("base_currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()


class SecurityInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=120)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class TransactionInput(BaseModel):
    account_id: int
    type: Literal["buy", "sell", "dividend", "fee", "split", "deposit", "withdrawal"]
    transaction_date: datetime
    symbol: str | None = Field(default=None, max_length=32)
    isin: str | None = Field(default=None, min_length=12, max_length=12)
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    fees: Decimal = Decimal("0")
    cash_amount: Decimal | None = None
    notes: str = Field(default="", max_length=500)

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("isin")
    @classmethod
    def uppercase_isin(cls, value: str | None) -> str | None:
        return value.upper() if value else None


def transaction_fingerprint(row: dict) -> str:
    fields = (
        "account_id", "symbol", "type", "transaction_date", "quantity", "price",
        "currency", "fees", "cash_amount", "notes",
    )
    canonical_row = {field: row.get(field) for field in fields}
    if row.get("isin"):
        canonical_row["isin"] = row["isin"]
    canonical = json.dumps(canonical_row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_transaction(row: dict) -> list[str]:
    errors: list[str] = []
    if row.get("isin") and len(row["isin"]) != 12:
        errors.append("ISIN must contain exactly 12 characters")
    if row["type"] in ("buy", "sell"):
        if not row.get("symbol"):
            errors.append("Symbol is required for a trade")
        if Decimal(row["quantity"]) <= 0:
            errors.append("Quantity must be greater than zero")
        if Decimal(row["price"]) <= 0:
            errors.append("Price must be greater than zero")
    if row["type"] == "split":
        if not row.get("symbol"):
            errors.append("Symbol is required for a split")
        if Decimal(row["quantity"]) <= 0:
            errors.append("Split ratio must be greater than zero")
    if row["type"] in ("dividend", "fee", "deposit", "withdrawal"):
        if row.get("cash_amount") is None:
            errors.append("Cash amount is required for cash transactions")
        elif Decimal(row["cash_amount"]) <= 0:
            errors.append("Cash amount must be greater than zero")
    if row.get("account_id") is None:
        errors.append("Account does not exist")
    return errors


def add_transaction(connection, row: dict) -> bool:
    fingerprint = transaction_fingerprint(row)
    cursor = connection.execute(
        """INSERT OR IGNORE INTO transactions
           (account_id, symbol, isin, type, transaction_date, quantity, price, currency,
            fees, cash_amount, notes, source_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["account_id"], row.get("symbol") or None, row.get("isin") or None, row["type"],
            row["transaction_date"], str(row["quantity"]), str(row["price"]),
            row["currency"], str(row["fees"]),
            str(row["cash_amount"]) if row.get("cash_amount") is not None else None,
            row.get("notes", ""), fingerprint,
        ),
    )
    if cursor.rowcount == 1 and row.get("symbol"):
        connection.execute(
            "INSERT OR IGNORE INTO securities(symbol, currency) VALUES (?, ?)",
            (row["symbol"].upper(), row["currency"]),
        )
    return cursor.rowcount == 1


def apply_ledger_row(row, holdings: dict, cash: dict) -> None:
    currency = row["currency"]
    quantity = Decimal(row["quantity"])
    price = Decimal(row["price"])
    fees = Decimal(row["fees"])
    amount = Decimal(row["cash_amount"]) if row["cash_amount"] is not None else Decimal("0")
    if row["type"] in ("buy", "sell", "split") and row["symbol"]:
        key = (row["symbol"].upper(), currency)
        current = holdings.get(key, Decimal("0"))
        if row["type"] == "buy":
            holdings[key] = current + quantity
            cash_key = (row["account_id"], currency)
            cash[cash_key] = cash.get(cash_key, Decimal("0")) - quantity * price - fees
        elif row["type"] == "sell":
            holdings[key] = current - quantity
            cash_key = (row["account_id"], currency)
            cash[cash_key] = cash.get(cash_key, Decimal("0")) + quantity * price - fees
        else:
            holdings[key] = current * quantity
    elif row["type"] == "deposit":
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) + amount
    elif row["type"] == "withdrawal":
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) - abs(amount)
    elif row["type"] == "dividend":
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) + amount - fees
    elif row["type"] == "fee":
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) - abs(amount)


def ledger(connection, account_id: int | None = None) -> tuple[dict, dict]:
    query = "SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON a.id=t.account_id"
    parameters: tuple = ()
    if account_id is not None:
        query += " WHERE t.account_id=?"
        parameters = (account_id,)
    query += " ORDER BY t.transaction_date, t.id"
    rows = connection.execute(query, parameters).fetchall()
    holdings: dict[tuple[str, str], Decimal] = {}
    cash: dict[tuple[int, str], Decimal] = {}
    for row in rows:
        apply_ledger_row(row, holdings, cash)
    return holdings, cash


def latest_price(connection, symbol: str):
    return connection.execute(
        """SELECT * FROM price_observations
           WHERE symbol=? AND interval='quote' ORDER BY observed_at DESC LIMIT 1""",
        (symbol,),
    ).fetchone()


def latest_fx(connection, base_currency: str, quote_currency: str):
    if base_currency == quote_currency:
        return Decimal("1")
    row = connection.execute(
        """SELECT rate FROM fx_observations
           WHERE base_currency=? AND quote_currency=? ORDER BY observed_at DESC LIMIT 1""",
        (base_currency, quote_currency),
    ).fetchone()
    return Decimal(row["rate"]) if row else None


def save_quote(connection, symbol: str, price: Decimal, currency: str, observed_at: str) -> None:
    connection.execute(
        """INSERT OR REPLACE INTO price_observations
           (symbol, currency, price, observed_at, provider, interval)
           VALUES (?, ?, ?, ?, ?, 'quote')""",
        (symbol.upper(), currency, str(price), observed_at, PROVIDER.name),
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "database": str(database.DB_PATH)}


@app.get("/api/accounts")
def list_accounts(connection=Depends(get_connection)):
    return [dict(row) for row in connection.execute("SELECT * FROM accounts ORDER BY name")]


@app.post("/api/accounts", status_code=201)
def create_account(payload: AccountInput, connection=Depends(get_connection)):
    try:
        cursor = connection.execute(
            "INSERT INTO accounts(name, broker, base_currency) VALUES (?, ?, ?)",
            (payload.name, payload.broker, payload.base_currency),
        )
        connection.commit()
    except Exception as error:
        if "UNIQUE constraint failed" in str(error):
            raise HTTPException(status_code=409, detail="An account with this name already exists")
        raise
    return dict(connection.execute("SELECT * FROM accounts WHERE id=?", (cursor.lastrowid,)).fetchone())


@app.delete("/api/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, connection=Depends(get_connection)):
    try:
        cursor = connection.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        connection.commit()
    except Exception as error:
        if "FOREIGN KEY constraint failed" in str(error):
            raise HTTPException(status_code=409, detail="Account has transactions and cannot be deleted")
        raise
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Account not found")


@app.get("/api/securities")
def list_securities(connection=Depends(get_connection)):
    return [dict(row) for row in connection.execute("SELECT * FROM securities ORDER BY symbol")]


@app.post("/api/securities", status_code=201)
def create_security(payload: SecurityInput, connection=Depends(get_connection)):
    symbol = payload.symbol.upper()
    currency = payload.currency.upper()
    try:
        connection.execute(
            "INSERT INTO securities(symbol, name, currency) VALUES (?, ?, ?)",
            (symbol, payload.name, currency),
        )
        connection.commit()
    except Exception as error:
        if "UNIQUE constraint failed" in str(error):
            raise HTTPException(status_code=409, detail="Security already exists")
        raise
    return dict(connection.execute("SELECT * FROM securities WHERE symbol=?", (symbol,)).fetchone())


@app.get("/api/transactions")
def list_transactions(account_id: int | None = None, connection=Depends(get_connection)):
    query = "SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON a.id=t.account_id"
    parameters: tuple = ()
    if account_id is not None:
        query += " WHERE t.account_id=?"
        parameters = (account_id,)
    query += " ORDER BY t.transaction_date DESC, t.id DESC LIMIT 500"
    return [dict(row) for row in connection.execute(query, parameters)]


@app.post("/api/transactions", status_code=201)
def create_transaction(payload: TransactionInput, connection=Depends(get_connection)):
    account = connection.execute("SELECT id FROM accounts WHERE id=?", (payload.account_id,)).fetchone()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    row = payload.model_dump(mode="json")
    row["transaction_date"] = payload.transaction_date.isoformat()
    row["currency"] = payload.currency.upper()
    row["symbol"] = payload.symbol.upper() if payload.symbol else None
    errors = validate_transaction(row)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    if not add_transaction(connection, row):
        raise HTTPException(status_code=409, detail="Duplicate transaction")
    connection.commit()
    fingerprint = transaction_fingerprint(row)
    return dict(connection.execute("SELECT * FROM transactions WHERE source_hash=?", (fingerprint,)).fetchone())


@app.patch("/api/transactions/{transaction_id}")
def update_transaction(transaction_id: int, payload: TransactionInput, connection=Depends(get_connection)):
    existing = connection.execute("SELECT id FROM transactions WHERE id=?", (transaction_id,)).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    account = connection.execute("SELECT id FROM accounts WHERE id=?", (payload.account_id,)).fetchone()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    row = payload.model_dump(mode="json")
    row["transaction_date"] = payload.transaction_date.isoformat()
    row["currency"] = payload.currency.upper()
    row["symbol"] = payload.symbol.upper() if payload.symbol else None
    errors = validate_transaction(row)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    fingerprint = transaction_fingerprint(row)
    duplicate = connection.execute(
        "SELECT id FROM transactions WHERE source_hash=? AND id<>?", (fingerprint, transaction_id)
    ).fetchone()
    if duplicate:
        raise HTTPException(status_code=409, detail="Duplicate transaction")
    connection.execute(
        """UPDATE transactions SET account_id=?, symbol=?, isin=?, type=?, transaction_date=?,
           quantity=?, price=?, currency=?, fees=?, cash_amount=?, notes=?, source_hash=?
           WHERE id=?""",
        (
            row["account_id"], row["symbol"], row.get("isin") or None, row["type"], row["transaction_date"],
            str(row["quantity"]), str(row["price"]), row["currency"], str(row["fees"]),
            str(row["cash_amount"]) if row["cash_amount"] is not None else None,
            row["notes"], fingerprint, transaction_id,
        ),
    )
    if row["symbol"]:
        connection.execute(
            "INSERT OR IGNORE INTO securities(symbol, currency) VALUES (?, ?)",
            (row["symbol"], row["currency"]),
        )
    connection.commit()
    return dict(connection.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone())


@app.delete("/api/transactions/{transaction_id}", status_code=204)
def delete_transaction(transaction_id: int, connection=Depends(get_connection)):
    cursor = connection.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Transaction not found")
    connection.commit()


@app.get("/api/portfolio/holdings")
def get_holdings(account_id: int | None = None, connection=Depends(get_connection)):
    holdings, _ = ledger(connection, account_id)
    result = []
    for (symbol, currency), quantity in sorted(holdings.items()):
        if quantity == 0:
            continue
        quote = latest_price(connection, symbol)
        result.append({
            "symbol": symbol,
            "currency": currency,
            "quantity": str(quantity),
            "price": quote["price"] if quote else None,
            "price_currency": quote["currency"] if quote else None,
            "price_observed_at": quote["observed_at"] if quote else None,
            "market_value": str(quantity * Decimal(quote["price"])) if quote else None,
        })
    return result


@app.get("/api/portfolio/summary")
def portfolio_summary(account_id: int | None = None, connection=Depends(get_connection)):
    holdings, cash = ledger(connection, account_id)
    setting = connection.execute("SELECT value FROM settings WHERE key='reporting_currency'").fetchone()
    reporting_currency = setting["value"] if setting else "USD"
    positions_total = Decimal("0")
    cash_total = Decimal("0")
    unpriced = 0
    for (symbol, _), quantity in holdings.items():
        if quantity == 0:
            continue
        quote = latest_price(connection, symbol)
        if not quote:
            unpriced += 1
            continue
        rate = latest_fx(connection, quote["currency"], reporting_currency)
        if rate is None:
            unpriced += 1
            continue
        positions_total += quantity * Decimal(quote["price"]) * rate
    for (_, currency), amount in cash.items():
        rate = latest_fx(connection, currency, reporting_currency)
        if rate is None:
            if amount:
                unpriced += 1
            continue
        cash_total += amount * rate
    return {
        "reporting_currency": reporting_currency,
        "positions_value": str(positions_total),
        "cash_balance": str(cash_total),
        "total_value": str(positions_total + cash_total),
        "unpriced_positions": unpriced,
    }


def history_start_date(period: str, today: date, first_event: date | None = None) -> date:
    if period == "1d":
        return today
    if period == "5d":
        return today - timedelta(days=7)
    if period == "1mo":
        return today - timedelta(days=31)
    if period == "3mo":
        return today - timedelta(days=92)
    if period == "6mo":
        return today - timedelta(days=184)
    if period == "ytd":
        return date(today.year, 1, 1)
    if period == "1y":
        return today - timedelta(days=365)
    if period == "2y":
        return today - timedelta(days=730)
    if period == "5y":
        return today - timedelta(days=1826)
    return first_event or date.min


def history_cache_is_usable(
    points: list[dict], period: str, start_date: date, today: date, max_coverage: bool = False
) -> bool:
    if not points:
        return False
    dates = [date.fromisoformat(point["date"][:10]) for point in points]
    if max(dates) < today - timedelta(days=3):
        return False
    if period == "1d":
        intraday = [datetime.fromisoformat(point["date"]) for point in points if len(point["date"]) > 10]
        if (
            len(intraday) < 3
            or max(intraday) - min(intraday) < timedelta(minutes=10)
            or max(intraday).date() < today - timedelta(days=3)
        ):
            return False
    if period == "max" and not max_coverage:
        return False
    return max_coverage or min(dates) <= start_date + timedelta(days=3)


@app.get("/api/portfolio/history")
def portfolio_history(
    period: Literal["1d", "5d", "1mo", "ytd", "1y", "max"] = "1mo",
    account_id: int | None = None,
    connection=Depends(get_connection),
):
    query = "SELECT * FROM transactions"
    parameters: tuple = ()
    if account_id is not None:
        query += " WHERE account_id=?"
        parameters = (account_id,)
    query += " ORDER BY transaction_date, id"
    transactions = connection.execute(query, parameters).fetchall()
    setting = connection.execute("SELECT value FROM settings WHERE key='reporting_currency'").fetchone()
    reporting_currency = setting["value"] if setting else "USD"
    symbols = sorted({row["symbol"].upper() for row in transactions if row["symbol"] and row["type"] in ("buy", "sell", "split")})
    price_series: dict[str, list[tuple[str, Decimal]]] = {}
    price_currencies: dict[str, str] = {}
    errors = []
    today = datetime.now(timezone.utc).date()
    for symbol in symbols:
        symbol_events = [
            date.fromisoformat(row["transaction_date"][:10])
            for row in transactions if row["symbol"] and row["symbol"].upper() == symbol
        ]
        start_date = history_start_date(period, today, min(symbol_events, default=None))
        cached_rows = connection.execute(
            """SELECT observed_at, price, currency, interval FROM price_observations
               WHERE symbol=? AND provider=? AND interval LIKE 'history:%' ORDER BY observed_at""",
            (symbol, PROVIDER.name),
        ).fetchall()
        points = [{"date": row["observed_at"], "close": row["price"]} for row in cached_rows]
        cached_currencies = {row["currency"] for row in cached_rows}
        has_max_coverage = any(row["interval"] == "history:max" for row in cached_rows)
        if not history_cache_is_usable(points, period, start_date, today, has_max_coverage) or len(cached_currencies) != 1:
            try:
                currency, points = PROVIDER.history(symbol, period)
            except Exception as error:
                errors.append(f"{symbol}: {error}")
                continue
            currency = currency.upper()
            connection.executemany(
                """INSERT OR REPLACE INTO price_observations
                   (symbol, currency, price, observed_at, provider, interval)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(symbol, currency, point["close"], point["date"], PROVIDER.name, f"history:{period}") for point in points],
            )
        else:
            currency = cached_currencies.pop()
        points = [
            point for point in points
            if (len(point["date"]) > 10) == (period == "1d")
        ]
        price_currencies[symbol] = currency.upper()
        price_series[symbol] = sorted((point["date"], Decimal(point["close"])) for point in points)

    currencies = set(price_currencies.values()) | {row["currency"] for row in transactions}
    fx_series: dict[str, list[tuple[str, Decimal]]] = {}
    for currency in sorted(currencies - {reporting_currency}):
        start_date = history_start_date(
            period, today, min((date.fromisoformat(row["transaction_date"][:10]) for row in transactions), default=None)
        )
        cached_rows = connection.execute(
            """SELECT observed_at, rate, provider FROM fx_observations
               WHERE base_currency=? AND quote_currency=? AND provider LIKE ? ORDER BY observed_at""",
            (currency, reporting_currency, f"{PROVIDER.name}:history:%"),
        ).fetchall()
        points = [{"date": row["observed_at"], "close": row["rate"]} for row in cached_rows]
        has_max_coverage = any(row["provider"].endswith(":history:max") for row in cached_rows)
        if not history_cache_is_usable(points, period, start_date, today, has_max_coverage):
            try:
                _, points = PROVIDER.history(f"{currency}{reporting_currency}=X", period)
            except Exception as error:
                errors.append(f"{currency}/{reporting_currency}: {error}")
                continue
            connection.executemany(
                """INSERT OR REPLACE INTO fx_observations
                   (base_currency, quote_currency, rate, observed_at, provider)
                   VALUES (?, ?, ?, ?, ?)""",
                    [(currency, reporting_currency, point["close"], point["date"], f"{PROVIDER.name}:history:{period}") for point in points],
            )
        points = [
            point for point in points
            if (len(point["date"]) > 10) == (period == "1d")
        ]
        fx_series[currency] = sorted((point["date"], Decimal(point["close"])) for point in points)
    if errors:
        connection.commit()
        raise HTTPException(status_code=503, detail=f"Historical data unavailable: {'; '.join(errors)}")

    start_date = history_start_date(period, today)
    dates = {point_date for series in price_series.values() for point_date, _ in series}
    dates.update(
        row["transaction_date"][:10] for row in transactions
        if date.fromisoformat(row["transaction_date"][:10]) >= start_date
    )
    dates = sorted(dates)
    holdings: dict[tuple[str, str], Decimal] = {}
    cash: dict[tuple[int, str], Decimal] = {}
    contributions: dict[tuple[int, str], Decimal] = {}
    transaction_index = 0
    price_indexes = {symbol: 0 for symbol in price_series}
    fx_indexes = {currency: 0 for currency in fx_series}
    latest_prices: dict[str, Decimal] = {}
    latest_fx_rates: dict[str, Decimal] = {}
    result = []
    for point_date in dates:
        date_only = point_date[:10]
        while transaction_index < len(transactions) and transactions[transaction_index]["transaction_date"][:10] <= date_only:
            transaction = transactions[transaction_index]
            apply_ledger_row(transaction, holdings, cash)
            if transaction["type"] in ("deposit", "withdrawal"):
                contribution_key = (transaction["account_id"], transaction["currency"])
                contribution = Decimal(transaction["cash_amount"])
                if transaction["type"] == "withdrawal":
                    contribution = -abs(contribution)
                contributions[contribution_key] = contributions.get(contribution_key, Decimal("0")) + contribution
            transaction_index += 1
        for symbol, series in price_series.items():
            while price_indexes[symbol] < len(series) and series[price_indexes[symbol]][0] <= point_date:
                latest_prices[symbol] = series[price_indexes[symbol]][1]
                price_indexes[symbol] += 1
        for currency, series in fx_series.items():
            while fx_indexes[currency] < len(series) and series[fx_indexes[currency]][0] <= point_date:
                latest_fx_rates[currency] = series[fx_indexes[currency]][1]
                fx_indexes[currency] += 1

        total = Decimal("0")
        priced = True
        for (symbol, _), quantity in holdings.items():
            if not quantity:
                continue
            price = latest_prices.get(symbol)
            currency = price_currencies.get(symbol)
            rate = Decimal("1") if currency == reporting_currency else latest_fx_rates.get(currency or "")
            if price is None or rate is None:
                priced = False
                break
            total += quantity * price * rate
        if not priced:
            continue
        for (_, currency), amount in cash.items():
            rate = Decimal("1") if currency == reporting_currency else latest_fx_rates.get(currency)
            if rate is None and amount:
                priced = False
                break
            if rate is not None:
                total += amount * rate
        invested = Decimal("0")
        for (_, currency), amount in contributions.items():
            rate = Decimal("1") if currency == reporting_currency else latest_fx_rates.get(currency)
            if rate is None and amount:
                priced = False
                break
            if rate is not None:
                invested += amount * rate
        if priced and date.fromisoformat(date_only) >= start_date:
            roi_amount = total - invested if invested > 0 else None
            roi = roi_amount * Decimal("100") / invested if roi_amount is not None else None
            result.append({
                "date": point_date,
                "value": str(total),
                "roi": str(roi) if roi is not None else None,
                "roi_amount": str(roi_amount) if roi_amount is not None else None,
            })

    connection.commit()
    return {"period": period, "reporting_currency": reporting_currency, "points": result}


@app.put("/api/settings")
def update_settings(reporting_currency: str, connection=Depends(get_connection)):
    if len(reporting_currency) != 3 or not reporting_currency.isalpha():
        raise HTTPException(status_code=422, detail="Currency must be a 3-letter code")
    currency = reporting_currency.upper()
    connection.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('reporting_currency', ?)",
        (currency,),
    )
    connection.commit()
    return {"reporting_currency": currency}


@app.post("/api/imports/csv/preview")
async def preview_csv(file: UploadFile = File(...), field_map: str = Form("{}"), connection=Depends(get_connection)):
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="CSV must be 2 MB or smaller")
    try:
        mapping = json.loads(field_map)
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="CSV or field mapping could not be read")
    required = {"date", "account", "type", "currency"}
    fields = ("date", "account", "symbol", "isin", "type", "quantity", "price", "currency", "fees", "cash_amount", "notes")
    mapped = {key: mapping.get(key, key) for key in fields}
    missing = sorted(key for key in required if mapped[key] not in (reader.fieldnames or []))
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required CSV fields: {', '.join(missing)}")
    account_ids = {row["name"]: row["id"] for row in connection.execute("SELECT id, name FROM accounts")}
    preview_rows = []
    seen_hashes: set[str] = set()
    for line_number, source in enumerate(reader, start=2):
        try:
            account_name = (source.get(mapped["account"]) or "").strip()
            source_date = (source.get(mapped["date"]) or "").strip()
            parsed_date = (
                date.fromisoformat(source_date).isoformat()
                if len(source_date) == 10
                else datetime.fromisoformat(source_date).isoformat()
            )
            transaction_type = (source.get(mapped["type"]) or "").strip().lower()
            cash_value = (source.get(mapped["cash_amount"]) or "").strip()
            row = {
                "account_id": account_ids.get(account_name), "account_name": account_name,
                "transaction_date": parsed_date, "type": transaction_type,
                "symbol": (source.get(mapped["symbol"]) or "").strip().upper() or None,
                "isin": (source.get(mapped["isin"]) or "").strip().upper() or None,
                "quantity": str(Decimal((source.get(mapped["quantity"]) or "0").strip())),
                "price": str(Decimal((source.get(mapped["price"]) or "0").strip())),
                "currency": (source.get(mapped["currency"]) or "").strip().upper(),
                "fees": str(Decimal((source.get(mapped["fees"]) or "0").strip())),
                "cash_amount": str(Decimal(cash_value)) if cash_value else None,
                "notes": (source.get(mapped["notes"]) or "").strip(),
            }
            if transaction_type not in ("buy", "sell", "dividend", "fee", "split", "deposit", "withdrawal"):
                raise ValueError("Type must be buy, sell, dividend, fee, split, deposit, or withdrawal")
            errors = validate_transaction(row)
            fingerprint = transaction_fingerprint(row)
            exists = connection.execute("SELECT 1 FROM transactions WHERE source_hash=?", (fingerprint,)).fetchone()
            if exists or fingerprint in seen_hashes:
                errors.append("Duplicate transaction")
            seen_hashes.add(fingerprint)
            row.update({"line": line_number, "valid": not errors, "errors": errors})
            preview_rows.append(row)
        except (ValueError, InvalidOperation, KeyError) as error:
            preview_rows.append({"line": line_number, "valid": False, "errors": [str(error) or "Invalid row"]})
    batch_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO import_batches(id, filename, status, preview_json) VALUES (?, ?, 'preview', ?)",
        (batch_id, Path(file.filename or "transactions.csv").name, json.dumps(preview_rows)),
    )
    connection.commit()
    return {
        "id": batch_id, "filename": Path(file.filename or "transactions.csv").name,
        "total_rows": len(preview_rows), "valid_rows": sum(row["valid"] for row in preview_rows),
        "invalid_rows": sum(not row["valid"] for row in preview_rows), "rows": preview_rows,
    }


@app.post("/api/imports/{batch_id}/commit")
def commit_csv(batch_id: str, connection=Depends(get_connection)):
    batch = connection.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    if batch["status"] != "preview":
        raise HTTPException(status_code=409, detail="Import batch has already been committed")
    rows = json.loads(batch["preview_json"])
    committed = sum(add_transaction(connection, row) for row in rows if row.get("valid"))
    connection.execute("UPDATE import_batches SET status='committed', imported_count=? WHERE id=?", (committed, batch_id))
    connection.commit()
    return {"id": batch_id, "imported_count": committed, "skipped_count": len(rows) - committed}


@app.get("/api/imports")
def list_imports(connection=Depends(get_connection)):
    return [dict(row) for row in connection.execute(
        "SELECT id, filename, status, imported_count, created_at FROM import_batches ORDER BY created_at DESC"
    )]


@app.get("/api/prices/{symbol}")
def get_quote(symbol: str, connection=Depends(get_connection)):
    try:
        price, currency, observed_at = PROVIDER.quote(symbol.upper())
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Quote unavailable: {error}")
    save_quote(connection, symbol, price, currency, observed_at)
    connection.commit()
    return {"symbol": symbol.upper(), "price": str(price), "currency": currency,
            "observed_at": observed_at, "provider": PROVIDER.name}


@app.get("/api/prices/{symbol}/history")
def get_price_history(
    symbol: str,
    period: Literal["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"] = "1y",
    connection=Depends(get_connection),
):
    symbol = symbol.upper()
    today = datetime.now(timezone.utc).date()
    start_date = history_start_date(period, today)
    cached_rows = connection.execute(
        """SELECT observed_at, price, currency, interval FROM price_observations
           WHERE symbol=? AND provider=? AND interval LIKE 'history:%' ORDER BY observed_at""",
        (symbol, PROVIDER.name),
    ).fetchall()
    points = [{"date": row["observed_at"], "close": row["price"]} for row in cached_rows]
    currencies = {row["currency"] for row in cached_rows}
    has_max_coverage = any(row["interval"] == "history:max" for row in cached_rows)
    if not history_cache_is_usable(points, period, start_date, today, has_max_coverage) or len(currencies) != 1:
        try:
            currency, points = PROVIDER.history(symbol, period)
        except Exception as error:
            raise HTTPException(status_code=503, detail=f"Price history unavailable: {error}")
        connection.executemany(
            """INSERT OR REPLACE INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(symbol, currency, point["close"], point["date"], PROVIDER.name, f"history:{period}") for point in points],
        )
        connection.commit()
    else:
        currency = currencies.pop()
    points = [point for point in points if date.fromisoformat(point["date"][:10]) >= start_date]
    return {"symbol": symbol, "currency": currency, "provider": PROVIDER.name, "points": points}


@app.get("/api/fx/{base_currency}/{quote_currency}/current")
def get_fx_rate(base_currency: str, quote_currency: str, connection=Depends(get_connection)):
    base, quote = base_currency.upper(), quote_currency.upper()
    try:
        rate, observed_at = PROVIDER.fx_rate(base, quote)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"FX rate unavailable: {error}")
    connection.execute(
        """INSERT OR REPLACE INTO fx_observations
           (base_currency, quote_currency, rate, observed_at, provider) VALUES (?, ?, ?, ?, ?)""",
        (base, quote, str(rate), observed_at, PROVIDER.name),
    )
    connection.commit()
    return {"base_currency": base, "quote_currency": quote, "rate": str(rate),
            "observed_at": observed_at, "provider": PROVIDER.name}


@app.get("/api/fx/{base_currency}/{quote_currency}/history")
def get_fx_history(
    base_currency: str,
    quote_currency: str,
    period: Literal["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"] = "1y",
    connection=Depends(get_connection),
):
    base, quote = base_currency.upper(), quote_currency.upper()
    if base == quote:
        return {"base_currency": base, "quote_currency": quote, "provider": "identity", "points": []}
    today = datetime.now(timezone.utc).date()
    start_date = history_start_date(period, today)
    cached_rows = connection.execute(
        """SELECT observed_at, rate, provider FROM fx_observations
           WHERE base_currency=? AND quote_currency=? AND provider LIKE ? ORDER BY observed_at""",
        (base, quote, f"{PROVIDER.name}:history:%"),
    ).fetchall()
    points = [{"date": row["observed_at"], "rate": row["rate"]} for row in cached_rows]
    has_max_coverage = any(row["provider"].endswith(":history:max") for row in cached_rows)
    if not history_cache_is_usable(
        [{"date": point["date"], "close": point["rate"]} for point in points],
        period, start_date, today, has_max_coverage,
    ):
        try:
            _, price_points = PROVIDER.history(f"{base}{quote}=X", period)
        except Exception as error:
            raise HTTPException(status_code=503, detail=f"FX history unavailable: {error}")
        points = [{"date": point["date"], "rate": point["close"]} for point in price_points]
        connection.executemany(
            """INSERT OR REPLACE INTO fx_observations
               (base_currency, quote_currency, rate, observed_at, provider)
                    VALUES (?, ?, ?, ?, ?)""",
                [(base, quote, point["rate"], point["date"], f"{PROVIDER.name}:history:{period}") for point in points],
        )
        connection.commit()
    points = [point for point in points if date.fromisoformat(point["date"][:10]) >= start_date]
    return {"base_currency": base, "quote_currency": quote, "provider": PROVIDER.name, "points": points}


@app.post("/api/portfolio/refresh")
def refresh_portfolio(connection=Depends(get_connection)):
    holdings, cash = ledger(connection)
    symbols = sorted({symbol for (symbol, _), quantity in holdings.items() if quantity})
    currencies = {currency for (_, currency), quantity in holdings.items() if quantity}
    currencies.update(currency for (_, currency), amount in cash.items() if amount)
    setting = connection.execute("SELECT value FROM settings WHERE key='reporting_currency'").fetchone()
    reporting_currency = setting["value"] if setting else "USD"
    currencies.add(reporting_currency)
    errors = []
    refreshed = 0
    for symbol in symbols:
        try:
            price, currency, observed_at = PROVIDER.quote(symbol)
            save_quote(connection, symbol, price, currency, observed_at)
            currencies.add(currency)
            refreshed += 1
        except Exception as error:
            errors.append({"symbol": symbol, "error": str(error)})
    for currency in currencies:
        if currency == reporting_currency:
            continue
        try:
            rate, observed_at = PROVIDER.fx_rate(currency, reporting_currency)
            connection.execute(
                """INSERT OR REPLACE INTO fx_observations
                   (base_currency, quote_currency, rate, observed_at, provider) VALUES (?, ?, ?, ?, ?)""",
                (currency, reporting_currency, str(rate), observed_at, PROVIDER.name),
            )
        except Exception as error:
            errors.append({"currency": currency, "error": str(error)})
    connection.commit()
    return {"refreshed_quotes": refreshed, "errors": errors, "provider": PROVIDER.name,
            "refreshed_at": datetime.now(timezone.utc).isoformat()}


if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        file_path = FRONTEND_DIST / path
        if path and file_path.is_file() and FRONTEND_DIST in file_path.parents:
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIST / "index.html")