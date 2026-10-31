from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import database, main, market_data


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


def test_security_history_supports_one_day_and_one_week(client, monkeypatch):
    today = datetime.now(timezone.utc)

    class RangeProvider:
        name = "fixture provider"
        calls = []

        def history(self, symbol, period):
            self.calls.append(period)
            if period == "1d":
                points = [
                    {"date": (today - timedelta(minutes=20)).isoformat(), "close": "99"},
                    {"date": (today - timedelta(minutes=10)).isoformat(), "close": "100"},
                    {"date": today.isoformat(), "close": "101"},
                ]
            else:
                points = [
                    {"date": (today.date() - timedelta(days=4)).isoformat(), "close": "99"},
                    {"date": today.date().isoformat(), "close": "101"},
                ]
            return "USD", points

    provider = RangeProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)

    one_day = client.get("/api/prices/ACME/history?period=1d")
    one_week = client.get("/api/prices/ACME/history?period=5d")
    client.get("/api/prices/ACME/history?period=1d")
    client.get("/api/prices/ACME/history?period=5d")

    assert one_day.status_code == 200
    assert one_week.status_code == 200
    assert len(one_day.json()["points"]) == 3
    assert len(one_week.json()["points"]) == 2
    assert provider.calls == ["1d", "5d"]


def test_one_week_history_keeps_hourly_points_and_prunes_older_points(client, monkeypatch):
    today = datetime.now(timezone.utc)
    old_point = (today - timedelta(days=8)).isoformat()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '90', ?, 'fixture provider', 'history:5d')""",
            (old_point,),
        )

    class HourlyProvider:
        name = "fixture provider"

        def history(self, symbol, period):
            assert period == "5d"
            return "USD", [
                {"date": (today - timedelta(hours=1)).isoformat(), "close": "100"},
                {"date": today.isoformat(), "close": "101"},
            ]

    monkeypatch.setattr(main, "PROVIDER", HourlyProvider())
    response = client.get("/api/prices/ACME/history?period=5d")

    assert response.status_code == 200
    assert [point["close"] for point in response.json()["points"]] == ["100", "101"]
    assert all("T" in point["date"] for point in response.json()["points"])
    with database.connect() as connection:
        old_count = connection.execute(
            """SELECT COUNT(*) FROM price_observations
               WHERE symbol='ACME' AND interval='history:5d' AND observed_at=?""",
            (old_point,),
        ).fetchone()[0]
    assert old_count == 0


def test_price_and_fx_history_retention_removes_un_displayable_rows(client):
    today = datetime.now(timezone.utc).date()
    retention_days = {
        "1mo": 31, "3mo": 92, "6mo": 184, "1y": 365,
        "2y": 730, "5y": 1826, "ytd": (today - datetime(today.year, 1, 1).date()).days,
        "5d": 7,
    }
    with database.connect() as connection:
        for period, days in retention_days.items():
            too_old = (today - timedelta(days=days + 2)).isoformat()
            retained = (today - timedelta(days=max(0, days - 1))).isoformat()
            connection.executemany(
                """INSERT INTO price_observations
                   (symbol, currency, price, observed_at, provider, interval)
                   VALUES ('ACME', 'USD', '100', ?, 'fixture provider', ?)""",
                [(too_old, f"history:{period}"), (retained, f"history:{period}")],
            )
            connection.executemany(
                """INSERT INTO fx_observations
                   (base_currency, quote_currency, rate, observed_at, provider)
                   VALUES ('EUR', 'USD', '1.2', ?, ?)""",
                [(too_old, f"fixture provider:history:{period}"),
                 (retained, f"fixture provider:history:{period}")],
            )
        # A prior 1D session remains available until a later session is cached.
        prior_session = (today - timedelta(days=3)).isoformat()
        connection.execute(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '99', ?, 'fixture provider', 'history:1d')""",
            (prior_session + "T15:00:00+00:00",),
        )
        connection.execute(
            """INSERT INTO fx_observations
               (base_currency, quote_currency, rate, observed_at, provider)
               VALUES ('EUR', 'USD', '1.1', ?, 'fixture provider:history:1d')""",
            (prior_session,),
        )

        main.prune_price_history(connection, "ACME", "fixture provider", today)
        main.prune_fx_history(connection, "EUR", "USD", "fixture provider", today)

        price_rows = connection.execute(
            "SELECT interval, observed_at FROM price_observations WHERE symbol='ACME'"
        ).fetchall()
        fx_rows = connection.execute(
            "SELECT provider, observed_at FROM fx_observations WHERE base_currency='EUR'"
        ).fetchall()

    price_keys = {(row["interval"], row["observed_at"][:10]) for row in price_rows}
    fx_keys = {(row["provider"].rsplit(':', 1)[-1], row["observed_at"][:10]) for row in fx_rows}
    for period, days in retention_days.items():
        retained = (today - timedelta(days=max(0, days - 1))).isoformat()
        assert (f"history:{period}", retained) in price_keys
        assert (period, retained) in fx_keys
        assert not any(key[0] == f"history:{period}" and key[1] == (today - timedelta(days=days + 2)).isoformat()
                       for key in price_keys)
        assert not any(key[0] == period and key[1] == (today - timedelta(days=days + 2)).isoformat()
                       for key in fx_keys)
    assert ("history:1d", prior_session) in price_keys
    assert ("1d", prior_session) in fx_keys


def test_one_day_history_uses_latest_available_session(client, monkeypatch):
    latest_session = datetime.now(timezone.utc).date() - timedelta(days=1)

    class PriorSessionProvider:
        name = "fixture provider"
        calls = 0

        def history(self, symbol, period):
            self.calls += 1
            assert period == "1d"
            return "USD", [
                {"date": datetime.combine(latest_session, datetime.min.time(), timezone.utc).isoformat(), "close": "99"},
                {"date": datetime.combine(latest_session, datetime.min.time(), timezone.utc).replace(minute=10).isoformat(), "close": "100"},
                {"date": datetime.combine(latest_session, datetime.min.time(), timezone.utc).replace(minute=20).isoformat(), "close": "101"},
            ]

    provider = PriorSessionProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)

    response = client.get("/api/prices/ACME/history?period=1d")
    cached_response = client.get("/api/prices/ACME/history?period=1d")

    assert response.status_code == 200
    assert len(response.json()["points"]) == 3
    assert all(point["date"].startswith(latest_session.isoformat()) for point in response.json()["points"])
    assert cached_response.json()["points"] == response.json()["points"]
    assert provider.calls == 1
    with database.connect() as connection:
        cached_rows = connection.execute(
            "SELECT COUNT(*) FROM price_observations WHERE symbol='ACME' AND interval='history:1d'"
        ).fetchone()[0]
        old_table = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='intraday_price_observations'"
        ).fetchone()[0]
    assert cached_rows == 3
    assert old_table == 0


def test_intraday_history_replaces_prior_session_after_new_session_arrives(client, monkeypatch):
    today = datetime.now(timezone.utc).date()

    class NewSessionProvider:
        name = "fixture provider"
        calls = 0

        def history(self, symbol, period):
            self.calls += 1
            session = today - timedelta(days=1) if self.calls == 1 else today
            return "USD", [
                {
                    "date": datetime.combine(session, datetime.min.time(), timezone.utc)
                    .replace(minute=offset).isoformat(),
                    "close": str(100 + offset),
                }
                for offset in (0, 10, 20)
            ]

    provider = NewSessionProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)
    first = client.get("/api/prices/ACME/history?period=1d")
    assert first.status_code == 200

    with database.connect() as connection:
        connection.execute(
            """UPDATE history_cache_refreshes SET refreshed_at=?
               WHERE symbol='ACME' AND period='1d'""",
            ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),),
        )

    second = client.get("/api/prices/ACME/history?period=1d")

    assert second.status_code == 200
    assert provider.calls == 2
    assert all(point["date"].startswith(today.isoformat()) for point in second.json()["points"])
    with database.connect() as connection:
        sessions = connection.execute(
            """SELECT DISTINCT substr(observed_at, 1, 10) FROM price_observations
               WHERE symbol='ACME' AND interval='history:1d' ORDER BY 1"""
        ).fetchall()
    assert [row[0] for row in sessions] == [today.isoformat()]


def test_security_history_deduplicates_cached_dates_and_granularity(client):
    today = datetime.now(timezone.utc).date()
    first_date = (today - timedelta(days=30)).isoformat()
    last_date = today.isoformat()
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO price_observations(symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', ?, ?, 'fixture provider', ?)""",
            [
                ("90", first_date, "history:1mo"),
                ("100", last_date, "history:1mo"),
                ("101", last_date, "history:1y"),
                ("102", f"{last_date}T15:00:00+00:00", "history:1d"),
            ],
        )

    response = client.get("/api/prices/ACME/history?period=1mo")

    assert response.status_code == 200
    points = response.json()["points"]
    assert points == [
        {"date": first_date, "close": "90"},
        {"date": last_date, "close": "100"},
    ]


def test_api_requests_are_logged_without_query_values(client, monkeypatch, caplog):
    caplog.set_level("INFO")

    class SearchProvider:
        def search(self, query):
            return []

    monkeypatch.setattr(main, "PROVIDER", SearchProvider())

    response = client.get("/api/securities/search?q=private-search-term")

    assert response.status_code == 200
    api_logs = [record.getMessage() for record in caplog.records if record.name == "uvicorn.error"]
    assert any("API request completed method=GET path=/api/securities/search status=200" in message for message in api_logs)
    assert "private-search-term" not in "\n".join(api_logs)


def test_external_provider_calls_log_success_and_failure(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(market_data.yf, "Search", lambda *args, **kwargs: type("Result", (), {"quotes": []})())
    provider = market_data.YahooFinanceProvider()

    assert provider.search("private-search-term") == []
    assert "External call completed provider=Yahoo Finance (yfinance) operation=search" in caplog.text
    assert "private-search-term" not in caplog.text

    def fail_search(*args, **kwargs):
        raise RuntimeError("provider response containing request data")

    monkeypatch.setattr(market_data.yf, "Search", fail_search)
    with pytest.raises(RuntimeError):
        provider.search("another-private-term")
    assert "External call failed provider=Yahoo Finance (yfinance) operation=search" in caplog.text
    assert "another-private-term" not in caplog.text
    assert "provider response containing request data" not in caplog.text


def test_security_search_preserves_exchange_qualified_symbol(client, monkeypatch):
    class SearchProvider:
        calls = []

        def search(self, query):
            self.calls.append(query)
            return [{
                "symbol": "NOVO-B.CO", "name": "Novo Nordisk B", "exchange": "Copenhagen",
                "quote_type": "EQUITY", "isin": None, "currency": "DKK",
            }]

    provider = SearchProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)

    response = client.get("/api/securities/search?q=Novo")
    cached_response = client.get("/api/securities/search?q=novo")

    assert response.status_code == 200
    assert response.json()[0]["symbol"] == "NOVO-B.CO"
    assert response.json()[0]["exchange"] == "Copenhagen"
    assert response.json()[0]["currency"] == "DKK"
    assert cached_response.json() == response.json()
    assert provider.calls == ["Novo"]


def test_security_search_refreshes_expired_results(client, monkeypatch):
    class SearchProvider:
        calls = 0

        def search(self, query):
            self.calls += 1
            return []

    provider = SearchProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)
    assert client.get("/api/securities/search?q=ACME").json() == []

    expired_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE security_search_cache SET cached_at=? WHERE query='acme'", (expired_at,))

    assert client.get("/api/securities/search?q=ACME").json() == []
    assert provider.calls == 2


def test_current_quote_and_fx_requests_reuse_fresh_observations(client, monkeypatch):
    class CurrentProvider:
        name = "fixture provider"
        quote_calls = 0
        fx_calls = 0

        def quote(self, symbol):
            self.quote_calls += 1
            return Decimal("101.25"), "USD", datetime.now(timezone.utc).isoformat()

        def fx_rate(self, base_currency, quote_currency):
            self.fx_calls += 1
            return Decimal("1.2"), datetime.now(timezone.utc).isoformat()

    provider = CurrentProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)

    first_quote = client.get("/api/prices/ACME")
    cached_quote = client.get("/api/prices/ACME")
    first_fx = client.get("/api/fx/EUR/USD/current")
    cached_fx = client.get("/api/fx/EUR/USD/current")

    assert first_quote.json() == cached_quote.json()
    assert first_fx.json() == cached_fx.json()
    assert (provider.quote_calls, provider.fx_calls) == (1, 1)

    expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE price_observations SET observed_at=? WHERE symbol='ACME'", (expired_at,))
        connection.execute("UPDATE fx_observations SET observed_at=? WHERE base_currency='EUR'", (expired_at,))

    client.get("/api/prices/ACME")
    client.get("/api/fx/EUR/USD/current")
    assert (provider.quote_calls, provider.fx_calls) == (2, 2)


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
    assert Decimal(summary["market_value"]) == Decimal("264")
    assert Decimal(summary["unrealized_pl"]) == Decimal("22.8")
    assert Decimal(summary["realized_pl"]) == 0
    assert Decimal(summary["total_return"]) == Decimal("22.8")
    assert Decimal(summary["cash_balance"]) == Decimal("958.8")
    assert Decimal(summary["total_value"]) == Decimal("1222.8")
    holding = client.get("/api/portfolio/holdings").json()[0]
    assert Decimal(holding["market_value"]) == Decimal("220")
    assert holding["market_value_currency"] == "EUR"
    assert Decimal(holding["market_value_reporting"]) == Decimal("264")
    assert holding["market_value_reporting_currency"] == "USD"


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
        "symbol": "ACME", "quantity": "2", "price": "100", "currency": "USD", "fees": "1", "taxes": "0.5",
    })

    response = client.get("/api/portfolio/history?period=max")

    assert response.status_code == 200
    assert response.json()["points"] == [
        {"date": "2025-01-01", "value": "1000", "roi": "0", "roi_amount": "0"},
        {"date": "2025-01-02", "value": "998.5", "roi": "-0.1", "roi_amount": "-1.0"},
        {"date": "2025-01-03", "value": "1038.5", "roi": "3.9", "roi_amount": "39.0"},
    ]


def test_portfolio_history_reports_profit_without_deposit_transactions(client, monkeypatch):
    class HistoryProvider:
        name = "fixture provider"

        def history(self, symbol, period):
            return "USD", [
                {"date": "2025-01-02", "close": "100"},
                {"date": "2025-01-03", "close": "120"},
            ]

    monkeypatch.setattr(main, "PROVIDER", HistoryProvider())
    account = client.post("/api/accounts", json={"name": "Buy only"}).json()
    client.post("/api/transactions", json={
        "account_id": account["id"], "type": "buy", "transaction_date": "2025-01-02",
        "symbol": "ACME", "quantity": "2", "price": "100", "currency": "USD", "fees": "0",
    })

    response = client.get("/api/portfolio/history?period=max")

    assert response.status_code == 200
    points = response.json()["points"]
    assert [point["roi_amount"] for point in points] == ["0", "40"]
    assert all(point["roi"] is None for point in points)


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
