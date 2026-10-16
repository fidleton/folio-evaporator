import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "folio.db")
    with TestClient(app) as test_client:
        yield test_client


def test_csv_commit_and_reimport_detect_duplicate(client):
    account = client.post("/api/accounts", json={"name": "Long term", "broker": "Example"})
    assert account.status_code == 201

    csv_content = (
        "date,account,symbol,type,quantity,price,currency,fees,cash_amount,notes\n"
        "2024-01-02,Long term,ACME,buy,10,12.5,USD,1\n"
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

    reimport = client.post(
        "/api/imports/csv/preview",
        files={"file": ("activity.csv", csv_content, "text/csv")},
    )
    assert reimport.json()["valid_rows"] == 0
    assert "Duplicate transaction" in reimport.json()["rows"][0]["errors"]


def test_reporting_currency_persists_after_database_initialization(client):
    response = client.put("/api/settings", params={"reporting_currency": "eur"})

    assert response.status_code == 200
    assert response.json() == {"reporting_currency": "EUR"}

    database.initialize()

    summary = client.get("/api/portfolio/summary")
    assert summary.json()["reporting_currency"] == "EUR"


def test_transactions_can_be_created_updated_and_deleted_manually(client):
    account = client.post("/api/accounts", json={"name": "Long term"}).json()
    transaction = {
        "account_id": account["id"], "type": "buy", "transaction_date": "2024-01-02",
        "symbol": "ACME", "quantity": "10", "price": "12.5", "currency": "USD",
        "fees": "1", "cash_amount": None, "notes": "Initial purchase",
    }

    created = client.post("/api/transactions", json=transaction)
    assert created.status_code == 201
    transaction_id = created.json()["id"]

    updated = client.patch(
        f"/api/transactions/{transaction_id}",
        json={**transaction, "quantity": "8", "notes": "Corrected quantity"},
    )
    assert updated.status_code == 200
    assert updated.json()["quantity"] == "8"
    assert updated.json()["notes"] == "Corrected quantity"
    assert client.get("/api/portfolio/holdings").json()[0]["quantity"] == "8"

    deleted = client.delete(f"/api/transactions/{transaction_id}")
    assert deleted.status_code == 204
    assert client.get("/api/transactions").json() == []
    assert client.get("/api/portfolio/holdings").json() == []