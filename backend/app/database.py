import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(DATA_DIR / "folio.db")))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS securities (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT 'USD'
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                symbol TEXT,
                type TEXT NOT NULL CHECK(type IN ('buy', 'sell', 'dividend', 'fee', 'split')),
                transaction_date TEXT NOT NULL,
                quantity TEXT NOT NULL DEFAULT '0',
                price TEXT NOT NULL DEFAULT '0',
                currency TEXT NOT NULL,
                fees TEXT NOT NULL DEFAULT '0',
                cash_amount TEXT,
                notes TEXT NOT NULL DEFAULT '',
                source_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_account_date
                ON transactions(account_id, transaction_date);
            CREATE INDEX IF NOT EXISTS idx_transactions_symbol_date
                ON transactions(symbol, transaction_date);
            CREATE TABLE IF NOT EXISTS import_batches (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                imported_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS price_observations (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                currency TEXT NOT NULL,
                price TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                interval TEXT NOT NULL DEFAULT 'quote',
                UNIQUE(symbol, observed_at, provider, interval)
            );
            CREATE INDEX IF NOT EXISTS idx_price_symbol_observed
                ON price_observations(symbol, observed_at);
            CREATE TABLE IF NOT EXISTS history_cache_refreshes (
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                provider TEXT NOT NULL,
                refreshed_at TEXT NOT NULL,
                PRIMARY KEY(symbol, period, provider)
            );
            CREATE TABLE IF NOT EXISTS fx_observations (
                id INTEGER PRIMARY KEY,
                base_currency TEXT NOT NULL,
                quote_currency TEXT NOT NULL,
                rate TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                UNIQUE(base_currency, quote_currency, observed_at, provider)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO settings(key, value) VALUES ('reporting_currency', 'USD');
            INSERT OR IGNORE INTO settings(key, value) VALUES ('time_format', '24-hour');
            """
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            connection.execute("PRAGMA user_version = 1")
            version = 1
        if version < 2:
            connection.execute("DROP INDEX IF EXISTS idx_transactions_account_date")
            connection.execute("DROP INDEX IF EXISTS idx_transactions_symbol_date")
            connection.execute("ALTER TABLE transactions RENAME TO transactions_v1")
            connection.execute(
                """CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    symbol TEXT,
                    type TEXT NOT NULL CHECK(type IN ('buy', 'sell', 'dividend', 'fee', 'split', 'deposit', 'withdrawal')),
                    transaction_date TEXT NOT NULL,
                    quantity TEXT NOT NULL DEFAULT '0',
                    price TEXT NOT NULL DEFAULT '0',
                    currency TEXT NOT NULL,
                    fees TEXT NOT NULL DEFAULT '0',
                    cash_amount TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    source_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """INSERT INTO transactions
                   SELECT id, account_id, symbol, type, transaction_date, quantity, price,
                          currency, fees, cash_amount, notes, source_hash, created_at
                   FROM transactions_v1"""
            )
            connection.execute("DROP TABLE transactions_v1")
            connection.execute(
                "CREATE INDEX idx_transactions_account_date ON transactions(account_id, transaction_date)"
            )
            connection.execute(
                "CREATE INDEX idx_transactions_symbol_date ON transactions(symbol, transaction_date)"
            )
            connection.execute("PRAGMA user_version = 2")
            version = 2
        if version < 3:
            connection.execute("ALTER TABLE transactions ADD COLUMN isin TEXT")
            connection.execute("PRAGMA user_version = 3")
            version = 3
        if version < 4:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS security_search_cache (
                    query TEXT PRIMARY KEY,
                    results_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL
                )"""
            )
            connection.execute("PRAGMA user_version = 4")
        if version < 5:
            connection.execute("ALTER TABLE transactions ADD COLUMN taxes TEXT NOT NULL DEFAULT '0'")
            connection.execute("PRAGMA user_version = 5")
            version = 5
        if version < 6:
            rows = connection.execute(
                "SELECT id, transaction_date FROM transactions WHERE length(transaction_date) > 10"
            ).fetchall()
            for row in rows:
                timestamp = datetime.fromisoformat(row["transaction_date"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.astimezone()
                connection.execute(
                    "UPDATE transactions SET transaction_date=? WHERE id=?",
                    (timestamp.astimezone(timezone.utc).isoformat(), row["id"]),
                )
            connection.execute("PRAGMA user_version = 6")
            version = 6
        if version < 7:
            connection.execute("PRAGMA user_version = 7")
            version = 7
        if version < 8:
            intraday_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intraday_price_observations'"
            ).fetchone()
            if intraday_table:
                connection.execute(
                    """INSERT OR IGNORE INTO price_observations
                       (symbol, currency, price, observed_at, provider, interval)
                       SELECT symbol, currency, price, observed_at, provider, 'history:1d'
                       FROM intraday_price_observations"""
                )
            connection.execute("DROP TABLE IF EXISTS intraday_price_observations")
            connection.execute("DROP INDEX IF EXISTS idx_intraday_price_symbol_observed")
            connection.execute("PRAGMA user_version = 8")
            version = 8
        if version < 9:
            account_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
            }
            if "broker" in account_columns:
                connection.execute("ALTER TABLE accounts DROP COLUMN broker")
            connection.execute("PRAGMA user_version = 9")
            version = 9
        if version < 10:
            account_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
            }
            if "base_currency" in account_columns:
                connection.execute("ALTER TABLE accounts DROP COLUMN base_currency")
            connection.execute("PRAGMA user_version = 10")
