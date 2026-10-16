from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import database, main


class FakeProvider:
    name = "fixture provider"

    def history(self, symbol, period):
        today = datetime.now(timezone.utc).date().isoformat()
        return "USD", [{"date": today, "close": "101.25"}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "folio.db")
    monkeypatch.setattr(main, "PROVIDER", FakeProvider())
    with TestClient(main.app) as test_client:
        yield test_client


def test_price_history_is_cached(client):
    response = client.get("/api/prices/ACME/history?period=1mo")
    assert response.status_code == 200
    assert response.json()["points"] == [{"date": datetime.now(timezone.utc).date().isoformat(), "close": "101.25"}]

    with database.connect() as connection:
        cached = connection.execute(
            "SELECT price, currency, interval FROM price_observations WHERE symbol='ACME'"
        ).fetchone()
    assert (cached["price"], cached["currency"], cached["interval"]) == ("101.25", "USD", "history:1mo")


def test_fx_history_is_cached(client):
    response = client.get("/api/fx/EUR/USD/history?period=1mo")
    assert response.status_code == 200
    assert response.json()["points"] == [{"date": datetime.now(timezone.utc).date().isoformat(), "rate": "101.25"}]

    with database.connect() as connection:
        cached = connection.execute(
            "SELECT base_currency, quote_currency, rate FROM fx_observations"
        ).fetchone()
    assert (cached["base_currency"], cached["quote_currency"], Decimal(cached["rate"])) == (
        "EUR", "USD", Decimal("101.25"),
    )


def test_price_and_fx_history_endpoints_reuse_cached_series(client, monkeypatch):
    today = datetime.now(timezone.utc).date()

    class CountingProvider:
        name = "fixture provider"
        calls = []

        def history(self, symbol, period):
            self.calls.append((symbol, period))
            return "USD", [
                {"date": (today - timedelta(days=31)).isoformat(), "close": "100"},
                {"date": today.isoformat(), "close": "101"},
            ]

    provider = CountingProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)

    assert client.get("/api/prices/ACME/history?period=1mo").status_code == 200
    assert client.get("/api/prices/ACME/history?period=1mo").status_code == 200
    assert client.get("/api/fx/EUR/USD/history?period=1mo").status_code == 200
    assert client.get("/api/fx/EUR/USD/history?period=1mo").status_code == 200

    assert provider.calls == [("ACME", "1mo"), ("EURUSD=X", "1mo")]


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


def test_portfolio_history_replays_transactions_at_historical_prices(client, monkeypatch):
    class HistoryProvider:
        name = "fixture provider"

        def history(self, symbol, period):
            assert period == "max"
            return "USD", [
                {"date": "2025-01-02", "close": "100"},
                {"date": "2025-01-03", "close": "120"},
            ]

    monkeypatch.setattr(main, "PROVIDER", HistoryProvider())
    account = client.post("/api/accounts", json={"name": "Long term"}).json()
    client.post("/api/transactions", json={
        "account_id": account["id"], "type": "deposit", "transaction_date": "2025-01-01",
        "currency": "USD", "cash_amount": "1000",
    })
    client.post("/api/transactions", json={
        "account_id": account["id"], "type": "buy", "transaction_date": "2025-01-02",
        "symbol": "ACME", "quantity": "2", "price": "100", "currency": "USD", "fees": "1",
    })

    response = client.get("/api/portfolio/history?period=max")

    assert response.status_code == 200
    assert response.json()["points"] == [
        {"date": "2025-01-01", "value": "1000", "roi": "0", "roi_amount": "0"},
        {"date": "2025-01-02", "value": "999", "roi": "-0.1", "roi_amount": "-1"},
        {"date": "2025-01-03", "value": "1039", "roi": "3.9", "roi_amount": "39"},
    ]


def test_portfolio_history_reuses_complete_cache_and_fetches_wider_range(client, monkeypatch):
    today = datetime.now(timezone.utc)

    class CountingProvider:
        name = "fixture provider"
        calls = []

        def history(self, symbol, period):
            self.calls.append((symbol, period))
            if period == "1d":
                points = [
                    {"date": (today - timedelta(minutes=20)).isoformat(), "close": "99"},
                    {"date": (today - timedelta(minutes=10)).isoformat(), "close": "100"},
                    {"date": today.isoformat(), "close": "101"},
                ]
            else:
                points = [
                    {"date": (today.date() - timedelta(days=365)).isoformat(), "close": "90"},
                    {"date": today.date().isoformat(), "close": "101"},
                ]
            return "USD", points

    provider = CountingProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)
    account = client.post("/api/accounts", json={"name": "Cache test"}).json()
    client.post("/api/transactions", json={
        "account_id": account["id"], "type": "buy", "transaction_date": today.date().isoformat(),
        "symbol": "ACME", "quantity": "1", "price": "100", "currency": "USD",
    })

    assert client.get("/api/portfolio/history?period=1d").status_code == 200
    assert client.get("/api/portfolio/history?period=1d").status_code == 200
    assert provider.calls == [("ACME", "1d")]

    assert client.get("/api/portfolio/history?period=1y").status_code == 200
    one_year = client.get("/api/portfolio/history?period=1y").json()
    year_dates = [point["date"] for point in one_year["points"]]
    assert all(len(point_date) == 10 for point_date in year_dates)
    assert len(year_dates) == len(set(year_dates))
    assert provider.calls == [("ACME", "1d"), ("ACME", "1y")]

    one_day = client.get("/api/portfolio/history?period=1d").json()
    assert provider.calls == [("ACME", "1d"), ("ACME", "1y")]
    assert all(point["date"].startswith(today.date().isoformat()) for point in one_day["points"])


def test_single_timestamped_quote_does_not_satisfy_intraday_history_cache():
    today = datetime.now(timezone.utc)
    point = {"date": today.isoformat(), "close": "100"}

    assert not main.history_cache_is_usable([point], "1d", today.date(), today.date())