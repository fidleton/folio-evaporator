from decimal import Decimal
import json

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


def test_import_templates_can_be_created_updated_listed_and_deleted(client):
    definition = {
        "schema_version": 1,
        "field_map": {"date": "Trade date", "account": "Account", "type": "Action", "symbol": "Ticker",
                       "quantity": "Shares", "price": "Unit price", "currency": "Currency"},
        "type_map": {"Purchase": "buy"},
    }
    created = client.post("/api/imports/templates", json={"name": "Broker CSV", "definition": definition})
    assert created.status_code == 201
    assert created.json()["definition"]["type_map"] == {"Purchase": "buy"}
    template_id = created.json()["id"]
    assert client.get("/api/imports/templates").json()[0]["id"] == template_id
    assert client.post("/api/imports/templates", json={"name": "Broker CSV", "definition": definition}).status_code == 409

    updated = client.put(
        f"/api/imports/templates/{template_id}",
        json={"name": "Broker Export", "definition": {**definition, "delimiter": ";"}},
    )
    assert updated.status_code == 200
    assert updated.json()["definition"]["delimiter"] == ";"
    assert client.delete(f"/api/imports/templates/{template_id}").status_code == 204
    assert client.get(f"/api/imports/templates/{template_id}").status_code == 404


def test_custom_template_normalizes_locale_and_gross_buy_debit_and_snapshots_definition(client):
    account = client.post("/api/accounts", json={"name": "Broker account"}).json()
    template = client.post("/api/imports/templates", json={
        "name": "Semicolon broker",
        "definition": {
            "schema_version": 1, "delimiter": ";", "date_format": "%d/%m/%Y",
            "decimal_separator": ",", "thousands_separator": ".",
            "field_map": {"date": "Date", "account": "Account", "type": "Activity", "symbol": "Ticker",
                          "quantity": "Shares", "gross_value": "Total", "currency": "Currency",
                          "fees": "Commission", "taxes": "Tax"},
            "type_map": {"Purchase": "buy"},
            "trade_value": {"mode": "buy_debit", "includes_fees": True, "includes_taxes": True},
        },
    }).json()
    csv_content = (
        "Date;Account;Activity;Ticker;Shares;Total;Currency;Commission;Tax\n"
        "02/01/2024;Broker account;Purchase;ACME;10,0;1.015,00;usd;10,00;5,00\n"
        "03/01/2024;Broker account;Mystery;ACME;2,0;20,00;USD;0,00;0,00\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        data={"template_id": template["id"]},
        files={"file": ("broker.csv", csv_content, "text/csv")},
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["valid_rows"] == 1
    assert payload["invalid_rows"] == 1
    assert payload["rows"][0]["price"] == "100.0"
    assert payload["rows"][0]["fees"] == "10.00"
    assert "Unknown transaction type" in payload["rows"][1]["errors"][0]

    client.put(f"/api/imports/templates/{template['id']}", json={
        "name": "Changed broker", "definition": {"field_map": {"date": "not the source"}},
    })
    client.delete(f"/api/imports/templates/{template['id']}")
    committed = client.post(f"/api/imports/{payload['id']}/commit")
    assert committed.json() == {"id": payload["id"], "imported_count": 1, "skipped_count": 1}
    transaction = client.get("/api/transactions").json()[0]
    assert transaction["price"] == "100.0"
    assert transaction["account_id"] == account["id"]


def test_gross_value_template_requires_interpretation_and_missing_costs_are_row_errors(client):
    definition = {
        "field_map": {"date": "date", "account": "account", "type": "type", "symbol": "symbol",
                      "quantity": "quantity", "gross_value": "total", "currency": "currency", "fees": "fees"},
        "trade_value": {"mode": "buy_debit", "includes_fees": True, "includes_taxes": False},
    }
    assert client.post("/api/imports/templates", json={"name": "Bad format", "definition": {
        **definition, "trade_value": None,
    }}).status_code == 422
    client.post("/api/accounts", json={"name": "Broker account"})
    content = "date,account,type,symbol,quantity,total,currency,fees\n2024-01-01,Broker account,buy,ACME,1,10,USD,\n"
    preview = client.post("/api/imports/csv/preview", data={"template_definition": __import__("json").dumps(definition)},
                          files={"file": ("x.csv", content, "text/csv")})
    assert preview.status_code == 200
    assert preview.json()["rows"][0]["valid"] is False
    assert "no fee value" in preview.json()["rows"][0]["errors"][0]


def test_sell_net_proceeds_conversion_adds_back_included_costs(client):
    client.post("/api/accounts", json={"name": "Broker account"})
    definition = {
        "field_map": {"date": "date", "account": "account", "type": "type", "symbol": "symbol",
                      "quantity": "quantity", "gross_value": "net", "currency": "currency",
                      "fees": "fees", "taxes": "taxes"},
        "trade_value": {"mode": "sell_proceeds", "includes_fees": True, "includes_taxes": True},
    }
    content = "date,account,type,symbol,quantity,net,currency,fees,taxes\n2024-01-01,Broker account,sell,ACME,10,138,USD,1,1\n"
    preview = client.post("/api/imports/csv/preview", data={"template_definition": json.dumps(definition)},
                          files={"file": ("x.csv", content, "text/csv")})
    assert preview.status_code == 200
    assert preview.json()["rows"][0]["price"] == "14"


def test_import_sign_rules_can_differ_for_withdrawals_distributions_fees_and_taxes(client):
    account_id = client.post("/api/accounts", json={"name": "Broker account"}).json()["id"]
    definition = {
        "field_map": {"date": "date", "account": "account", "type": "type", "currency": "currency",
                      "cash_amount": "amount", "fees": "fees", "taxes": "taxes"},
        "type_map": {"Withdrawal": "withdrawal", "Distribution": "dividend"},
        "sign_handling": {"cash_amount": "absolute", "fees": "absolute", "taxes": "absolute"},
    }
    content = (
        "date,account,type,currency,amount,fees,taxes\n"
        "2026-08-05,Broker account,Withdrawal,EUR,-10.00,-0.50,-0.25\n"
        "2026-08-06,Broker account,Distribution,EUR,-4.99,-0.20,-0.07\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        data={"template_definition": json.dumps(definition)},
        files={"file": ("scalable.csv", content, "text/csv")},
    )
    assert preview.status_code == 200
    rows = preview.json()["rows"]
    assert preview.json()["valid_rows"] == 2
    assert rows[0]["cash_amount"] == "10.00"
    assert rows[0]["fees"] == "-0.50"
    assert rows[0]["taxes"] == "-0.25"
    assert rows[1]["cash_amount"] == "-4.99"
    assert rows[1]["fees"] == "-0.20"
    assert rows[1]["taxes"] == "-0.07"

    client.post(f"/api/imports/{preview.json()['id']}/commit")
    with database.connect() as connection:
        _, cash = main.ledger(connection)
    assert cash[(account_id, "EUR")] == Decimal("-14.47")


def test_interest_import_tracks_signed_cash_income_separately(client):
    account_id = client.post("/api/accounts", json={"name": "Broker account"}).json()["id"]
    definition = {
        "field_map": {"date": "date", "account": "account", "type": "type", "currency": "currency",
                      "cash_amount": "amount", "fees": "fees", "taxes": "taxes"},
        "type_map": {"Interest payment": "interest"},
    }
    content = (
        "date,account,type,currency,amount,fees,taxes\n"
        "2026-08-05,Broker account,Interest payment,USD,5.00,0.25,0.50\n"
        "2026-08-06,Broker account,Interest payment,USD,-1.00,0,0\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        data={"template_definition": json.dumps(definition)},
        files={"file": ("interest.csv", content, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["valid_rows"] == 2
    assert [row["type"] for row in preview.json()["rows"]] == ["interest", "interest"]
    assert [row["cash_amount"] for row in preview.json()["rows"]] == ["5.00", "-1.00"]

    committed = client.post(f"/api/imports/{preview.json()['id']}/commit")
    assert committed.json()["imported_count"] == 2
    with database.connect() as connection:
        rows = connection.execute("SELECT type FROM transactions ORDER BY id").fetchall()
        _, cash = main.ledger(connection)
    assert [row["type"] for row in rows] == ["interest", "interest"]
    assert cash[(account_id, "USD")] == Decimal("3.25")
    summary = client.get("/api/portfolio/summary").json()
    assert summary["interest_income"] == "3.75"
    assert summary["total_return"] == "3.75"


def test_security_transfers_move_shares_and_carry_cost_basis_without_cash(client):
    client.post("/api/accounts", json={"name": "Broker account"})
    content = (
        "date,account,symbol,type,quantity,price,currency\n"
        "2026-08-01,Broker account,ACME,transfer_in,5,10,USD\n"
        "2026-08-02,Broker account,ACME,transfer_out,2,10,USD\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        files={"file": ("transfers.csv", content, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["valid_rows"] == 2
    committed = client.post(f"/api/imports/{preview.json()['id']}/commit")
    assert committed.json()["imported_count"] == 2

    with database.connect() as connection:
        holdings, cash = main.ledger(connection)
        average_costs, _, _, _ = main.security_performance(connection)
    assert holdings[("ACME", "USD")] == Decimal("3")
    assert cash == {}
    assert average_costs[("ACME", "USD")] == Decimal("10")


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


def test_csv_import_combines_separate_date_and_time_columns(client):
    client.post("/api/accounts", json={"name": "Long term"})
    definition = {
        "date_format": "%d/%m/%Y", "time_format": "%I:%M:%S %p",
        "field_map": {"date": "Trade date", "time": "Trade time", "account": "Account", "type": "Action",
                      "symbol": "Ticker", "quantity": "Shares", "price": "Unit price", "currency": "Currency"},
        "type_map": {"Purchase": "buy"},
    }
    csv_content = "Trade date,Trade time,Account,Action,Ticker,Shares,Unit price,Currency\n02/01/2024,02:35:00 PM,Long term,Purchase,ACME,10,12.5,USD\n"
    preview = client.post(
        "/api/imports/csv/preview",
        data={"template_definition": json.dumps(definition)},
        files={"file": ("activity.csv", csv_content, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["rows"][0]["transaction_date"] == "2024-01-02T14:35:00+00:00"


def test_csv_import_parses_combined_custom_date_time_format(client):
    client.post("/api/accounts", json={"name": "Long term"})
    definition = {
        "date_format": "%d/%m/%Y %H:%M",
        "field_map": {"date": "Executed", "account": "Account", "type": "type", "symbol": "symbol",
                      "quantity": "quantity", "price": "price", "currency": "currency"},
    }
    csv_content = "Executed,Account,type,symbol,quantity,price,currency\n02/01/2024 14:35,Long term,buy,ACME,10,12.5,USD\n"
    preview = client.post(
        "/api/imports/csv/preview",
        data={"template_definition": json.dumps(definition)},
        files={"file": ("activity.csv", csv_content, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["rows"][0]["transaction_date"] == "2024-01-02T14:35:00+00:00"


def test_reporting_currency_persists_after_database_initialization(client):
    response = client.put("/api/settings", params={"reporting_currency": "eur"})

    assert response.status_code == 200
    assert response.json() == {"reporting_currency": "EUR"}

    database.initialize()

    summary = client.get("/api/portfolio/summary")
    assert summary.json()["reporting_currency"] == "EUR"


def test_time_format_is_not_a_server_setting(client):
    summary = client.get("/api/portfolio/summary").json()
    assert "time_format" not in summary
    assert client.put("/api/settings", params={"time_format": "12-hour"}).status_code == 422


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


def test_empty_isin_is_normalized_to_none_and_non_12_character_isin_is_rejected(client):
    account_id = client.post("/api/accounts", json={"name": "Cash account"}).json()["id"]
    transaction = {
        "account_id": account_id, "type": "fee", "transaction_date": "2024-01-02",
        "isin": "", "currency": "EUR", "cash_amount": "4.99",
    }
    created = client.post("/api/transactions", json=transaction)
    assert created.status_code == 201
    assert created.json()["isin"] is None
    assert client.post("/api/transactions", json={**transaction, "isin": "TKYACBETL6DJZBFT"}).status_code == 422


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


def test_csv_preview_shows_known_company_name_for_symbol(client):
    account = client.post("/api/accounts", json={"name": "Target"}).json()
    existing = client.post("/api/transactions", json={
        "account_id": account["id"],
        "type": "buy",
        "transaction_date": "2026-01-01",
        "symbol": "ADYEN.AS",
        "security_name": "Adyen N.V.",
        "isin": "NL0012969182",
        "quantity": "1",
        "price": "100",
        "currency": "EUR",
    })
    assert existing.status_code == 201

    content = (
        "date,account,symbol,type,quantity,price,currency\n"
        "2026-02-01,Target,ADYEN.AS,buy,1,110,EUR\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        files={"file": ("positions.csv", content, "text/csv")},
    )

    assert preview.status_code == 200
    assert preview.json()["rows"][0]["security_name"] == "Adyen N.V."


def test_csv_preview_fills_isin_from_unambiguous_open_position_in_target_account(client):
    target_account = client.post("/api/accounts", json={"name": "Target"}).json()
    other_account = client.post("/api/accounts", json={"name": "Other"}).json()

    def add_trade(account_id, symbol, transaction_type, transaction_date, isin):
        response = client.post("/api/transactions", json={
            "account_id": account_id,
            "type": transaction_type,
            "transaction_date": transaction_date,
            "symbol": symbol,
            "isin": isin,
            "quantity": "1",
            "price": "10",
            "currency": "USD",
        })
        assert response.status_code == 201

    add_trade(target_account["id"], "AEM", "buy", "2026-01-01", "US0378331005")
    add_trade(other_account["id"], "AEM", "buy", "2026-01-02", "US5949181045")
    add_trade(target_account["id"], "CLOSED", "buy", "2026-01-03", "US0231351067")
    add_trade(target_account["id"], "CLOSED", "sell", "2026-01-04", "US0231351067")
    add_trade(target_account["id"], "MULTI", "buy", "2026-01-05", "US0378331005")
    add_trade(target_account["id"], "MULTI", "buy", "2026-01-06", "US5949181045")

    content = (
        "date,symbol,type,quantity,price,currency\n"
        "2026-02-01,AEM,buy,1,11,USD\n"
        "2026-02-02,CLOSED,buy,1,11,USD\n"
        "2026-02-03,MULTI,buy,1,11,USD\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        data={"account_id": str(target_account["id"])},
        files={"file": ("positions.csv", content, "text/csv")},
    )

    assert preview.status_code == 200
    rows = {row["symbol"]: row for row in preview.json()["rows"]}
    assert rows["AEM"]["isin"] == "US0378331005"
    assert rows["CLOSED"]["isin"] is None
    assert rows["MULTI"]["isin"] is None


def test_csv_preview_reuses_open_position_symbol_for_same_isin(client):
    account = client.post("/api/accounts", json={"name": "Target"}).json()
    existing = client.post("/api/transactions", json={
        "account_id": account["id"],
        "type": "buy",
        "transaction_date": "2026-01-01",
        "symbol": "ADYEN.AS",
        "isin": "NL0012969182",
        "quantity": "1",
        "price": "100",
        "currency": "EUR",
    })
    assert existing.status_code == 201

    content = (
        "date,isin,type,quantity,price,currency\n"
        "2026-02-01,NL0012969182,buy,1,110,EUR\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        data={"account_id": str(account["id"])},
        files={"file": ("positions.csv", content, "text/csv")},
    )

    assert preview.status_code == 200
    row = preview.json()["rows"][0]
    assert row["symbol"] == "ADYEN.AS"
    assert row["isin"] == "NL0012969182"
    assert row["valid"] is True


def test_import_preview_accepts_user_selected_symbol_for_isin_and_description(client):
    account = client.post("/api/accounts", json={"name": "Broker account"}).json()
    content = (
        "date,account,description,type,isin,quantity,price,currency,cash_amount\n"
        "2026-08-03,Broker account,Amazon.com,sell,US0231351067,1,240.10,EUR,\n"
        "2026-08-04,Broker account,Amazon.com,dividend,US0231351067,0,0,EUR,2.00\n"
    )
    preview = client.post(
        "/api/imports/csv/preview",
        data={"field_map": json.dumps({"security_name": "description"})},
        files={"file": ("scalable.csv", content, "text/csv")},
    )
    assert preview.status_code == 200
    assert preview.json()["valid_rows"] == 0
    assert "Symbol is required for a trade" in preview.json()["rows"][0]["errors"]
    assert "Symbol is required for a security distribution" in preview.json()["rows"][1]["errors"]

    selected = client.put(
        f"/api/imports/{preview.json()['id']}/rows/2/symbol",
        json={"symbol": "AMZN", "security_name": "Amazon.com, Inc."},
    )
    assert selected.status_code == 200
    assert selected.json()["valid_rows"] == 2
    assert selected.json()["rows"][0]["symbol"] == "AMZN"
    assert selected.json()["rows"][1]["symbol"] == "AMZN"
    committed = client.post(f"/api/imports/{preview.json()['id']}/commit")
    assert committed.json()["imported_count"] == 2
    assert all(transaction["isin"] == "US0231351067" for transaction in client.get("/api/transactions").json())
    assert client.get("/api/securities").json()[0]["symbol"] == "AMZN"
    assert client.get("/api/securities").json()[0]["name"] == "Amazon.com, Inc."


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
