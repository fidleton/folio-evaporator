from decimal import Decimal

from app.main import ledger


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_):
        return self

    def fetchall(self):
        return self.rows


def test_ledger_calculates_positions_cash_and_splits():
    rows = [
        {"account_id": 1, "account_name": "Broker A", "symbol": None, "currency": "USD",
         "type": "deposit", "quantity": "0", "price": "0", "fees": "0", "taxes": "0", "cash_amount": "2000"},
        {"account_id": 1, "account_name": "Broker A", "symbol": "ACME", "currency": "USD",
         "type": "buy", "quantity": "10", "price": "12.50", "fees": "1.00", "taxes": "0", "cash_amount": None},
        {"account_id": 1, "account_name": "Broker A", "symbol": "ACME", "currency": "USD",
         "type": "split", "quantity": "2", "price": "0", "fees": "0", "taxes": "0", "cash_amount": None},
        {"account_id": 1, "account_name": "Broker A", "symbol": "ACME", "currency": "USD",
         "type": "sell", "quantity": "3", "price": "15", "fees": "0.50", "taxes": "0", "cash_amount": None},
        {"account_id": 1, "account_name": "Broker A", "symbol": None, "currency": "USD",
         "type": "dividend", "quantity": "0", "price": "0", "fees": "0", "taxes": "0", "cash_amount": "4.25"},
    ]
    holdings, cash = ledger(FakeConnection(rows))
    assert holdings[("ACME", "USD")] == Decimal("17")
    assert cash[(1, "USD")] == Decimal("1922.75")