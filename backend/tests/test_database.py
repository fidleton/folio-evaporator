import sqlite3

from app import database


def test_v1_database_migrates_without_losing_transactions(tmp_path, monkeypatch):
    db_path = tmp_path / "folio.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                broker TEXT NOT NULL DEFAULT '', base_currency TEXT NOT NULL DEFAULT 'USD',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE transactions (
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
            INSERT INTO accounts(id, name) VALUES (1, 'Legacy account');
            INSERT INTO transactions(account_id, symbol, type, transaction_date, currency, source_hash)
                VALUES (1, 'ACME', 'buy', '2024-01-02', 'USD', 'legacy-hash');
            PRAGMA user_version = 1;
            """
        )

    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.initialize()

    with database.connect() as connection:
        migrated = connection.execute("SELECT type, source_hash, isin, taxes FROM transactions WHERE id=1").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        search_cache = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='security_search_cache'"
        ).fetchone()
        connection.execute(
            """INSERT INTO transactions(account_id, type, transaction_date, currency, cash_amount, source_hash)
               VALUES (1, 'deposit', '2024-01-01', 'USD', '1000', 'deposit-hash')"""
        )

    assert (migrated["type"], migrated["source_hash"], migrated["isin"], migrated["taxes"]) == (
        "buy", "legacy-hash", None, "0",
    )
    assert version == 10
    assert search_cache["name"] == "security_search_cache"
    with database.connect() as connection:
        account_columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    assert "broker" not in account_columns
    assert "base_currency" not in account_columns


def test_v6_intraday_observations_migrate_into_price_observations(tmp_path, monkeypatch):
    db_path = tmp_path / "folio.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '101', '2026-10-30T15:00:00+00:00', 'Yahoo', 'history:1d')"""
        )
        connection.execute("PRAGMA user_version = 6")

    database.initialize()

    with database.connect() as connection:
        migrated = connection.execute(
            "SELECT symbol, price, interval FROM price_observations WHERE interval='history:1d'"
        ).fetchone()
        old_table = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='intraday_price_observations'"
        ).fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert (migrated["symbol"], migrated["price"], migrated["interval"]) == ("ACME", "101", "history:1d")
    assert old_table == 0
    assert version == 10


def test_v7_intraday_table_is_merged_without_overwriting_price_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "folio.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """CREATE TABLE intraday_price_observations (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, currency TEXT NOT NULL,
                price TEXT NOT NULL, observed_at TEXT NOT NULL, provider TEXT NOT NULL,
                UNIQUE(symbol, observed_at, provider))"""
        )
        connection.execute(
            """INSERT INTO intraday_price_observations
               (symbol, currency, price, observed_at, provider)
               VALUES ('ACME', 'USD', '102', '2026-10-30T15:00:00+00:00', 'Yahoo')"""
        )
        connection.execute(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '103', '2026-10-30T15:00:00+00:00', 'Yahoo', 'history:1d')"""
        )
        connection.execute("PRAGMA user_version = 7")

    database.initialize()

    with database.connect() as connection:
        rows = connection.execute("SELECT price FROM price_observations WHERE interval='history:1d'").fetchall()
        old_table = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='intraday_price_observations'"
        ).fetchone()[0]

    assert [row["price"] for row in rows] == ["103"]
    assert old_table == 0
