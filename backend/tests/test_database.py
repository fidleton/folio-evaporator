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
        migrated = connection.execute("SELECT type, source_hash, isin FROM transactions WHERE id=1").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute(
            """INSERT INTO transactions(account_id, type, transaction_date, currency, cash_amount, source_hash)
               VALUES (1, 'deposit', '2024-01-01', 'USD', '1000', 'deposit-hash')"""
        )

    assert (migrated["type"], migrated["source_hash"], migrated["isin"]) == ("buy", "legacy-hash", None)
    assert version == 3