from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import database, main


class FakeProvider:
    name = "fixture provider"

    def history(self, symbol, period):
        return "USD", [{"date": "2025-01-02", "close": "101.25"}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "folio.db")
    monkeypatch.setattr(main, "PROVIDER", FakeProvider())
    with TestClient(main.app) as test_client:
        yield test_client


def test_price_history_is_cached(client):
    response = client.get("/api/prices/ACME/history?period=1mo")
    assert response.status_code == 200
    assert response.json()["points"] == [{"date": "2025-01-02", "close": "101.25"}]

    with database.connect() as connection:
        cached = connection.execute(
            "SELECT price, currency, interval FROM price_observations WHERE symbol='ACME'"
        ).fetchone()
    assert (cached["price"], cached["currency"], cached["interval"]) == ("101.25", "USD", "history")


def test_fx_history_is_cached(client):
    response = client.get("/api/fx/EUR/USD/history?period=1mo")
    assert response.status_code == 200
    assert response.json()["points"] == [{"date": "2025-01-02", "rate": "101.25"}]

    with database.connect() as connection:
        cached = connection.execute(
            "SELECT base_currency, quote_currency, rate FROM fx_observations"
        ).fetchone()
    assert (cached["base_currency"], cached["quote_currency"], Decimal(cached["rate"])) == (
        "EUR", "USD", Decimal("101.25"),
    )


def test_portfolio_summary_converts_position_and_cash_to_reporting_currency(client):
    account = client.post("/api/accounts", json={"name": "Euro account", "base_currency": "EUR"}).json()
    deposit = client.post("/api/transactions", json={
        "account_id": account["id"], "type": "deposit", "transaction_date": "2025-01-01",
        "currency": "EUR", "cash_amount": "1000",
    })
    assert deposit.status_code == 201
    purchase = client.post("/api/transactions", json={
        "account_id": account["id"], "type": "buy", "transaction_date": "2025-01-02",
        "symbol": "ACME", "quantity": "2", "price": "100", "currency": "EUR", "fees": "1",
    })
    assert purchase.status_code == 201

    with database.connect() as connection:
        connection.execute(
            """INSERT INTO price_observations(symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'EUR', '110', '2025-01-03T00:00:00+00:00', 'fixture provider', 'quote')"""
        )
        connection.execute(
            """INSERT INTO fx_observations(base_currency, quote_currency, rate, observed_at, provider)
               VALUES ('EUR', 'USD', '1.2', '2025-01-03T00:00:00+00:00', 'fixture provider')"""
        )

    summary = client.get("/api/portfolio/summary").json()
    assert Decimal(summary["positions_value"]) == Decimal("264")
    assert Decimal(summary["cash_balance"]) == Decimal("958.8")
    assert Decimal(summary["total_value"]) == Decimal("1222.8")