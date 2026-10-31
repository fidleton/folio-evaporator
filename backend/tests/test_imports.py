from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import database, main
from app.main import app, transaction_fingerprint


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "folio.db")
    with TestClient(app) as test_client:
        yield test_client


def test_csv_commit_and_reimport_detect_duplicate(client):
    account = client.post("/api/accounts", json={"name": "Long term"})
    assert account.status_code == 201
    assert set(account.json()) == {"id", "name", "created_at"}

    csv_content = (
        "date,account,symbol,type,quantity,price,currency,fees,taxes,cash_amount,notes\n"
        "2024-01-02,Long term,ACME,buy,10,12.5,USD,1,0.25,,\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        files={"file": ("activity.csv", csv_content, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["valid_rows"] == 1

    committed = client.post(f"/api/imports/{preview.json()['id']}/commit")
    assert committed.json()["imported_count"] == 1
    assert client.get("/api/portfolio/holdings").json()[0]["quantity"] == "10"
    assert client.get("/api/securities").json()[0]["symbol"] == "ACME"
    assert client.get("/api/transactions").json()[0]["taxes"] == "0.25"

    reimport = client.post(
        "/api/imports/csv/preview",
        files={"file": ("activity.csv", csv_content, "text/csv")},
    )
    assert reimport.json()["valid_rows"] == 0
    assert "Duplicate transaction" in reimport.json()["rows"][0]["errors"]


def test_frontend_index_responses_require_revalidation(client, tmp_path, monkeypatch):
    frontend_dist = tmp_path / "frontend"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<main>new bundle</main>")
    monkeypatch.setattr(main, "FRONTEND_DIST", frontend_dist)

    for path in ("/", "/index.html", "/overview"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_csv_import_preserves_transaction_time(client):
    client.post("/api/accounts", json={"name": "Long term"})
    csv_content = (
        "date,account,symbol,type,quantity,price,currency,fees,cash_amount,notes\n"
        "2024-01-02T14:35:00,Long term,ACME,buy,10,12.5,USD,1\n"
    )

    preview = client.post(
        "/api/imports/csv/preview",
        files={"file": ("activity.csv", csv_content, "text/csv")},
    )

    assert preview.status_code == 200
    assert preview.json()["rows"][0]["transaction_date"] == "2024-01-02T14:35:00+00:00"
    committed = client.post(f"/api/imports/{preview.json()['id']}/commit")
    assert committed.json()["imported_count"] == 1
    assert client.get("/api/transactions").json()[0]["transaction_date"] == "2024-01-02T14:35:00+00:00"


def test_reporting_currency_persists_after_database_initialization(client):
    response = client.put("/api/settings", params={"reporting_currency": "eur"})

    assert response.status_code == 200
    assert response.json() == {"reporting_currency": "EUR"}

    database.initialize()

    summary = client.get("/api/portfolio/summary")
    assert summary.json()["reporting_currency"] == "EUR"


def test_time_format_defaults_to_24_hour_and_persists(client):
    assert client.get("/api/portfolio/summary").json()["time_format"] == "24-hour"

    response = client.put("/api/settings", params={"time_format": "12-hour"})

    assert response.status_code == 200
    assert response.json() == {"time_format": "12-hour"}
    assert client.get("/api/portfolio/summary").json()["time_format"] == "12-hour"
    assert client.put("/api/settings", params={"time_format": "digital"}).status_code == 422


def test_transactions_can_be_created_updated_and_deleted_manually(client):
    account = client.post("/api/accounts", json={"name": "Long term"}).json()
    transaction = {
        "account_id": account["id"], "type": "buy", "transaction_date": "2024-01-02T14:35:00",
        "symbol": "ACME", "security_name": "Acme Corporation", "isin": "us0378331005",
        "quantity": "10", "price": "12.5", "currency": "USD",
        "fees": "1", "taxes": "0.25", "cash_amount": None, "notes": "Initial purchase",
    }

    created = client.post("/api/transactions", json=transaction)
    assert created.status_code == 201
    assert created.json()["transaction_date"] == "2024-01-02T14:35:00+00:00"
    assert created.json()["isin"] == "US0378331005"
    assert created.json()["taxes"] == "0.25"
    transaction_id = created.json()["id"]
    assert client.get("/api/portfolio/holdings").json()[0]["name"] == "Acme Corporation"

    updated = client.patch(
        f"/api/transactions/{transaction_id}",
        json={**transaction, "quantity": "8", "taxes": "0.5", "notes": "Corrected quantity"},
    )
    assert updated.status_code == 200
    assert updated.json()["quantity"] == "8"
    assert updated.json()["isin"] == "US0378331005"
    assert updated.json()["taxes"] == "0.5"
    assert updated.json()["notes"] == "Corrected quantity"
    assert client.get("/api/portfolio/holdings").json()[0]["quantity"] == "8"
    assert client.get("/api/portfolio/holdings").json()[0]["name"] == "Acme Corporation"

    deleted = client.delete(f"/api/transactions/{transaction_id}")
    assert deleted.status_code == 204
    assert client.get("/api/transactions").json() == []
    assert client.get("/api/portfolio/holdings").json() == []


def test_holdings_include_weighted_average_cost_after_buys_sells_and_splits(client):
    account_id = client.post("/api/accounts", json={"name": "Long term"}).json()["id"]
    base_transaction = {
        "account_id": account_id, "symbol": "ACME", "currency": "USD", "fees": "0",
    }
    transactions = [
        {**base_transaction, "type": "buy", "transaction_date": "2024-01-01", "quantity": "10", "price": "10", "fees": "2"},
        {**base_transaction, "type": "buy", "transaction_date": "2024-01-02", "quantity": "5", "price": "20", "fees": "3"},
        {**base_transaction, "type": "split", "transaction_date": "2024-01-03", "quantity": "2", "price": "0"},
        {**base_transaction, "type": "sell", "transaction_date": "2024-01-04", "quantity": "3", "price": "15"},
    ]
    for transaction in transactions:
        assert client.post("/api/transactions", json=transaction).status_code == 201

    holding = client.get("/api/portfolio/holdings").json()[0]

    assert holding["quantity"] == "27"
    assert Decimal(holding["average_cost"]) == Decimal("205") / Decimal("30")


def test_holdings_calculate_realized_unrealized_and_total_return(client):
    account_id = client.post("/api/accounts", json={"name": "Long term"}).json()["id"]
    base_transaction = {"account_id": account_id, "symbol": "ACME", "currency": "USD"}
    transactions = [
        {**base_transaction, "type": "buy", "transaction_date": "2024-01-01", "quantity": "10", "price": "10", "fees": "2"},
        {**base_transaction, "type": "sell", "transaction_date": "2024-01-02", "quantity": "4", "price": "15", "fees": "1"},
        {**base_transaction, "type": "dividend", "transaction_date": "2024-01-03", "cash_amount": "5", "fees": "0.25", "taxes": "0.5"},
    ]
    for transaction in transactions:
        assert client.post("/api/transactions", json=transaction).status_code == 201

    with database.connect() as connection:
        connection.execute(
            """INSERT INTO price_observations(symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '12', '2024-01-04', 'test', 'quote')"""
        )

    holding = client.get("/api/portfolio/holdings").json()[0]

    assert Decimal(holding["unrealized_pl"]) == Decimal("10.8")
    assert Decimal(holding["realized_pl"]) == Decimal("18.2")
    assert Decimal(holding["total_return"]) == Decimal("33.75")
    summary = client.get("/api/portfolio/summary").json()
    assert Decimal(summary["cash_balance"]) == Decimal("-38.75")


def test_holdings_calculate_return_for_short_positions(client):
    account_id = client.post("/api/accounts", json={"name": "Short account"}).json()["id"]
    base_transaction = {"account_id": account_id, "symbol": "ACME", "currency": "USD"}
    transactions = [
        {**base_transaction, "type": "sell", "transaction_date": "2024-01-01", "quantity": "3", "price": "15", "fees": "0.75"},
        {**base_transaction, "type": "buy", "transaction_date": "2024-01-02", "quantity": "1", "price": "12", "fees": "0.5"},
    ]
    for transaction in transactions:
        assert client.post("/api/transactions", json=transaction).status_code == 201

    with database.connect() as connection:
        connection.execute(
            """INSERT INTO price_observations(symbol, currency, price, observed_at, provider, interval)
               VALUES ('ACME', 'USD', '10', '2024-01-03', 'test', 'quote')"""
        )

    holding = client.get("/api/portfolio/holdings").json()[0]

    assert Decimal(holding["quantity"]) == Decimal("-2")
    assert Decimal(holding["realized_pl"]) == Decimal("2.25")
    assert Decimal(holding["unrealized_pl"]) == Decimal("9.5")
    assert Decimal(holding["total_return"]) == Decimal("11.75")


def test_closed_positions_preserve_realized_return(client):
    account_id = client.post("/api/accounts", json={"name": "Closed account"}).json()["id"]
    base_transaction = {"account_id": account_id, "symbol": "ACME", "currency": "USD"}
    transactions = [
        {**base_transaction, "type": "buy", "transaction_date": "2024-01-01", "quantity": "10", "price": "10"},
        {**base_transaction, "type": "sell", "transaction_date": "2024-01-02", "quantity": "10", "price": "12", "fees": "1"},
        {**base_transaction, "type": "dividend", "transaction_date": "2024-01-03", "cash_amount": "2"},
        {**base_transaction, "symbol": "BETA", "type": "buy", "transaction_date": "2024-01-04", "quantity": "2", "price": "20"},
    ]
    for transaction in transactions:
        assert client.post("/api/transactions", json=transaction).status_code == 201

    with database.connect() as connection:
        connection.execute(
            """INSERT INTO price_observations(symbol, currency, price, observed_at, provider, interval)
               VALUES ('BETA', 'USD', '25', '2024-01-05', 'test', 'quote')"""
        )

    open_positions = client.get("/api/portfolio/holdings").json()
    closed_positions = client.get("/api/portfolio/holdings?status=closed").json()
    assert [position["symbol"] for position in open_positions] == ["BETA"]
    assert [position["symbol"] for position in closed_positions] == ["ACME"]
    closed_position = closed_positions[0]

    assert Decimal(closed_position["quantity"]) == 0
    assert Decimal(closed_position["unrealized_pl"]) == 0
    assert Decimal(closed_position["realized_pl"]) == Decimal("19")
    assert Decimal(closed_position["total_return"]) == Decimal("21")
    summary = client.get("/api/portfolio/summary").json()
    assert Decimal(summary["market_value"]) == Decimal("50")
    assert Decimal(summary["unrealized_pl"]) == Decimal("10")
    assert Decimal(summary["realized_pl"]) == Decimal("19")
    assert Decimal(summary["total_return"]) == Decimal("31")


def test_csv_import_preserves_isin(client):
    client.post("/api/accounts", json={"name": "Long term"})
    csv_content = (
        "date,account,symbol,isin,type,quantity,price,currency\n"
        "2024-01-02,Long term,ACME,us0378331005,buy,10,12.5,USD\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        files={"file": ("activity.csv", csv_content, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["rows"][0]["isin"] == "US0378331005"
    client.post(f"/api/imports/{preview.json()['id']}/commit")
    assert client.get("/api/transactions").json()[0]["isin"] == "US0378331005"


def test_transaction_fingerprint_includes_isin_without_changing_legacy_rows():
    row = {
        "account_id": 1, "symbol": "ACME", "type": "buy", "transaction_date": "2024-01-02",
        "quantity": "10", "price": "12.5", "currency": "USD", "fees": "0",
        "cash_amount": None, "notes": "",
    }

    assert transaction_fingerprint(row) == transaction_fingerprint({**row, "isin": None})
    assert transaction_fingerprint({**row, "isin": "US0378331005"}) != transaction_fingerprint(
        {**row, "isin": "US5949181045"}
    )
