import os
import sqlite3
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
                broker TEXT NOT NULL DEFAULT '',
                base_currency TEXT NOT NULL DEFAULT 'USD',
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
