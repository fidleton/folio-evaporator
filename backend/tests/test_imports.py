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