import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import database
from . import secret_storage
from .import_formats import (
    CsvTemplateDefinition,
    canonical_definition,
    definition_json,
    normalize_source_row,
    parse_csv,
)
from .market_data import NoPriceHistoryError, OpenFigiProvider, YahooFinanceProvider


PROVIDER = YahooFinanceProvider()
OPENFIGI_PROVIDER = OpenFigiProvider()
ENCRYPTION_FERNET = None
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
SECURITY_SEARCH_CACHE_TTL = timedelta(days=1)
QUOTE_CACHE_TTL = timedelta(minutes=15)
FX_CACHE_TTL = timedelta(hours=6)
INTRADAY_HISTORY_CACHE_TTL = timedelta(minutes=5)
WEEK_HISTORY_RETENTION = timedelta(days=7)
HISTORY_CACHE_TTLS = {
    "1d": INTRADAY_HISTORY_CACHE_TTL,
    "1w-30m": timedelta(minutes=30),
    "1m": timedelta(days=1),
    "3mo": timedelta(days=1),
    "6mo": timedelta(days=1),
    "1y": timedelta(days=1),
    "2y": timedelta(days=1),
    "5y": timedelta(weeks=2),
    "ytd": timedelta(days=1),
    "max": timedelta(weeks=2),
}
HISTORY_CACHE_PERIODS = {"5d": "1w-30m", "1mo": "1m"}


def normalize_transaction_date(value: datetime | str) -> str:
    """Return timestamped transaction dates in UTC while preserving date-only values."""
    if isinstance(value, str):
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        # Naïve API/import values represent local time. astimezone() applies the
        # host's configured local timezone before converting to UTC.
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat()


def is_valid_isin(value: str) -> bool:
    normalized = value.strip().upper()
    if len(normalized) != 12 or not normalized.isascii() or not normalized.isalnum():
        return False
    expanded = "".join(str(int(character, 36)) for character in normalized)
    checksum = 0
    for index, character in enumerate(reversed(expanded)):
        digit = int(character)
        if index % 2:
            digit *= 2
            digit = digit // 10 + digit % 10
        checksum += digit
    return checksum % 10 == 0


def merge_security_candidates(
    yahoo_results: list[dict],
    openfigi_results: list[dict],
) -> list[dict]:
    merged = [dict(result) for result in yahoo_results]
    ticker_mapping_exchanges: dict[str, set[str]] = {}
    for mapping in openfigi_results:
        ticker = str(mapping["symbol"]).casefold()
        exchange = str(mapping.get("exchange") or "").casefold()
        ticker_mapping_exchanges.setdefault(ticker, set()).add(exchange)
    for mapping in openfigi_results:
        ticker = str(mapping["symbol"]).casefold()
        matching_yahoo_results = [
            result for result in merged
            if len(ticker_mapping_exchanges[ticker]) == 1
            and (
                str(result["symbol"]).casefold() == ticker
                or str(result["symbol"]).split(".", maxsplit=1)[0].casefold() == ticker
            )
        ]
        matching_yahoo_symbols = {str(result["symbol"]).casefold() for result in matching_yahoo_results}
        yahoo_match = matching_yahoo_results[0] if len(matching_yahoo_symbols) == 1 else None
        if yahoo_match is None:
            continue

        candidate = dict(mapping)
        candidate.update({
            "symbol": yahoo_match["symbol"],
            "currency": candidate.get("currency") or yahoo_match.get("currency"),
            "yahoo_search_match": True,
            "source": "OpenFIGI and Yahoo Finance",
        })
        key = (str(candidate["symbol"]).casefold(), str(candidate.get("exchange") or "").casefold())
        existing_index = next(
            (
                index for index, result in enumerate(merged)
                if (str(result["symbol"]).casefold(), str(result.get("exchange") or "").casefold()) == key
                or (
                    candidate.get("yahoo_search_match")
                    and str(result["symbol"]).casefold() == key[0]
                )
            ),
            None,
        )
        if existing_index is None:
            merged.append(candidate)
        elif candidate.get("yahoo_search_match"):
            merged[existing_index].update(candidate)
    return merged


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ENCRYPTION_FERNET
    database.initialize()
    with database.connect() as connection:
        existing_key = connection.execute(
            "SELECT value FROM settings WHERE key='openfigi_api_key'"
        ).fetchone()
        has_encrypted_secrets = bool(
            existing_key and existing_key["value"].startswith(secret_storage.ENCRYPTED_VALUE_PREFIX)
        )
    ENCRYPTION_FERNET = secret_storage.load_or_create_fernet(
        database.DB_PATH.parent,
        has_encrypted_secrets=has_encrypted_secrets,
    )
    with database.connect() as connection:
        secret_storage.migrate_openfigi_key(connection, ENCRYPTION_FERNET)
    yield


app = FastAPI(title="Folio Portfolio API", version="0.1.0", lifespan=lifespan)
api_logger = logging.getLogger("uvicorn.error")


@app.middleware("http")
async def log_api_request(request, call_next):
    if request.url.path != "/api" and not request.url.path.startswith("/api/"):
        return await call_next(request)
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as error:
        api_logger.warning(
            "API request failed method=%s path=%s duration_ms=%.2f error_type=%s",
            request.method,
            request.url.path,
            (time.perf_counter() - started_at) * 1000,
            type(error).__name__,
        )
        raise
    api_logger.info(
        "API request completed method=%s path=%s status=%s duration_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - started_at) * 1000,
    )
    return response


def get_connection():
    connection = database.connect()
    try:
        yield connection
    finally:
        connection.close()


class AccountInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)


class SecurityInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=120)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class SettingsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    openfigi_api_key: object | None = None


class TransactionInput(BaseModel):
    account_id: int
    type: Literal["buy", "sell", "dividend", "interest", "fee", "split", "deposit", "withdrawal", "transfer_in", "transfer_out"]
    transaction_date: datetime
    symbol: str | None = Field(default=None, max_length=32)
    security_name: str = Field(default="", max_length=120)
    isin: str | None = None
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    fees: Decimal = Decimal("0")
    taxes: Decimal = Decimal("0")
    cash_amount: Decimal | None = None
    notes: str = Field(default="", max_length=500)

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("isin")
    @classmethod
    def uppercase_isin(cls, value: str | None) -> str | None:
        normalized = value.strip().upper() if value else None
        if not normalized:
            return None
        if len(normalized) != 12:
            raise ValueError("ISIN must contain exactly 12 characters or be empty")
        return normalized


def transaction_fingerprint(row: dict) -> str:
    fields = (
        "account_id", "symbol", "type", "transaction_date", "quantity", "price",
        "currency", "fees", "cash_amount", "notes",
    )
    canonical_row = {field: row.get(field) for field in fields}
    taxes = Decimal(row.get("taxes") or "0")
    if taxes:
        canonical_row["taxes"] = str(taxes)
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
    if row["type"] in ("transfer_in", "transfer_out"):
        if not row.get("symbol"):
            errors.append("Symbol is required for a security transfer")
        if Decimal(row["quantity"]) <= 0:
            errors.append("Transfer quantity must be greater than zero")
        if Decimal(row["price"]) <= 0:
            errors.append("Transfer cost basis per share must be greater than zero")
        if Decimal(row.get("fees") or "0") != 0 or Decimal(row.get("taxes") or "0") != 0:
            errors.append("Record transfer fees or taxes as separate cash transactions")
    if row["type"] in ("dividend", "interest", "fee", "deposit", "withdrawal"):
        if row.get("cash_amount") is None:
            errors.append("Cash amount is required for cash transactions")
        elif row["type"] in ("dividend", "interest") and Decimal(row["cash_amount"]) == 0:
            errors.append(f"{row['type'].capitalize()} cash amount cannot be zero")
        elif row["type"] not in ("dividend", "interest") and Decimal(row["cash_amount"]) <= 0:
            errors.append("Cash amount must be greater than zero")
    if row.get("account_id") is None:
        errors.append("Account does not exist")
    return errors


def validate_import_transaction(row: dict) -> list[str]:
    errors = validate_transaction(row)
    if row.get("type") == "dividend" and (row.get("isin") or row.get("security_name")) and not row.get("symbol"):
        errors.append("Symbol is required for a security distribution")
    return errors


def add_transaction(connection, row: dict) -> bool:
    fingerprint = transaction_fingerprint(row)
    cursor = connection.execute(
        """INSERT OR IGNORE INTO transactions
           (account_id, symbol, isin, type, transaction_date, quantity, price, currency,
                fees, taxes, cash_amount, notes, source_hash)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["account_id"], row.get("symbol") or None, row.get("isin") or None, row["type"],
            row["transaction_date"], str(row["quantity"]), str(row["price"]),
            row["currency"], str(row["fees"]), str(row.get("taxes", "0")),
            str(row["cash_amount"]) if row.get("cash_amount") is not None else None,
            row.get("notes", ""), fingerprint,
        ),
    )
    if cursor.rowcount == 1 and row.get("symbol"):
        save_security(connection, row)
    return cursor.rowcount == 1


def save_security(connection, row: dict) -> None:
    name = (row.get("security_name") or "").strip()
    connection.execute(
        """INSERT INTO securities(symbol, name, currency) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
               name=CASE WHEN excluded.name <> '' THEN excluded.name ELSE securities.name END,
               currency=excluded.currency""",
        (row["symbol"].upper(), name, row["currency"]),
    )


def apply_ledger_row(row, holdings: dict, cash: dict) -> None:
    currency = row["currency"]
    quantity = Decimal(row["quantity"])
    price = Decimal(row["price"])
    fees = Decimal(row["fees"])
    taxes = Decimal(row["taxes"])
    amount = Decimal(row["cash_amount"]) if row["cash_amount"] is not None else Decimal("0")
    if row["type"] in ("buy", "sell", "split", "transfer_in", "transfer_out") and row["symbol"]:
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
        elif row["type"] == "split":
            holdings[key] = current * quantity
        elif row["type"] == "transfer_in":
            holdings[key] = current + quantity
        else:
            holdings[key] = current - quantity
    elif row["type"] == "deposit":
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) + amount
    elif row["type"] == "withdrawal":
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) - abs(amount)
    elif row["type"] in ("dividend", "interest"):
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) + amount - fees
    elif row["type"] == "fee":
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) - abs(amount)
    if taxes:
        cash_key = (row["account_id"], currency)
        cash[cash_key] = cash.get(cash_key, Decimal("0")) - taxes


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


def security_performance(connection, account_id: int | None = None) -> tuple[dict, dict, dict, dict]:
    query = "SELECT symbol, currency, type, quantity, price, fees, cash_amount FROM transactions"
    parameters: tuple = ()
    if account_id is not None:
        query += " WHERE account_id=?"
        parameters = (account_id,)
    query += " ORDER BY transaction_date, id"
    rows = connection.execute(query, parameters).fetchall()
    quantities: dict[tuple[str, str], Decimal] = {}
    cost_bases: dict[tuple[str, str], Decimal] = {}
    realized_pl: dict[tuple[str, str], Decimal] = {}
    dividends: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        if not row["symbol"]:
            continue
        key = (row["symbol"].upper(), row["currency"])
        if row["type"] == "dividend":
            dividends[key] = dividends.get(key, Decimal("0")) + Decimal(row["cash_amount"] or "0") - Decimal(row["fees"])
            continue
        if row["type"] in ("transfer_in", "transfer_out"):
            quantity = Decimal(row["quantity"])
            carrying_cost = quantity * Decimal(row["price"])
            if row["type"] == "transfer_in":
                quantities[key] = quantities.get(key, Decimal("0")) + quantity
                cost_bases[key] = cost_bases.get(key, Decimal("0")) + carrying_cost
            else:
                quantities[key] = quantities.get(key, Decimal("0")) - quantity
                cost_bases[key] = cost_bases.get(key, Decimal("0")) - carrying_cost
            continue
        if row["type"] not in ("buy", "sell", "split"):
            continue
        quantity = Decimal(row["quantity"])
        current_quantity = quantities.get(key, Decimal("0"))
        cost_basis = cost_bases.get(key, Decimal("0"))
        price = Decimal(row["price"])
        fees = Decimal(row["fees"])
        if row["type"] == "buy":
            if current_quantity < 0:
                covered_quantity = min(quantity, -current_quantity)
                remaining_quantity = quantity - covered_quantity
                allocated_proceeds = cost_basis * covered_quantity / -current_quantity
                allocated_cost = covered_quantity * price + fees * covered_quantity / quantity
                realized_pl[key] = realized_pl.get(key, Decimal("0")) + allocated_proceeds - allocated_cost
                remaining_short_basis = cost_basis - allocated_proceeds
                remaining_long_basis = remaining_quantity * price + fees * remaining_quantity / quantity
                cost_bases[key] = remaining_short_basis + remaining_long_basis
            else:
                cost_bases[key] = cost_basis + quantity * price + fees
            quantities[key] = current_quantity + quantity
        elif row["type"] == "sell":
            long_quantity_sold = min(current_quantity, quantity) if current_quantity > 0 else Decimal("0")
            short_quantity_opened = quantity - long_quantity_sold
            if long_quantity_sold > 0:
                allocated_cost = cost_basis * long_quantity_sold / current_quantity
                allocated_fees = fees * long_quantity_sold / quantity
                realized_pl[key] = realized_pl.get(key, Decimal("0")) + long_quantity_sold * price - allocated_cost - allocated_fees
                remaining_quantity = max(current_quantity - quantity, Decimal("0"))
                cost_bases[key] = (
                    cost_basis * remaining_quantity / current_quantity
                    if not short_quantity_opened
                    else short_quantity_opened * price - fees * short_quantity_opened / quantity
                )
            else:
                cost_bases[key] = cost_basis + quantity * price - fees
            quantities[key] = current_quantity - quantity
        elif row["type"] == "split":
            quantities[key] = current_quantity * quantity
        else:
            quantities[key] = current_quantity
    unit_costs = {
        key: cost_bases[key] / abs(quantity)
        for key, quantity in quantities.items()
        if quantity != 0 and cost_bases.get(key, Decimal("0")) > 0
    }
    average_costs = {
        key: unit_costs[key]
        for key, quantity in quantities.items()
        if quantity > 0 and key in unit_costs
    }
    return average_costs, unit_costs, realized_pl, dividends


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


def observation_is_fresh(observed_at: str, ttl: timedelta) -> bool:
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
    return timedelta(0) <= age < ttl


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
            "INSERT INTO accounts(name) VALUES (?)",
            (payload.name,),
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


@app.get("/api/securities/search")
def search_securities(q: str = Query(min_length=2, max_length=80), connection=Depends(get_connection)):
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="Search query must contain at least 2 characters")
    normalized_query = query.upper() if is_valid_isin(query) else query
    cache_key = (
        f"openfigi:v3:{normalized_query.casefold()}"
        if is_valid_isin(normalized_query)
        else normalized_query.casefold()
    )
    cached = connection.execute(
        "SELECT results_json, cached_at FROM security_search_cache WHERE query=?", (cache_key,)
    ).fetchone()
    if cached and observation_is_fresh(cached["cached_at"], SECURITY_SEARCH_CACHE_TTL):
        try:
            cached_results = json.loads(cached["results_json"])
        except json.JSONDecodeError:
            cached_results = None
        if isinstance(cached_results, list) and all(isinstance(item, dict) for item in cached_results):
            for result in cached_results:
                result.setdefault("yahoo_search_match", True)
                result.setdefault("source", "Yahoo Finance")
            return {"results": cached_results, "warnings": []}
        connection.execute("DELETE FROM security_search_cache WHERE query=?", (cache_key,))
        connection.commit()

    warnings = []
    yahoo_results = []
    yahoo_error = None
    isin = normalized_query if is_valid_isin(normalized_query) else None
    if not isin:
        try:
            yahoo_results = PROVIDER.search(query)
        except Exception as error:
            yahoo_error = error

    results = yahoo_results
    api_key_setting = connection.execute(
        "SELECT value FROM settings WHERE key='openfigi_api_key'"
    ).fetchone()
    if isin:
        try:
            api_key = None
            if api_key_setting:
                if ENCRYPTION_FERNET is None:
                    raise RuntimeError("Encryption key is not initialized")
                api_key = secret_storage.decrypt_secret(ENCRYPTION_FERNET, api_key_setting["value"])
            openfigi_results = OPENFIGI_PROVIDER.search_isin(isin, api_key)
            additional_yahoo_results = []
            yahoo_lookup_failed = False
            for ticker in dict.fromkeys(
                str(mapping["symbol"]).strip().upper()
                for mapping in openfigi_results
                if mapping.get("symbol")
            ):
                try:
                    ticker_results = PROVIDER.search(ticker)
                except Exception:
                    yahoo_lookup_failed = True
                    continue
                additional_yahoo_results.extend(
                    result for result in ticker_results
                    if str(result["symbol"]).casefold() == ticker.casefold()
                    or str(result["symbol"]).split(".", maxsplit=1)[0].casefold() == ticker.casefold()
                )
            if yahoo_lookup_failed:
                warnings.append(
                    "Some OpenFIGI-mapped tickers could not be checked against Yahoo Finance."
                )
            yahoo_results.extend(additional_yahoo_results)
            results = merge_security_candidates(yahoo_results, openfigi_results)
        except Exception as error:
            if getattr(error, "code", None) == 429:
                warnings.append(
                    "OpenFIGI rate limit reached; ISIN mappings could not be loaded. "
                    "An API key provides a higher rate limit—configure one in Settings if you have not already."
                )
            else:
                warnings.append("OpenFIGI mappings could not be loaded; Yahoo Finance could not validate mapped tickers.")
    if yahoo_error is not None:
        if not results:
            raise HTTPException(status_code=502, detail="Security search is unavailable") from yahoo_error
        warnings.append("Yahoo Finance results could not be loaded; showing available OpenFIGI mappings.")

    if warnings and not results and yahoo_error is not None:
        raise HTTPException(status_code=502, detail="Security search is unavailable") from yahoo_error

    for result in results:
        result.setdefault("yahoo_search_match", True)
        result.setdefault("source", "Yahoo Finance")

    cached_at = datetime.now(timezone.utc).isoformat()
    if not warnings:
        connection.execute("DELETE FROM security_search_cache WHERE cached_at < ?", (
            (datetime.now(timezone.utc) - SECURITY_SEARCH_CACHE_TTL).isoformat(),
        ))
        connection.execute(
            """INSERT INTO security_search_cache(query, results_json, cached_at) VALUES (?, ?, ?)
               ON CONFLICT(query) DO UPDATE SET results_json=excluded.results_json, cached_at=excluded.cached_at""",
            (cache_key, json.dumps(results), cached_at),
        )
        connection.commit()
    return {"results": results, "warnings": warnings}


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
    row["transaction_date"] = normalize_transaction_date(payload.transaction_date)
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
    row["transaction_date"] = normalize_transaction_date(payload.transaction_date)
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
              quantity=?, price=?, currency=?, fees=?, taxes=?, cash_amount=?, notes=?, source_hash=?
           WHERE id=?""",
        (
            row["account_id"], row["symbol"], row.get("isin") or None, row["type"], row["transaction_date"],
            str(row["quantity"]), str(row["price"]), row["currency"], str(row["fees"]), str(row["taxes"]),
            str(row["cash_amount"]) if row["cash_amount"] is not None else None,
            row["notes"], fingerprint, transaction_id,
        ),
    )
    if row["symbol"]:
        save_security(connection, row)
    connection.commit()
    return dict(connection.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone())


@app.delete("/api/transactions/{transaction_id}", status_code=204)
def delete_transaction(transaction_id: int, connection=Depends(get_connection)):
    cursor = connection.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Transaction not found")
    connection.commit()


@app.get("/api/portfolio/holdings")
def get_holdings(
    account_id: int | None = None,
    status: Literal["open", "closed"] = "open",
    connection=Depends(get_connection),
):
    holdings, _ = ledger(connection, account_id)
    average_costs, unit_costs, realized_pl, dividends = security_performance(connection, account_id)
    setting = connection.execute("SELECT value FROM settings WHERE key='reporting_currency'").fetchone()
    reporting_currency = setting["value"] if setting else "USD"
    result = []
    for (symbol, currency), quantity in sorted(holdings.items()):
        if (quantity == 0) != (status == "closed"):
            continue
        quote = latest_price(connection, symbol)
        security = connection.execute("SELECT name FROM securities WHERE symbol=?", (symbol,)).fetchone()
        average_cost = average_costs.get((symbol, currency))
        market_value = Decimal("0") if quantity == 0 else quantity * Decimal(quote["price"]) if quote else None
        market_value_reporting = None
        if market_value is not None and quote:
            reporting_rate = latest_fx(connection, quote["currency"], reporting_currency)
            if reporting_rate is not None:
                market_value_reporting = market_value * reporting_rate
        unit_cost = unit_costs.get((symbol, currency))
        unrealized = Decimal("0") if quantity == 0 else None
        if quantity != 0 and market_value is not None and unit_cost is not None:
            unrealized = market_value - quantity * unit_cost if quantity > 0 else abs(quantity) * (unit_cost - Decimal(quote["price"]))
        realized = realized_pl.get((symbol, currency), Decimal("0"))
        dividend_total = dividends.get((symbol, currency), Decimal("0"))
        result.append({
            "symbol": symbol,
            "name": security["name"] if security and security["name"] else symbol,
            "currency": currency,
            "quantity": str(quantity),
            "average_cost": str(average_cost) if average_cost is not None else None,
            "price": quote["price"] if quote else None,
            "price_currency": quote["currency"] if quote else None,
            "price_observed_at": quote["observed_at"] if quote else None,
            "market_value": str(market_value) if market_value is not None else None,
            "market_value_currency": quote["currency"] if quote else None,
            "market_value_reporting": str(market_value_reporting) if market_value_reporting is not None else None,
            "market_value_reporting_currency": reporting_currency,
            "unrealized_pl": str(unrealized) if unrealized is not None else None,
            "realized_pl": str(realized),
            "total_return": str(unrealized + realized + dividend_total) if unrealized is not None else None,
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
    _, unit_costs, realized_by_security, dividends_by_security = security_performance(connection, account_id)
    realized_total = Decimal("0")
    dividends_total = Decimal("0")
    interest_by_currency: dict[str, Decimal] = {}
    interest_query = "SELECT currency, cash_amount, fees FROM transactions WHERE type='interest'"
    interest_parameters: tuple = ()
    if account_id is not None:
        interest_query += " AND account_id=?"
        interest_parameters = (account_id,)
    for row in connection.execute(interest_query, interest_parameters).fetchall():
        currency = row["currency"]
        interest_by_currency[currency] = interest_by_currency.get(currency, Decimal("0")) + (
            Decimal(row["cash_amount"] or "0") - Decimal(row["fees"])
        )
    interest_total = Decimal("0")
    realized_complete = True
    unrealized_total = Decimal("0")
    unrealized_complete = True
    for (_, currency), amount in realized_by_security.items():
        rate = latest_fx(connection, currency, reporting_currency)
        if rate is None:
            if amount:
                realized_complete = False
            continue
        realized_total += amount * rate
    for (_, currency), amount in dividends_by_security.items():
        rate = latest_fx(connection, currency, reporting_currency)
        if rate is None:
            if amount:
                realized_complete = False
            continue
        dividends_total += amount * rate
    for currency, amount in interest_by_currency.items():
        rate = latest_fx(connection, currency, reporting_currency)
        if rate is None:
            if amount:
                realized_complete = False
            continue
        interest_total += amount * rate
    for (symbol, transaction_currency), quantity in holdings.items():
        if quantity == 0:
            continue
        quote = latest_price(connection, symbol)
        if not quote:
            unpriced += 1
            unrealized_complete = False
            continue
        rate = latest_fx(connection, quote["currency"], reporting_currency)
        if rate is None:
            unpriced += 1
            unrealized_complete = False
            continue
        positions_total += quantity * Decimal(quote["price"]) * rate
        cost_rate = latest_fx(connection, transaction_currency, reporting_currency)
        unit_cost = unit_costs.get((symbol, transaction_currency))
        if cost_rate is None or unit_cost is None:
            unrealized_complete = False
        elif quantity > 0:
            unrealized_total += quantity * (Decimal(quote["price"]) * rate - unit_cost * cost_rate)
        else:
            unrealized_total += abs(quantity) * (unit_cost * cost_rate - Decimal(quote["price"]) * rate)
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
        "market_value": str(positions_total),
        "unrealized_pl": str(unrealized_total) if unrealized_complete else None,
        "realized_pl": str(realized_total) if realized_complete else None,
        "interest_income": str(interest_total),
        "total_return": str(unrealized_total + realized_total + dividends_total + interest_total) if unrealized_complete and realized_complete else None,
        "cash_balance": str(cash_total),
        "total_value": str(positions_total + cash_total),
        "unpriced_positions": unpriced,
    }


def history_start_date(period: str, today: date, first_event: date | None = None) -> date:
    if period == "1d":
        return today
    if period == "5d":
        return today - WEEK_HISTORY_RETENTION
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
    # Keep intraday timestamps out of longer daily ranges, but use them for
    # the hourly one-week cache.
    coverage_points = points if period in {"1d", "5d"} else [
        point for point in points if len(point["date"]) == 10
    ]
    if not coverage_points:
        return False
    dates = [date.fromisoformat(point["date"][:10]) for point in coverage_points]
    if max(dates) < today - timedelta(days=1):
        return False
    if period == "1d":
        intraday = [datetime.fromisoformat(point["date"]) for point in points if len(point["date"]) > 10]
        if (
            len(intraday) < 3
            or max(intraday) - min(intraday) < timedelta(minutes=10)
            # A prior session is a valid response fallback, but do not let it
            # make the cache look current when a new session may be available.
            or max(intraday).date() != today
        ):
            return False
    if period == "5d" and any(len(point["date"]) > 10 for point in points):
        if max(dates) < today - timedelta(days=1):
            return False
    if period == "max" and not max_coverage:
        return False
    return max_coverage or min(dates) <= start_date + timedelta(days=3)


def prune_price_history(connection, symbol: str, provider: str, today: date) -> None:
    """Remove history rows outside the view their interval can serve."""
    cutoffs = {
        "history:5d": (today - WEEK_HISTORY_RETENTION).isoformat(),
        "history:1mo": (today - timedelta(days=31)).isoformat(),
        "history:3mo": (today - timedelta(days=92)).isoformat(),
        "history:6mo": (today - timedelta(days=184)).isoformat(),
        "history:1y": (today - timedelta(days=365)).isoformat(),
        "history:2y": (today - timedelta(days=730)).isoformat(),
        "history:5y": (today - timedelta(days=1826)).isoformat(),
        "history:ytd": date(today.year, 1, 1).isoformat(),
    }
    for interval, cutoff in cutoffs.items():
        connection.execute(
            "DELETE FROM price_observations WHERE symbol=? AND provider=? AND interval=? AND observed_at < ?",
            (symbol, provider, interval, cutoff),
        )
    connection.execute(
        """DELETE FROM price_observations
           WHERE symbol=? AND provider=? AND interval='history:1d'
             AND substr(observed_at, 1, 10) < (
                 SELECT MAX(substr(observed_at, 1, 10)) FROM price_observations
                 WHERE symbol=? AND provider=? AND interval='history:1d')""",
        (symbol, provider, symbol, provider),
    )


def prune_fx_history(connection, base_currency: str, quote_currency: str, provider: str, today: date) -> None:
    cutoffs = {
        "5d": (today - WEEK_HISTORY_RETENTION).isoformat(),
        "1mo": (today - timedelta(days=31)).isoformat(),
        "3mo": (today - timedelta(days=92)).isoformat(),
        "6mo": (today - timedelta(days=184)).isoformat(),
        "1y": (today - timedelta(days=365)).isoformat(),
        "2y": (today - timedelta(days=730)).isoformat(),
        "5y": (today - timedelta(days=1826)).isoformat(),
        "ytd": date(today.year, 1, 1).isoformat(),
    }
    for period, cutoff in cutoffs.items():
        connection.execute(
            """DELETE FROM fx_observations
               WHERE base_currency=? AND quote_currency=? AND provider=? AND observed_at < ?""",
            (base_currency, quote_currency, f"{provider}:history:{period}", cutoff),
        )
    connection.execute(
        """DELETE FROM fx_observations
           WHERE base_currency=? AND quote_currency=? AND provider=?
             AND substr(observed_at, 1, 10) < (
                 SELECT MAX(substr(observed_at, 1, 10)) FROM fx_observations
                 WHERE base_currency=? AND quote_currency=? AND provider=?)""",
        (base_currency, quote_currency, f"{provider}:history:1d",
         base_currency, quote_currency, f"{provider}:history:1d"),
    )


def history_refresh_is_due(connection, symbol: str, provider: str, period: str) -> bool:
    cache_period = HISTORY_CACHE_PERIODS.get(period, period)
    refreshed = connection.execute(
        """SELECT refreshed_at FROM history_cache_refreshes
           WHERE symbol=? AND period=? AND provider=?""",
        (symbol, cache_period, provider),
    ).fetchone()
    if refreshed is None:
        return True
    refreshed_at = datetime.fromisoformat(refreshed["refreshed_at"])
    return datetime.now(timezone.utc) - refreshed_at >= HISTORY_CACHE_TTLS.get(cache_period, timedelta(days=1))


def mark_history_refreshed(connection, symbol: str, provider: str, period: str) -> None:
    cache_period = HISTORY_CACHE_PERIODS.get(period, period)
    connection.execute(
        """INSERT OR REPLACE INTO history_cache_refreshes
           (symbol, period, provider, refreshed_at) VALUES (?, ?, ?, ?)""",
        (symbol, cache_period, provider, datetime.now(timezone.utc).isoformat()),
    )


def load_price_history_rows(connection, symbol: str, provider: str, period: str):
    if period == "1d":
        return connection.execute(
            """SELECT observed_at, price, currency, interval FROM price_observations
               WHERE symbol=? AND provider=? AND interval='history:1d' ORDER BY observed_at""",
            (symbol, provider),
        ).fetchall()
    if period == "5d":
        return connection.execute(
            """SELECT observed_at, price, currency, interval FROM price_observations
               WHERE symbol=? AND provider=? AND interval='history:5d' ORDER BY observed_at""",
            (symbol, provider),
        ).fetchall()
    return connection.execute(
        """SELECT observed_at, price, currency, interval FROM price_observations
           WHERE symbol=? AND provider=? AND interval LIKE 'history:%'
           AND interval!='history:1d'
           ORDER BY observed_at""",
        (symbol, provider),
    ).fetchall()


def store_intraday_price_history(connection, symbol: str, currency: str, points: list[dict], provider: str) -> None:
    if not points:
        return
    latest_session = max(point["date"][:10] for point in points)
    cached_latest = connection.execute(
        """SELECT MAX(substr(observed_at, 1, 10)) AS latest_session
           FROM price_observations WHERE symbol=? AND provider=? AND interval='history:1d'""",
        (symbol, provider),
    ).fetchone()["latest_session"]
    if cached_latest and latest_session < cached_latest:
        return
    if cached_latest and latest_session > cached_latest:
        connection.execute(
            """DELETE FROM price_observations
               WHERE symbol=? AND provider=? AND interval='history:1d' AND substr(observed_at, 1, 10) < ?""",
            (symbol, provider, latest_session),
        )
    session_points = [point for point in points if point["date"][:10] == latest_session]
    connection.executemany(
        """INSERT OR REPLACE INTO price_observations
           (symbol, currency, price, observed_at, provider, interval) VALUES (?, ?, ?, ?, ?, 'history:1d')""",
        [(symbol, currency, point["close"], point["date"], provider) for point in session_points],
    )


def store_price_history(connection, symbol: str, currency: str, points: list[dict], provider: str, period: str) -> None:
    if not points:
        return
    interval = f"history:{period}"
    if period == "5d":
        # The weekly provider granularity is 30 minutes; remove previously cached hourly bars.
        connection.execute(
            "DELETE FROM price_observations WHERE symbol=? AND provider=? AND interval=?",
            (symbol, provider, interval),
        )
    connection.executemany(
        """INSERT OR REPLACE INTO price_observations
           (symbol, currency, price, observed_at, provider, interval) VALUES (?, ?, ?, ?, ?, ?)""",
        [(symbol, currency, point["close"], point["date"], provider, interval) for point in points],
    )


def filter_history_points(points: list[dict], period: str, start_date: date) -> list[dict]:
    """Keep one complete latest exchange session for an intraday 1D range."""
    if period == "1d":
        intraday_points = [point for point in points if len(point["date"]) > 10]
        if not intraday_points:
            return []
        session_date = max(date.fromisoformat(point["date"][:10]) for point in intraday_points)
        return [
            point for point in intraday_points
            if date.fromisoformat(point["date"][:10]) == session_date
        ]
    return [
        point for point in points
        if date.fromisoformat(point["date"][:10]) >= start_date
        and (period == "5d" or len(point["date"]) == 10)
    ]


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
    symbols = sorted({row["symbol"].upper() for row in transactions if row["symbol"] and row["type"] in ("buy", "sell", "split", "transfer_in", "transfer_out")})
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
        prune_price_history(connection, symbol, PROVIDER.name, today)
        cached_rows = load_price_history_rows(connection, symbol, PROVIDER.name, period)
        points = [{"date": row["observed_at"], "close": row["price"]} for row in cached_rows]
        cached_currencies = {row["currency"] for row in cached_rows}
        has_max_coverage = any(row["interval"] == "history:max" for row in cached_rows)
        refresh_due = history_refresh_is_due(connection, symbol, PROVIDER.name, period)
        refresh_required = (
            not history_cache_is_usable(points, period, start_date, today, has_max_coverage)
            or len(cached_currencies) != 1
            or refresh_due
        )
        if not points and not refresh_due:
            refresh_required = False
        if period == "1d" and points and len(cached_currencies) == 1 and not refresh_due:
            refresh_required = False
        if refresh_required:
            try:
                currency, points = PROVIDER.history(symbol, period)
            except NoPriceHistoryError:
                mark_history_refreshed(connection, symbol, PROVIDER.name, period)
                if not cached_rows or len(cached_currencies) != 1:
                    continue
                currency = cached_currencies.pop()
                points = [{"date": row["observed_at"], "close": row["price"]} for row in cached_rows]
            except Exception as error:
                errors.append(f"{symbol}: {error}")
                continue
            else:
                currency = currency.upper()
                if period == "1d":
                    store_intraday_price_history(connection, symbol, currency, points, PROVIDER.name)
                    cached_rows = load_price_history_rows(connection, symbol, PROVIDER.name, period)
                    points = [{"date": row["observed_at"], "close": row["price"]} for row in cached_rows]
                    cached_currencies = {row["currency"] for row in cached_rows}
                    if len(cached_currencies) == 1:
                        currency = cached_currencies.pop()
                    mark_history_refreshed(connection, symbol, PROVIDER.name, period)
                else:
                    store_price_history(connection, symbol, currency, points, PROVIDER.name, period)
                    mark_history_refreshed(connection, symbol, PROVIDER.name, period)
        else:
            currency = next(iter(cached_currencies), "")
        if not points:
            continue
        points = filter_history_points(points, period, start_date)
        price_currencies[symbol] = currency.upper()
        price_series[symbol] = sorted((point["date"], Decimal(point["close"])) for point in points)

    currencies = set(price_currencies.values()) | {row["currency"] for row in transactions}
    fx_series: dict[str, list[tuple[str, Decimal]]] = {}
    for currency in sorted(currencies - {reporting_currency}):
        start_date = history_start_date(
            period, today, min((date.fromisoformat(row["transaction_date"][:10]) for row in transactions), default=None)
        )
        prune_fx_history(connection, currency, reporting_currency, PROVIDER.name, today)
        cached_rows = connection.execute(
            """SELECT observed_at, rate, provider FROM fx_observations
               WHERE base_currency=? AND quote_currency=? AND provider LIKE ? ORDER BY observed_at""",
            (currency, reporting_currency, f"{PROVIDER.name}:history:%"),
        ).fetchall()
        points = [{"date": row["observed_at"], "close": row["rate"]} for row in cached_rows]
        has_max_coverage = any(row["provider"].endswith(":history:max") for row in cached_rows)
        fx_symbol = f"{currency}{reporting_currency}=X"
        refresh_due = history_refresh_is_due(connection, fx_symbol, PROVIDER.name, period)
        refresh_required = (
            not history_cache_is_usable(points, period, start_date, today, has_max_coverage)
            or refresh_due
        )
        if period == "1d" and points and not refresh_due:
            refresh_required = False
        if refresh_required:
            try:
                _, points = PROVIDER.history(fx_symbol, period)
            except Exception as error:
                errors.append(f"{currency}/{reporting_currency}: {error}")
                continue
            connection.executemany(
                """INSERT OR REPLACE INTO fx_observations
                   (base_currency, quote_currency, rate, observed_at, provider)
                   VALUES (?, ?, ?, ?, ?)""",
                    [(currency, reporting_currency, point["close"], point["date"], f"{PROVIDER.name}:history:{period}") for point in points],
            )
            mark_history_refreshed(connection, fx_symbol, PROVIDER.name, period)
        points = filter_history_points(points, period, start_date)
        fx_series[currency] = sorted((point["date"], Decimal(point["close"])) for point in points)
    if errors:
        connection.commit()
        raise HTTPException(status_code=503, detail=f"Historical data unavailable: {'; '.join(errors)}")

    start_date = history_start_date(period, today)
    dates = {point_date for series in price_series.values() for point_date, _ in series}
    if period != "5d":
        dates.update(
            row["transaction_date"][:10] for row in transactions
            if date.fromisoformat(row["transaction_date"][:10]) >= start_date
        )
    dates = sorted(dates)
    holdings: dict[tuple[str, str], Decimal] = {}
    cash: dict[tuple[int, str], Decimal] = {}
    period_contributions: dict[str, Decimal] = {}
    period_tax_adjustments: dict[str, Decimal] = {}
    period_start_date: str | None = None
    period_start_value: Decimal | None = None
    transaction_index = 0
    price_indexes = {symbol: 0 for symbol in price_series}
    fx_indexes = {currency: 0 for currency in fx_series}
    latest_prices: dict[str, Decimal] = {}
    latest_fx_rates: dict[str, Decimal] = {}
    tax_adjustments: dict[str, Decimal] = {}
    result = []
    for point_date in dates:
        date_only = point_date[:10]
        while transaction_index < len(transactions) and transactions[transaction_index]["transaction_date"][:10] <= date_only:
            transaction = transactions[transaction_index]
            apply_ledger_row(transaction, holdings, cash)
            tax_amount = Decimal(transaction["taxes"] or "0")
            transaction_date = transaction["transaction_date"][:10]
            currency = transaction["currency"]
            if period_start_date is not None and transaction_date > period_start_date:
                if tax_amount:
                    period_tax_adjustments[currency] = period_tax_adjustments.get(currency, Decimal("0")) + tax_amount
                if transaction["type"] in ("deposit", "withdrawal"):
                    contribution = Decimal(transaction["cash_amount"])
                    if transaction["type"] == "withdrawal":
                        contribution = -abs(contribution)
                    period_contributions[currency] = period_contributions.get(currency, Decimal("0")) + contribution
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
        period_contributed = Decimal("0")
        for currency, amount in period_contributions.items():
            rate = Decimal("1") if currency == reporting_currency else latest_fx_rates.get(currency)
            if rate is None and amount:
                priced = False
                break
            if rate is not None:
                period_contributed += amount * rate
        if priced and date.fromisoformat(date_only) >= start_date:
            tax_total = Decimal("0")
            for currency, amount in period_tax_adjustments.items():
                rate = Decimal("1") if currency == reporting_currency else latest_fx_rates.get(currency)
                if rate is None and amount:
                    priced = False
                    break
                if rate is not None:
                    tax_total += amount * rate
            if not priced:
                continue
            if period_start_value is None:
                period_start_date = date_only
                period_start_value = total
            roi_amount = total - period_start_value - period_contributed + tax_total
            roi_basis = period_start_value + period_contributed
            roi = roi_amount * Decimal("100") / roi_basis if roi_basis > 0 else None
            result.append({
                "date": point_date,
                "value": str(total),
                "roi": str(roi) if roi is not None else None,
                "roi_amount": str(roi_amount) if roi_amount is not None else None,
            })

    connection.commit()
    return {"period": period, "reporting_currency": reporting_currency, "points": result}


@app.get("/api/settings")
def get_settings(connection=Depends(get_connection)):
    settings = {
        row["key"]: row["value"]
        for row in connection.execute(
            "SELECT key, value FROM settings WHERE key IN ('reporting_currency', 'openfigi_api_key')"
        )
    }
    return {
        "reporting_currency": settings.get("reporting_currency", "USD"),
        "openfigi_configured": bool(settings.get("openfigi_api_key")),
    }


@app.put("/api/settings")
def update_settings(
    reporting_currency: str | None = None,
    settings: SettingsInput | None = Body(default=None),
    connection=Depends(get_connection),
):
    settings = settings or SettingsInput()
    if settings.openfigi_api_key is not None and not isinstance(settings.openfigi_api_key, str):
        raise HTTPException(status_code=422, detail="OpenFIGI API key must be a string")
    openfigi_api_key = settings.openfigi_api_key.strip() if isinstance(settings.openfigi_api_key, str) else None
    if openfigi_api_key is not None and len(openfigi_api_key) > 512:
        raise HTTPException(status_code=422, detail="OpenFIGI API key is too long")
    if reporting_currency is None and settings.openfigi_api_key is None:
        raise HTTPException(status_code=422, detail="At least one setting is required")
    result: dict[str, str | bool] = {}
    if reporting_currency is not None:
        if len(reporting_currency) != 3 or not reporting_currency.isalpha():
            raise HTTPException(status_code=422, detail="Currency must be a 3-letter code")
        currency = reporting_currency.upper()
        connection.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('reporting_currency', ?)",
            (currency,),
        )
        result["reporting_currency"] = currency
    configured_key = bool(connection.execute(
        "SELECT value FROM settings WHERE key='openfigi_api_key'"
    ).fetchone())
    key_changed = openfigi_api_key is not None or settings.openfigi_api_key == ""
    if key_changed:
        if openfigi_api_key:
            if ENCRYPTION_FERNET is None:
                raise HTTPException(status_code=503, detail="Encryption key is not initialized")
            connection.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES ('openfigi_api_key', ?)",
                (secret_storage.encrypt_secret(ENCRYPTION_FERNET, openfigi_api_key),),
            )
            configured_key = True
        else:
            connection.execute("DELETE FROM settings WHERE key='openfigi_api_key'")
            configured_key = False
    if key_changed:
        connection.execute("DELETE FROM security_search_cache")
    if key_changed:
        result["openfigi_configured"] = configured_key
    connection.commit()
    return result


class ImportTemplateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    definition: CsvTemplateDefinition


class ImportSymbolSelection(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    symbol: str = Field(min_length=1, max_length=32)
    security_name: str = Field(default="", max_length=120)


def template_response(row):
    try:
        definition = CsvTemplateDefinition.model_validate_json(row["definition_json"])
    except ValueError as error:
        raise HTTPException(status_code=409, detail="Stored import template is invalid or unsupported") from error
    if definition.schema_version != row["schema_version"]:
        raise HTTPException(status_code=409, detail="Stored import template version does not match its definition")
    return {
        "id": row["id"], "name": row["name"], "schema_version": row["schema_version"],
        "definition": definition.model_dump(mode="json"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@app.get("/api/imports/templates")
def list_import_templates(connection=Depends(get_connection)):
    return [template_response(row) for row in connection.execute(
        "SELECT * FROM import_templates ORDER BY name COLLATE NOCASE, id"
    )]


@app.post("/api/imports/templates", status_code=201)
def create_import_template(payload: ImportTemplateInput, connection=Depends(get_connection)):
    try:
        cursor = connection.execute(
            "INSERT INTO import_templates(name, definition_json, schema_version) VALUES (?, ?, ?)",
            (payload.name, definition_json(payload.definition), payload.definition.schema_version),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="A template with that name already exists")
    return template_response(connection.execute("SELECT * FROM import_templates WHERE id=?", (cursor.lastrowid,)).fetchone())


@app.get("/api/imports/templates/{template_id}")
def get_import_template(template_id: int, connection=Depends(get_connection)):
    row = connection.execute("SELECT * FROM import_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Import template not found")
    return template_response(row)


@app.put("/api/imports/templates/{template_id}")
def update_import_template(template_id: int, payload: ImportTemplateInput, connection=Depends(get_connection)):
    try:
        cursor = connection.execute(
            """UPDATE import_templates SET name=?, definition_json=?, schema_version=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (payload.name, definition_json(payload.definition), payload.definition.schema_version, template_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Import template not found")
        connection.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="A template with that name already exists")
    return template_response(connection.execute("SELECT * FROM import_templates WHERE id=?", (template_id,)).fetchone())


@app.delete("/api/imports/templates/{template_id}", status_code=204)
def delete_import_template(template_id: int, connection=Depends(get_connection)):
    cursor = connection.execute("DELETE FROM import_templates WHERE id=?", (template_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Import template not found")
    connection.commit()


def open_position_identities(
    connection,
) -> tuple[dict[tuple[int, str], str], dict[tuple[int, str], str]]:
    holdings_by_account: dict[int, dict[tuple[str, str], Decimal]] = {}
    cash_by_account: dict[int, dict[tuple[int, str], Decimal]] = {}
    isins_by_account_symbol: dict[tuple[int, str], set[str]] = {}
    symbols_by_account_isin: dict[tuple[int, str], set[str]] = {}
    rows = connection.execute("SELECT * FROM transactions ORDER BY transaction_date, id").fetchall()
    for row in rows:
        account_id = row["account_id"]
        holdings = holdings_by_account.setdefault(account_id, {})
        cash = cash_by_account.setdefault(account_id, {})
        apply_ledger_row(row, holdings, cash)
        if row["symbol"] and row["isin"]:
            symbol = row["symbol"].upper()
            isin = row["isin"].upper()
            isins_by_account_symbol.setdefault((account_id, symbol), set()).add(isin)
            symbols_by_account_isin.setdefault((account_id, isin), set()).add(symbol)

    positions = {
        (account_id, symbol)
        for account_id, holdings in holdings_by_account.items()
        for (symbol, _), quantity in holdings.items()
        if quantity != 0
    }
    open_isins_by_symbol = {
        key: next(iter(isins))
        for key, isins in isins_by_account_symbol.items()
        if key in positions and len(isins) == 1
    }
    open_symbols_by_isin: dict[tuple[int, str], str] = {}
    for (account_id, isin), symbols in symbols_by_account_isin.items():
        open_symbols = {
            symbol for symbol in symbols
            if (account_id, symbol) in positions
            and open_isins_by_symbol.get((account_id, symbol)) == isin
        }
        if len(open_symbols) == 1:
            open_symbols_by_isin[(account_id, isin)] = next(iter(open_symbols))
    return open_isins_by_symbol, open_symbols_by_isin


@app.post("/api/imports/csv/preview")
async def preview_csv(
    file: UploadFile = File(...),
    field_map: str = Form("{}"),
    template_id: int | None = Form(None),
    template_definition: str | None = Form(None),
    account_id: int | None = Form(None),
    connection=Depends(get_connection),
):
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="CSV must be 2 MB or smaller")
    try:
        if sum(value is not None for value in (template_id, template_definition)) > 1:
            raise HTTPException(status_code=422, detail="Choose a saved template or an inline definition, not both")
        if template_id is not None:
            template = connection.execute("SELECT * FROM import_templates WHERE id=?", (template_id,)).fetchone()
            if template is None:
                raise HTTPException(status_code=404, detail="Import template not found")
            definition = CsvTemplateDefinition.model_validate_json(template["definition_json"])
        elif template_definition is not None:
            definition = CsvTemplateDefinition.model_validate_json(template_definition)
            template_id = None
        else:
            try:
                legacy_mapping = json.loads(field_map)
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=400, detail="CSV field mapping could not be read") from error
            if not isinstance(legacy_mapping, dict):
                raise HTTPException(status_code=400, detail="CSV field mapping must be an object")
            definition = canonical_definition(legacy_mapping)
        headers, sources = parse_csv(raw, definition)
    except HTTPException:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=422, detail=str(error) or "CSV or template could not be read") from error
    required_fields = ("date", "type", "currency") if account_id is not None else ("date", "account", "type", "currency")
    missing = sorted({definition.field_map[key] for key in required_fields
                      if key in definition.field_map and definition.field_map[key] not in headers})
    if missing:
        raise HTTPException(status_code=422, detail=f"Mapped CSV columns not found: {', '.join(missing)}")
    account_ids = {row["name"]: row["id"] for row in connection.execute("SELECT id, name FROM accounts")}
    valid_account_ids = set(account_ids.values())
    if account_id is not None and account_id not in valid_account_ids:
        raise HTTPException(status_code=422, detail="Account override does not exist")
    existing_position_isins, existing_position_symbols = open_position_identities(connection)
    preview_rows = []
    seen_hashes: set[str] = set()
    for source in sources:
        line_number = int(source.pop("__line__", "0"))
        if source_error := source.pop("__row_error__", None):
            preview_rows.append({"line": line_number, "valid": False, "errors": [source_error]})
            continue
        try:
            row = normalize_source_row(source, line_number, definition, account_ids, account_id, normalize_transaction_date)
            if not row.get("symbol") and row.get("isin") and row.get("account_id") is not None:
                row["symbol"] = existing_position_symbols.get(
                    (row["account_id"], row["isin"].upper())
                )
            if not row.get("isin") and row.get("symbol") and row.get("account_id") is not None:
                row["isin"] = existing_position_isins.get(
                    (row["account_id"], row["symbol"].upper())
                )
            if row.get("symbol") and not row.get("security_name"):
                security = connection.execute(
                    "SELECT name FROM securities WHERE symbol=?",
                    (row["symbol"].upper(),),
                ).fetchone()
                if security and security["name"]:
                    row["security_name"] = security["name"]
            errors = validate_import_transaction(row)
            fingerprint = transaction_fingerprint(row)
            exists = connection.execute("SELECT 1 FROM transactions WHERE source_hash=?", (fingerprint,)).fetchone()
            if exists or fingerprint in seen_hashes:
                errors.append("Duplicate transaction")
            seen_hashes.add(fingerprint)
            row.update({"line": line_number, "valid": not errors, "errors": errors})
            preview_rows.append(row)
        except (ValueError, InvalidOperation, KeyError, ArithmeticError) as error:
            preview_rows.append({"line": line_number, "valid": False, "errors": [str(error) or "Invalid row"]})
    batch_id = str(uuid.uuid4())
    connection.execute(
        """INSERT INTO import_batches(id, filename, status, preview_json, template_id, template_json)
           VALUES (?, ?, 'preview', ?, ?, ?)""",
        (batch_id, Path(file.filename or "transactions.csv").name, json.dumps(preview_rows),
         template_id, definition_json(definition)),
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


@app.put("/api/imports/{batch_id}/rows/{line_number}/symbol")
def select_import_symbol(
    batch_id: str,
    line_number: int,
    payload: ImportSymbolSelection,
    connection=Depends(get_connection),
):
    batch = connection.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    if batch["status"] != "preview":
        raise HTTPException(status_code=409, detail="Import batch has already been committed")
    rows = json.loads(batch["preview_json"])
    row = next((item for item in rows if item.get("line") == line_number), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Import preview row not found")
    if row.get("type") not in ("buy", "sell", "split", "dividend", "interest", "fee", "deposit", "withdrawal", "transfer_in", "transfer_out"):
        raise HTTPException(status_code=422, detail="This row has no recognized transaction type")
    valid_types = ("buy", "sell", "split", "dividend", "interest", "fee", "deposit", "withdrawal", "transfer_in", "transfer_out")
    selected_isin = (row.get("isin") or "").upper()
    for candidate in rows:
        same_isin = selected_isin and (candidate.get("isin") or "").upper() == selected_isin
        if candidate is row or (same_isin and not candidate.get("symbol")):
            candidate["symbol"] = payload.symbol.upper()
            candidate["security_name"] = payload.security_name

    # Revalidate the batch in source order so duplicate markings remain deterministic
    # after a symbol choice makes previously incomplete transactions comparable.
    seen_hashes: set[str] = set()
    for candidate in rows:
        if candidate.get("type") not in valid_types:
            continue
        resolved_symbol_errors = {
            "Symbol is required for a trade",
            "Symbol is required for a split",
            "Symbol is required for a security distribution",
        }
        errors = [
            error for error in candidate.get("errors", [])
            if error != "Duplicate transaction" and not (candidate.get("symbol") and error in resolved_symbol_errors)
        ]
        errors.extend(validate_import_transaction(candidate))
        errors = list(dict.fromkeys(errors))
        if not errors:
            fingerprint = transaction_fingerprint(candidate)
            exists = connection.execute("SELECT 1 FROM transactions WHERE source_hash=?", (fingerprint,)).fetchone()
            if exists or fingerprint in seen_hashes:
                errors.append("Duplicate transaction")
            else:
                seen_hashes.add(fingerprint)
        candidate["errors"] = errors
        candidate["valid"] = not errors
    connection.execute("UPDATE import_batches SET preview_json=? WHERE id=?", (json.dumps(rows), batch_id))
    connection.commit()
    return {
        "id": batch_id,
        "filename": batch["filename"],
        "total_rows": len(rows),
        "valid_rows": sum(item.get("valid", False) for item in rows),
        "invalid_rows": sum(not item.get("valid", False) for item in rows),
        "rows": rows,
    }


@app.get("/api/imports")
def list_imports(connection=Depends(get_connection)):
    return [dict(row) for row in connection.execute(
        "SELECT id, filename, status, imported_count, created_at FROM import_batches ORDER BY created_at DESC"
    )]


@app.get("/api/prices/{symbol}")
def get_quote(symbol: str, connection=Depends(get_connection)):
    normalized_symbol = symbol.upper()
    cached = connection.execute(
        """SELECT symbol, price, currency, observed_at, provider FROM price_observations
           WHERE symbol=? AND provider=? AND interval='quote' ORDER BY observed_at DESC LIMIT 1""",
        (normalized_symbol, PROVIDER.name),
    ).fetchone()
    if cached and observation_is_fresh(cached["observed_at"], QUOTE_CACHE_TTL):
        return dict(cached)
    try:
        price, currency, observed_at = PROVIDER.quote(normalized_symbol)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Quote unavailable: {error}")
    save_quote(connection, normalized_symbol, price, currency, observed_at)
    connection.commit()
    return {"symbol": normalized_symbol, "price": str(price), "currency": currency,
            "observed_at": observed_at, "provider": PROVIDER.name}


@app.get("/api/prices/{symbol}/history")
def get_price_history(
    symbol: str,
    period: Literal["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"] = "1y",
    connection=Depends(get_connection),
):
    symbol = symbol.upper()
    today = datetime.now(timezone.utc).date()
    start_date = history_start_date(period, today)
    prune_price_history(connection, symbol, PROVIDER.name, today)
    cached_rows = load_price_history_rows(connection, symbol, PROVIDER.name, period)
    points = [{"date": row["observed_at"], "close": row["price"], "interval": row["interval"]} for row in cached_rows]
    currencies = {row["currency"] for row in cached_rows}
    has_max_coverage = any(row["interval"] == "history:max" for row in cached_rows)
    refresh_due = history_refresh_is_due(connection, symbol, PROVIDER.name, period)
    refresh_required = (
        not history_cache_is_usable(points, period, start_date, today, has_max_coverage)
        or len(currencies) != 1
        or refresh_due
    )
    if not points and not refresh_due:
        refresh_required = False
    if period == "1d" and points and len(currencies) == 1 and not refresh_due:
        refresh_required = False
    if refresh_required:
        try:
            currency, points = PROVIDER.history(symbol, period)
        except NoPriceHistoryError:
            mark_history_refreshed(connection, symbol, PROVIDER.name, period)
            if cached_rows:
                currency = next(iter(currencies))
                points = [
                    {"date": row["observed_at"], "close": row["price"], "interval": row["interval"]}
                    for row in cached_rows
                ]
            else:
                currency = ""
                points = []
            connection.commit()
        except Exception as error:
            raise HTTPException(status_code=503, detail=f"Price history unavailable: {error}")
        else:
            if period == "1d":
                store_intraday_price_history(connection, symbol, currency, points, PROVIDER.name)
                cached_rows = load_price_history_rows(connection, symbol, PROVIDER.name, period)
                points = [{"date": row["observed_at"], "close": row["price"], "interval": "history:1d"} for row in cached_rows]
                currencies = {row["currency"] for row in cached_rows}
                if len(currencies) == 1:
                    currency = currencies.pop()
                mark_history_refreshed(connection, symbol, PROVIDER.name, period)
            else:
                store_price_history(connection, symbol, currency, points, PROVIDER.name, period)
                mark_history_refreshed(connection, symbol, PROVIDER.name, period)
            connection.commit()
    else:
        currency = next(iter(currencies), "")
    points = filter_history_points(points, period, start_date)
    unique_points = {}
    for point in points:
        current = unique_points.get(point["date"])
        if current is None or point.get("interval") == f"history:{period}":
            unique_points[point["date"]] = point
    points = [{"date": point_date, "close": point["close"]} for point_date, point in sorted(unique_points.items())]
    return {"symbol": symbol, "currency": currency, "provider": PROVIDER.name, "points": points}


@app.get("/api/fx/{base_currency}/{quote_currency}/current")
def get_fx_rate(base_currency: str, quote_currency: str, connection=Depends(get_connection)):
    base, quote = base_currency.upper(), quote_currency.upper()
    if base == quote:
        return {"base_currency": base, "quote_currency": quote, "rate": "1",
                "observed_at": datetime.now(timezone.utc).isoformat(), "provider": "identity"}
    cached = connection.execute(
        """SELECT base_currency, quote_currency, rate, observed_at, provider FROM fx_observations
           WHERE base_currency=? AND quote_currency=? AND provider=? ORDER BY observed_at DESC LIMIT 1""",
        (base, quote, PROVIDER.name),
    ).fetchone()
    if cached and observation_is_fresh(cached["observed_at"], FX_CACHE_TTL):
        return dict(cached)
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
    prune_fx_history(connection, base, quote, PROVIDER.name, today)
    cached_rows = connection.execute(
        """SELECT observed_at, rate, provider FROM fx_observations
           WHERE base_currency=? AND quote_currency=? AND provider LIKE ? ORDER BY observed_at""",
        (base, quote, f"{PROVIDER.name}:history:%"),
    ).fetchall()
    points = [{"date": row["observed_at"], "rate": row["rate"]} for row in cached_rows]
    has_max_coverage = any(row["provider"].endswith(":history:max") for row in cached_rows)
    cache_usable = history_cache_is_usable(
        [{"date": point["date"], "close": point["rate"]} for point in points],
        period, start_date, today, has_max_coverage,
    )
    if not cache_usable or history_refresh_is_due(connection, f"{base}{quote}=X", PROVIDER.name, period):
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
        mark_history_refreshed(connection, f"{base}{quote}=X", PROVIDER.name, period)
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
            headers = {"Cache-Control": "no-cache"} if file_path.name == "index.html" else None
            return FileResponse(file_path, headers=headers)
        return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})
