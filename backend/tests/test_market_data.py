import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import database, main, market_data, secret_storage


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


def test_missing_price_history_returns_empty_result_and_is_cached(client, monkeypatch):
    class NoHistoryProvider:
        name = "fixture provider"
        calls = 0

        def history(self, symbol, period):
            self.calls += 1
            raise market_data.NoPriceHistoryError("No price history available")

    provider = NoHistoryProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)

    first = client.get("/api/prices/ACME/history?period=1y")
    second = client.get("/api/prices/ACME/history?period=1y")

    assert first.status_code == 200
    assert first.json()["points"] == []
    assert second.status_code == 200
    assert second.json()["points"] == []
    assert provider.calls == 1


def test_missing_symbol_history_does_not_fail_portfolio_history(client, monkeypatch):
    account = client.post("/api/accounts", json={"name": "No history"}).json()
    transaction = client.post("/api/transactions", json={
        "account_id": account["id"], "type": "buy", "transaction_date": "2026-01-01",
        "symbol": "ACME", "quantity": "1", "price": "100", "currency": "USD",
    })
    assert transaction.status_code == 201

    class NoHistoryProvider:
        name = "fixture provider"

        def history(self, symbol, period):
            raise market_data.NoPriceHistoryError("No price history available")

    monkeypatch.setattr(main, "PROVIDER", NoHistoryProvider())

    response = client.get("/api/portfolio/history?period=1mo")

    assert response.status_code == 200


def test_one_week_price_history_excludes_one_day_intraday_observations(client):
    today = datetime.now(timezone.utc).date().isoformat()
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', ?, ?, 'fixture provider', ?)""",
            [
                ("101", f"{today}T14:30:00+00:00", "history:1d"),
                ("102", f"{today}T14:35:00+00:00", "history:1d"),
                ("98", today, "history:1mo"),
                ("100", today, "history:5d"),
            ],
        )

        rows = main.load_price_history_rows(connection, "ACME", "fixture provider", "5d")

    assert [(row["interval"], row["observed_at"]) for row in rows] == [("history:5d", today)]


def test_one_week_portfolio_history_uses_only_timestamped_weekly_observations(client):
    today = datetime.now(timezone.utc).date()
    account = client.post("/api/accounts", json={"name": "Weekly chart"}).json()
    transaction = client.post("/api/transactions", json={
        "account_id": account["id"], "type": "buy", "transaction_date": today.isoformat(),
        "symbol": "ACME", "quantity": "1", "price": "100", "currency": "USD",
    })
    assert transaction.status_code == 201

    point_dates = [
        (today - timedelta(days=5)).isoformat() + "T15:00:00+00:00",
        datetime.combine(today, datetime.min.time(), timezone.utc).replace(hour=15).isoformat(),
        datetime.combine(today, datetime.min.time(), timezone.utc).replace(hour=15, minute=30).isoformat(),
    ]
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '100', ?, 'fixture provider', 'history:5d')""",
            [(point_date,) for point_date in point_dates],
        )
        main.mark_history_refreshed(connection, "ACME", "fixture provider", "5d")

    response = client.get("/api/portfolio/history?period=5d")

    assert response.status_code == 200
    dates = [point["date"] for point in response.json()["points"]]
    assert dates == point_dates


def test_one_week_history_refresh_uses_the_one_week_cache_interval(client):
    with database.connect() as connection:
        # A pre-30-minute weekly refresh marker must not validate the old hourly cache.
        connection.execute(
            """INSERT INTO history_cache_refreshes(symbol, period, provider, refreshed_at)
               VALUES ('ACME', '1w', 'fixture provider', ?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        assert main.history_refresh_is_due(connection, "ACME", "fixture provider", "5d") is True

        main.mark_history_refreshed(connection, "ACME", "fixture provider", "5d")
        cached_period = connection.execute(
            "SELECT period FROM history_cache_refreshes WHERE symbol='ACME' AND period='1w-30m'"
        ).fetchone()["period"]
        assert cached_period == "1w-30m"
        assert main.history_refresh_is_due(connection, "ACME", "fixture provider", "5d") is False

        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        connection.execute(
            "UPDATE history_cache_refreshes SET refreshed_at=? WHERE symbol='ACME'",
            (expired_at,),
        )
        assert main.history_refresh_is_due(connection, "ACME", "fixture provider", "5d") is True


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
        main.mark_history_refreshed(connection, "ACME", "fixture provider", "1mo")

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
    assert response.json() == {"results": [], "warnings": []}
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


def test_yahoo_week_history_requests_half_hour_bars(monkeypatch):
    start = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)

    class IndexValue:
        def __init__(self, value):
            self.value = value

        def to_pydatetime(self):
            return self.value

    class HistoryFrame:
        empty = False

        def iterrows(self):
            return iter([
                (IndexValue(start), {"Close": 100}),
                (IndexValue(start + timedelta(minutes=30)), {"Close": 101}),
            ])

    class Ticker:
        fast_info = {"currency": "USD"}

        def history(self, **kwargs):
            self.request = kwargs
            return HistoryFrame()

    ticker = Ticker()
    monkeypatch.setattr(market_data.yf, "Ticker", lambda symbol: ticker)

    currency, points = market_data.YahooFinanceProvider().history("ACME", "5d")

    assert ticker.request["period"] == "5d"
    assert ticker.request["interval"] == "30m"
    assert currency == "USD"
    assert points[1]["date"] == (start + timedelta(minutes=30)).isoformat()


def test_storing_week_history_replaces_existing_hourly_observations(client):
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '99', '2026-10-30T14:00:00+00:00',
                       'fixture provider', 'history:5d')"""
        )

        main.store_price_history(
            connection,
            "ACME",
            "USD",
            [
                {"date": "2026-10-30T14:00:00+00:00", "close": "100"},
                {"date": "2026-10-30T14:30:00+00:00", "close": "101"},
            ],
            "fixture provider",
            "5d",
        )
        rows = connection.execute(
            "SELECT price, observed_at FROM price_observations WHERE symbol='ACME' AND interval='history:5d' ORDER BY observed_at"
        ).fetchall()

    assert [(row["price"], row["observed_at"]) for row in rows] == [
        ("100", "2026-10-30T14:00:00+00:00"),
        ("101", "2026-10-30T14:30:00+00:00"),
    ]


def test_week_history_refreshes_a_usable_hourly_cache(client, monkeypatch):
    today = datetime.now(timezone.utc).date()
    new_points = [
        {"date": datetime.combine(today, datetime.min.time(), timezone.utc).replace(hour=15).isoformat(), "close": "101"},
        {"date": datetime.combine(today, datetime.min.time(), timezone.utc).replace(hour=15, minute=30).isoformat(), "close": "102"},
    ]

    class HalfHourProvider:
        name = "fixture provider"
        calls = 0

        def history(self, symbol, period):
            self.calls += 1
            assert period == "5d"
            return "USD", new_points

    provider = HalfHourProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO price_observations
               (symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '100', ?, 'fixture provider', 'history:5d')""",
            [
                ((today - timedelta(days=5)).isoformat() + "T15:00:00+00:00",),
                ((today - timedelta(days=1)).isoformat() + "T14:30:00+00:00",),
                ((today - timedelta(days=1)).isoformat() + "T15:30:00+00:00",),
            ],
        )
        # A fresh legacy marker exists, but the 30-minute cache generation must refresh once.
        connection.execute(
            """INSERT INTO history_cache_refreshes(symbol, period, provider, refreshed_at)
               VALUES ('ACME', '1w', 'fixture provider', ?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )

    response = client.get("/api/prices/ACME/history?period=5d")

    assert response.status_code == 200
    assert provider.calls == 1
    assert response.json()["points"] == new_points
    with database.connect() as connection:
        stored = connection.execute(
            """SELECT observed_at FROM price_observations
               WHERE symbol='ACME' AND interval='history:5d' ORDER BY observed_at"""
        ).fetchall()
    assert [row["observed_at"] for row in stored] == [point["date"] for point in new_points]


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
    assert response.json()["results"][0]["symbol"] == "NOVO-B.CO"
    assert response.json()["results"][0]["exchange"] == "Copenhagen"
    assert response.json()["results"][0]["currency"] == "DKK"
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
    assert client.get("/api/securities/search?q=ACME").json() == {"results": [], "warnings": []}

    expired_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE security_search_cache SET cached_at=? WHERE query='acme'", (expired_at,))

    assert client.get("/api/securities/search?q=ACME").json() == {"results": [], "warnings": []}
    assert provider.calls == 2


def test_isin_validation_checks_luhn_checksum():
    assert main.is_valid_isin("US0378331005")
    assert main.is_valid_isin("us0378331005")
    assert not main.is_valid_isin("US0378331006")
    assert not main.is_valid_isin("US03783310O5")
    assert not main.is_valid_isin("US037833100")


def test_openfigi_unconfirmed_symbols_are_not_sent_to_yahoo(monkeypatch):
    def unexpected_ticker(symbol):
        raise AssertionError(f"Unexpected Yahoo request for {symbol}")

    monkeypatch.setattr(market_data.yf, "Ticker", unexpected_ticker)
    provider = market_data.YahooFinanceProvider()

    with pytest.raises(ValueError, match="no confirmed Yahoo Finance symbol"):
        provider.quote("NOVO@NYSE")
    with pytest.raises(ValueError, match="no confirmed Yahoo Finance symbol"):
        provider.history("NOVO@NYSE", "1y")


def test_openfigi_provider_maps_isin_without_logging_api_key(monkeypatch, caplog):
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["api_key"] = request.get_header("X-openfigi-apikey")
        captured["timeout"] = timeout
        return Response(json.dumps([{"data": [
            {"ticker": "NOVO-B", "name": "Novo Nordisk", "exchCode": "XCSE", "securityType": "Common Stock"},
            {"name": "Missing ticker"},
        ]}]).encode())

    monkeypatch.setattr(market_data, "urlopen", fake_urlopen)
    caplog.set_level("INFO")

    candidates = market_data.OpenFigiProvider().search_isin("US0378331005", "private-api-key")

    assert captured == {
        "url": "https://api.openfigi.com/v3/mapping",
        "body": [{"idType": "ID_ISIN", "idValue": "US0378331005"}],
        "api_key": "private-api-key",
        "timeout": 10,
    }
    assert candidates == [{
        "symbol": "NOVO-B", "name": "Novo Nordisk", "exchange": "XCSE",
        "quote_type": "Common Stock", "isin": "US0378331005", "currency": None,
        "yahoo_search_match": False, "source": "OpenFIGI", "source_symbol": "NOVO-B",
    }]
    assert "private-api-key" not in caplog.text
    assert "US0378331005" not in caplog.text


def test_openfigi_settings_key_is_optional_and_never_returned(client):
    assert client.get("/api/settings").json() == {
        "reporting_currency": "USD", "openfigi_configured": False,
    }
    response = client.put(
        "/api/settings",
        json={"openfigi_api_key": "private-api-key"},
    )

    assert response.status_code == 200
    assert response.json() == {"openfigi_configured": True}
    assert "private-api-key" not in response.text
    assert "private-api-key" not in client.get("/api/settings").text

    removed = client.put("/api/settings", json={"openfigi_api_key": ""})
    assert removed.json() == {"openfigi_configured": False}
    assert client.get("/api/settings").json()["openfigi_configured"] is False


def test_openfigi_provider_omits_api_key_header_when_keyless(monkeypatch):
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    captured = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = request.headers
        captured["timeout"] = timeout
        return Response(json.dumps([{"data": []}]).encode())

    monkeypatch.setattr(market_data, "urlopen", fake_urlopen)

    assert market_data.OpenFigiProvider().search_isin("US0378331005") == []
    assert captured["headers"] == {"Content-type": "application/json"}
    assert captured["timeout"] == 10


def test_openfigi_mapping_without_yahoo_listing_is_not_returned(client, monkeypatch):
    class SearchProvider:
        def search(self, query):
            return []

    class MappingProvider:
        calls = []

        def search_isin(self, isin, api_key):
            self.calls.append((isin, api_key))
            return [{
                "symbol": "ACME", "name": "Acme", "exchange": "XNYS",
                "quote_type": "Common Stock", "isin": isin, "currency": "USD",
                "yahoo_search_match": False, "source": "OpenFIGI", "source_symbol": "ACME",
            }]

    mapping_provider = MappingProvider()
    monkeypatch.setattr(main, "PROVIDER", SearchProvider())
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", mapping_provider)

    response = client.get("/api/securities/search?q=US0378331005")

    assert response.status_code == 200
    assert mapping_provider.calls == [("US0378331005", None)]
    assert response.json()["results"] == []


def test_openfigi_api_key_is_encrypted_in_database(client):
    private_key = "private-api-key"

    response = client.put(
        "/api/settings",
        json={"openfigi_api_key": private_key},
    )

    assert response.status_code == 200
    with database.connect() as connection:
        stored_value = connection.execute(
            "SELECT value FROM settings WHERE key='openfigi_api_key'"
        ).fetchone()["value"]
    assert stored_value.startswith(secret_storage.ENCRYPTED_VALUE_PREFIX)
    assert private_key not in stored_value
    assert secret_storage.decrypt_secret(main.ENCRYPTION_FERNET, stored_value) == private_key


def test_legacy_plaintext_openfigi_key_is_migrated(client):
    private_key = "legacy-api-key"
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES ('openfigi_api_key', ?)",
            (private_key,),
        )
        secret_storage.migrate_openfigi_key(connection, main.ENCRYPTION_FERNET)
        stored_value = connection.execute(
            "SELECT value FROM settings WHERE key='openfigi_api_key'"
        ).fetchone()["value"]

    assert stored_value.startswith(secret_storage.ENCRYPTED_VALUE_PREFIX)
    assert private_key not in stored_value
    assert secret_storage.decrypt_secret(main.ENCRYPTION_FERNET, stored_value) == private_key


def test_generated_encryption_key_is_persisted_in_home_assistant_options(monkeypatch, caplog):
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    captured = {"encryption_key": ""}
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.method, request.full_url, timeout))
        if request.method == "GET":
            return Response(json.dumps({"result": "ok", "data": captured}).encode())
        payload = json.loads(request.data)
        captured.update(payload["options"])
        return Response(json.dumps({"result": "ok"}).encode())

    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-supervisor-token")
    monkeypatch.setattr(secret_storage, "urlopen", fake_urlopen)
    caplog.set_level("INFO")

    key = secret_storage.load_or_create_fernet(Path("/tmp/folio-secret-test"))

    assert captured["encryption_key"]
    assert key.decrypt(key.encrypt(b"api-key")) == b"api-key"
    assert requests == [
        ("GET", "http://supervisor/addons/self/options/config", 10),
        ("POST", "http://supervisor/addons/self/options", 10),
    ]
    assert "private-supervisor-token" not in caplog.text
    assert captured["encryption_key"] not in caplog.text


def test_configured_encryption_key_is_used_without_rewriting_options(monkeypatch):
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    configured_key = Fernet.generate_key().decode("ascii")
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request.method)
        return Response(json.dumps({"result": "ok", "data": {"encryption_key": configured_key}}).encode())

    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-supervisor-token")
    monkeypatch.setattr(secret_storage, "urlopen", fake_urlopen)

    key = secret_storage.load_or_create_fernet(Path("/tmp/folio-secret-test"))

    assert key.decrypt(key.encrypt(b"test")) == b"test"
    assert requests == ["GET"]


def test_missing_home_assistant_key_does_not_replace_key_needed_for_saved_credentials(monkeypatch):
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request.method)
        return Response(json.dumps({"result": "ok", "data": {"encryption_key": ""}}).encode())

    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-supervisor-token")
    monkeypatch.setattr(secret_storage, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="restore the original key"):
        secret_storage.load_or_create_fernet(Path("/tmp/folio-secret-test"), has_encrypted_secrets=True)

    assert requests == ["GET"]


def test_invalid_openfigi_key_is_not_echoed_in_validation_response(client):
    private_key = "x" * 513

    response = client.put("/api/settings", json={"openfigi_api_key": private_key})

    assert response.status_code == 422
    assert "OpenFIGI API key is too long" in response.text
    assert private_key not in response.text


def test_isin_search_merges_yahoo_and_openfigi_candidates(client, monkeypatch):
    class SearchProvider:
        def search(self, query):
            if query == "NOVO-B":
                return [{
                    "symbol": "NOVO-B.CO", "name": "Novo Nordisk B", "exchange": "Copenhagen",
                    "quote_type": "EQUITY", "isin": None, "currency": "DKK",
                    "yahoo_search_match": True, "source": "Yahoo Finance",
                }]
            assert query == "NOVO"
            return []

    class MappingProvider:
        def search_isin(self, isin, api_key):
            assert isin == "US0378331005"
            assert api_key == "private-api-key"
            return [
                {"symbol": "NOVO-B", "name": "Novo Nordisk", "exchange": "Copenhagen", "quote_type": "Common Stock",
                 "isin": isin, "currency": None, "yahoo_search_match": False, "source": "OpenFIGI"},
                {"symbol": "NOVO", "name": "Novo ADR", "exchange": "NYSE", "quote_type": "ADR",
                 "isin": isin, "currency": "USD", "yahoo_search_match": False, "source": "OpenFIGI"},
                {"symbol": "NOVO", "name": "Novo listing", "exchange": "LSE", "quote_type": "Common Stock",
                 "isin": isin, "currency": "GBP", "yahoo_search_match": False, "source": "OpenFIGI"},
            ]

    monkeypatch.setattr(main, "PROVIDER", SearchProvider())
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", MappingProvider())
    client.put("/api/settings", json={"openfigi_api_key": "private-api-key"})

    response = client.get("/api/securities/search?q=us0378331005")
    candidates = response.json()["results"]

    assert response.status_code == 200
    assert response.json()["warnings"] == []
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "NOVO-B.CO"
    assert candidates[0]["yahoo_search_match"] is True
    assert candidates[0]["source"] == "OpenFIGI and Yahoo Finance"


def test_isin_search_discovers_yahoo_listings_by_openfigi_ticker(client, monkeypatch):
    class SearchProvider:
        calls = []

        def search(self, query):
            self.calls.append(query)
            if query == "AEM":
                return [{
                    "symbol": "AEM", "name": "Agnico Eagle Mines", "exchange": "NYSE",
                    "quote_type": "EQUITY", "isin": None, "currency": "USD",
                    "yahoo_search_match": True, "source": "Yahoo Finance",
                }]
            if query == "AE9":
                return [
                    {
                        "symbol": "AE9.F", "name": "Agnico Eagle Mines", "exchange": "Frankfurt",
                        "quote_type": "EQUITY", "isin": None, "currency": "EUR",
                        "yahoo_search_match": True, "source": "Yahoo Finance",
                    },
                    {
                        "symbol": "AE90.F", "name": "Unrelated security", "exchange": "Frankfurt",
                        "quote_type": "EQUITY", "isin": None, "currency": "EUR",
                        "yahoo_search_match": True, "source": "Yahoo Finance",
                    },
                ]
            assert query == "NOTLISTED"
            return []

    class MappingProvider:
        def search_isin(self, isin, api_key):
            return [
                {"symbol": "AEM", "name": "Agnico Eagle Mines", "exchange": "XNYS",
                 "quote_type": "Common Stock", "isin": isin, "currency": "USD"},
                {"symbol": "AE9", "name": "Agnico Eagle Mines", "exchange": "XFRA",
                 "quote_type": "Common Stock", "isin": isin, "currency": "EUR"},
                {"symbol": "NOTLISTED", "name": "No Yahoo listing", "exchange": "XTSE",
                 "quote_type": "Common Stock", "isin": isin, "currency": "CAD"},
            ]

    provider = SearchProvider()
    monkeypatch.setattr(main, "PROVIDER", provider)
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", MappingProvider())

    response = client.get("/api/securities/search?q=US0378331005")

    assert response.status_code == 200
    assert [item["symbol"] for item in response.json()["results"]] == ["AEM", "AE9.F"]
    assert response.json()["results"][1]["source"] == "OpenFIGI and Yahoo Finance"
    assert provider.calls == ["AEM", "AE9", "NOTLISTED"]


def test_openfigi_is_always_used_keyless_but_unsupported_mapping_is_hidden(client, monkeypatch):
    class SearchProvider:
        def search(self, query):
            return []

    class MappingProvider:
        def search_isin(self, isin, api_key):
            assert isin == "US0378331005"
            assert api_key is None
            return [{
                "symbol": "ACME", "name": "Acme", "exchange": "XNYS",
                "quote_type": "Common Stock", "isin": isin, "currency": "USD",
                "yahoo_search_match": False, "source": "OpenFIGI", "source_symbol": "ACME",
            }]

    monkeypatch.setattr(main, "PROVIDER", SearchProvider())
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", MappingProvider())
    response = client.get("/api/securities/search?q=US0378331005")

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_invalid_isin_never_calls_openfigi(client, monkeypatch):
    class SearchProvider:
        def search(self, query):
            assert query == "US0378331006"
            return []

    class MappingProvider:
        def search_isin(self, isin, api_key):
            raise AssertionError("Invalid ISIN must not be sent to OpenFIGI")

    monkeypatch.setattr(main, "PROVIDER", SearchProvider())
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", MappingProvider())
    client.put("/api/settings", json={"openfigi_api_key": "private-api-key"})

    response = client.get("/api/securities/search?q=US0378331006")

    assert response.status_code == 200
    assert response.json() == {"results": [], "warnings": []}


def test_openfigi_settings_changes_invalidate_search_cache(client, monkeypatch):
    class SearchProvider:
        def search(self, query):
            return []

    monkeypatch.setattr(main, "PROVIDER", SearchProvider())
    assert client.get("/api/securities/search?q=ACME").status_code == 200
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_search_cache").fetchone()[0] == 1

    response = client.put("/api/settings", json={"openfigi_api_key": "private-api-key"})

    assert response.status_code == 200
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_search_cache").fetchone()[0] == 0


def test_openfigi_failure_reports_unavailable_mapping_without_yahoo_isin_search(client, monkeypatch):
    class SearchProvider:
        calls = 0

        def search(self, query):
            self.calls += 1
            return [{
                "symbol": "ACME", "name": "Acme", "exchange": "NYSE", "quote_type": "EQUITY",
                "isin": None, "currency": "USD", "yahoo_search_match": True, "source": "Yahoo Finance",
            }]

    class MappingProvider:
        calls = 0

        def search_isin(self, isin, api_key):
            self.calls += 1
            raise RuntimeError("private-api-key and ISIN in provider error")

    yahoo_provider = SearchProvider()
    mapping_provider = MappingProvider()
    monkeypatch.setattr(main, "PROVIDER", yahoo_provider)
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", mapping_provider)
    client.put("/api/settings", json={"openfigi_api_key": "private-api-key"})

    first = client.get("/api/securities/search?q=US0378331005")
    second = client.get("/api/securities/search?q=US0378331005")

    assert first.status_code == 200
    assert first.json()["results"] == []
    assert first.json()["warnings"] == [
        "OpenFIGI mappings could not be loaded; Yahoo Finance could not validate mapped tickers.",
    ]
    assert second.json()["warnings"] == first.json()["warnings"]
    assert mapping_provider.calls == 2
    assert yahoo_provider.calls == 0


def test_openfigi_rate_limit_warning_recommends_optional_api_key(client, monkeypatch):
    class SearchProvider:
        def search(self, query):
            return [{
                "symbol": "ACME", "name": "Acme", "exchange": "NYSE", "quote_type": "EQUITY",
                "isin": None, "currency": "USD", "yahoo_search_match": True, "source": "Yahoo Finance",
            }]

    class MappingProvider:
        def search_isin(self, isin, api_key):
            raise HTTPError(
                market_data.OpenFigiProvider.endpoint,
                429,
                "Too Many Requests",
                None,
                None,
            )

    monkeypatch.setattr(main, "PROVIDER", SearchProvider())
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", MappingProvider())

    response = client.get("/api/securities/search?q=US0378331005")

    assert response.status_code == 200
    assert response.json()["results"] == []
    assert response.json()["warnings"] == [
        "OpenFIGI rate limit reached; ISIN mappings could not be loaded. "
        "An API key provides a higher rate limit—configure one in Settings if you have not already."
    ]


def test_non_isin_search_returns_error_when_yahoo_fails(client, monkeypatch):
    class SearchProvider:
        def search(self, query):
            raise RuntimeError("Yahoo failure")

    class MappingProvider:
        def search_isin(self, isin, api_key):
            raise AssertionError("OpenFIGI must not be called for a non-ISIN search")

    monkeypatch.setattr(main, "PROVIDER", SearchProvider())
    monkeypatch.setattr(main, "OPENFIGI_PROVIDER", MappingProvider())
    client.put("/api/settings", json={"openfigi_api_key": "private-api-key"})

    response = client.get("/api/securities/search?q=ACME")

    assert response.status_code == 502
    assert response.json()["detail"] == "Security search is unavailable"


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
    account = client.post("/api/accounts", json={"name": "Euro account"}).json()
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


def test_ytd_portfolio_roi_excludes_pre_year_returns_and_nets_period_contributions(client, monkeypatch):
    today = datetime.now(timezone.utc).date()
    contribution_date = today - timedelta(days=10)
    class HistoryProvider:
        name = "fixture provider"

        def history(self, symbol, period):
            assert period == "ytd"
            return "EUR", [
                {"date": f"{today.year}-01-02", "close": "100"},
                {"date": contribution_date.isoformat(), "close": "105"},
                {"date": today.isoformat(), "close": "110"},
            ]

    monkeypatch.setattr(main, "PROVIDER", HistoryProvider())
    assert client.put("/api/settings", params={"reporting_currency": "EUR"}).status_code == 200
    account = client.post("/api/accounts", json={"name": "Long term"}).json()
    client.post("/api/transactions", json={
        "account_id": account["id"], "type": "deposit",
        "transaction_date": f"{today.year - 1}-12-01", "currency": "EUR", "cash_amount": "150000",
    })
    client.post("/api/transactions", json={
        "account_id": account["id"], "type": "buy",
        "transaction_date": f"{today.year - 1}-12-02", "symbol": "ACME",
        "quantity": "1000", "price": "150", "currency": "EUR",
    })
    client.post("/api/transactions", json={
        "account_id": account["id"], "type": "deposit",
        "transaction_date": contribution_date.isoformat(), "currency": "EUR", "cash_amount": "5000",
    })

    response = client.get("/api/portfolio/history?period=ytd")

    assert response.status_code == 200
    points = response.json()["points"]
    assert [point["roi_amount"] for point in points] == ["0", "5000", "10000"]
    assert [point["roi"] for point in points] == [
        "0", "4.761904761904761904761904762", "9.523809523809523809523809524",
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
