"""CSV import template validation and canonical row normalization."""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CANONICAL_TYPES = {
    "buy", "sell", "dividend", "interest", "fee", "split", "deposit", "withdrawal", "transfer_in", "transfer_out",
}
CANONICAL_FIELDS = {
    "date", "time", "account", "type", "symbol", "isin", "quantity", "price",
    "gross_value", "currency", "fees", "taxes", "cash_amount", "notes", "security_name",
}
NUMERIC_FIELDS = {"quantity", "price", "gross_value", "fees", "taxes", "cash_amount"}
DEFAULT_FIELD_MAP = {
    "date": "date", "account": "account", "type": "type", "symbol": "symbol",
    "isin": "isin", "quantity": "quantity", "price": "price", "currency": "currency",
    "fees": "fees", "taxes": "taxes", "cash_amount": "cash_amount", "notes": "notes",
    "security_name": "security_name",
}
DEFAULT_TYPE_SIGN_HANDLING = {
    transaction_type: {"fees": "preserve", "taxes": "preserve"}
    for transaction_type in CANONICAL_TYPES
}
DEFAULT_TYPE_SIGN_HANDLING.update({
    "withdrawal": {"cash_amount": "absolute", "fees": "preserve", "taxes": "preserve"},
    "fee": {"cash_amount": "absolute", "fees": "preserve", "taxes": "preserve"},
    "dividend": {"cash_amount": "preserve", "fees": "preserve", "taxes": "preserve"},
    "interest": {"cash_amount": "preserve", "fees": "preserve", "taxes": "preserve"},
})


class TradeValueRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["principal", "buy_debit", "sell_proceeds"]
    includes_fees: bool = False
    includes_taxes: bool = False


class CsvTemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    delimiter: str = ","
    quotechar: str = '"'
    encoding: Literal["utf-8", "utf-8-sig"] = "utf-8-sig"
    header_row: bool = True
    skip_rows: int = Field(default=0, ge=0, le=100)
    field_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FIELD_MAP))
    date_format: str | None = Field(default=None, max_length=50)
    time_format: str | None = Field(default=None, max_length=50)
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: str | None = None
    sign_handling: dict[str, Literal["preserve", "absolute", "reject_negative"]] = Field(default_factory=dict)
    sign_handling_by_type: dict[str, dict[str, Literal["preserve", "absolute", "reject_negative"]]] = Field(default_factory=dict)
    type_map: dict[str, str] = Field(default_factory=dict)
    trade_value: TradeValueRule | None = None

    @field_validator("delimiter", "quotechar")
    @classmethod
    def single_character(cls, value: str) -> str:
        if len(value) != 1 or value in "\r\n":
            raise ValueError("CSV delimiter and quote character must each be one character")
        return value

    @field_validator("thousands_separator")
    @classmethod
    def valid_thousands_separator(cls, value: str | None) -> str | None:
        if value == "":
            return None
        if value is not None and (len(value) != 1 or value in "\r\n"):
            raise ValueError("Thousands separator must be one character")
        return value

    @model_validator(mode="after")
    def validate_mappings(self):
        unknown = set(self.field_map) - CANONICAL_FIELDS
        if unknown:
            raise ValueError(f"Unsupported canonical field mapping: {', '.join(sorted(unknown))}")
        if any(not isinstance(header, str) or not header.strip() for header in self.field_map.values()):
            raise ValueError("Mapped CSV headers cannot be empty")
        if any(len(header) > 256 for header in self.field_map.values()):
            raise ValueError("Mapped CSV headers must be 256 characters or fewer")
        missing_required = {"date", "type", "currency"} - set(self.field_map)
        if missing_required:
            raise ValueError(f"Template must map required fields: {', '.join(sorted(missing_required))}")
        if self.delimiter == self.quotechar:
            raise ValueError("CSV delimiter and quote character must differ")
        date_has_time = bool(self.date_format and ("%H" in self.date_format or "%I" in self.date_format))
        has_time_column = "time" in self.field_map
        if has_time_column and not self.time_format:
            raise ValueError("A time format is required when a separate time column is mapped")
        if has_time_column and date_has_time:
            raise ValueError("Use either a combined date/time column or separate date and time columns")
        if self.time_format and not has_time_column:
            raise ValueError("A time format requires a separate time column mapping")
        if self.decimal_separator == self.thousands_separator:
            raise ValueError("Decimal and thousands separators must differ")
        if self.trade_value:
            if "gross_value" not in self.field_map:
                raise ValueError("Map a gross_value column to use trade-value conversion")
            if "price" in self.field_map:
                raise ValueError("Map either price or gross_value, not both")
            if self.trade_value.mode == "principal" and (self.trade_value.includes_fees or self.trade_value.includes_taxes):
                raise ValueError("Trade principal is before fees and taxes; included-cost options apply to debit or proceeds values")
            if self.trade_value.includes_fees and "fees" not in self.field_map:
                raise ValueError("Map a fees column when gross trade value includes fees")
            if self.trade_value.includes_taxes and "taxes" not in self.field_map:
                raise ValueError("Map a taxes column when gross trade value includes taxes")
        elif "gross_value" in self.field_map:
            raise ValueError("A gross_value mapping requires an explicit trade-value interpretation")
        for source_type, canonical_type in self.type_map.items():
            if canonical_type not in CANONICAL_TYPES:
                raise ValueError(f"Unsupported canonical transaction type: {canonical_type}")
            if not source_type.strip():
                raise ValueError("Source transaction type labels cannot be empty")
        if len({label.lower() for label in self.type_map}) != len(self.type_map):
            raise ValueError("Source transaction type labels must be unique ignoring case")
        for field in self.sign_handling:
            if field not in NUMERIC_FIELDS:
                raise ValueError(f"Sign handling is not supported for {field}")
        for transaction_type, field_rules in self.sign_handling_by_type.items():
            if transaction_type not in CANONICAL_TYPES:
                raise ValueError(f"Unsupported transaction type in sign rules: {transaction_type}")
            for field in field_rules:
                if field not in NUMERIC_FIELDS:
                    raise ValueError(f"Sign handling is not supported for {field}")
        return self


def canonical_definition(field_map: dict[str, str] | None = None) -> CsvTemplateDefinition:
    """Build a definition compatible with the original canonical CSV upload."""
    mapping = dict(DEFAULT_FIELD_MAP)
    if field_map:
        mapping.update({key: value for key, value in field_map.items() if key in CANONICAL_FIELDS})
    return CsvTemplateDefinition(field_map=mapping)


def definition_json(definition: CsvTemplateDefinition) -> str:
    return json.dumps(definition.model_dump(mode="json"), sort_keys=True)


def _parse_decimal(value: str, definition: CsvTemplateDefinition, field: str, transaction_type: str) -> Decimal:
    normalized = value.strip().replace("\u00a0", " ")
    if definition.thousands_separator:
        normalized = normalized.replace(definition.thousands_separator, "")
    if definition.decimal_separator != ".":
        normalized = normalized.replace(definition.decimal_separator, ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as error:
        raise ValueError(f"{field} must be a valid number") from error
    if not amount.is_finite():
        raise ValueError(f"{field} must be a finite number")
    handling = definition.sign_handling_by_type.get(transaction_type, {}).get(field)
    if handling is None:
        handling = DEFAULT_TYPE_SIGN_HANDLING.get(transaction_type, {}).get(field)
    if handling is None:
        handling = definition.sign_handling.get(field, "reject_negative")
    if amount < 0:
        if handling == "absolute":
            amount = abs(amount)
        elif handling == "reject_negative":
            raise ValueError(f"{field} cannot be negative under the selected sign rule")
    return amount


def _parse_date(value: str, time_value: str, definition: CsvTemplateDefinition, normalize_date) -> str:
    stripped = value.strip()
    if "time" in definition.field_map:
        if not time_value.strip():
            raise ValueError("Time is required for this import format")
        parsed_date = datetime.strptime(stripped, definition.date_format).date() if definition.date_format else date.fromisoformat(stripped)
        assert definition.time_format is not None
        parsed_time = datetime.strptime(time_value.strip(), definition.time_format).timetz()
        return normalize_date(datetime.combine(parsed_date, parsed_time).isoformat())
    if not definition.date_format:
        return normalize_date(stripped)
    parsed = datetime.strptime(stripped, definition.date_format)
    if "%H" not in definition.date_format and "%I" not in definition.date_format:
        return parsed.date().isoformat()
    return normalize_date(parsed.isoformat())


def parse_csv(raw: bytes, definition: CsvTemplateDefinition) -> tuple[list[str], list[dict[str, str]]]:
    try:
        content = raw.decode(definition.encoding).lstrip("\ufeff")
    except UnicodeDecodeError as error:
        raise ValueError("CSV encoding is not supported by this template") from error
    reader = csv.reader(io.StringIO(content, newline=""), delimiter=definition.delimiter, quotechar=definition.quotechar)
    for _ in range(definition.skip_rows):
        next(reader, None)
    try:
        first = next(reader)
    except StopIteration:
        return [], []
    if definition.header_row:
        headers = [header.strip() for header in first]
        if len(headers) != len(set(headers)):
            raise ValueError("CSV headers must be unique")
        data_start_line = reader.line_num + 1
    else:
        headers = [f"Column {index + 1}" for index in range(len(first))]
        reader = iter([first, *reader])
        data_start_line = definition.skip_rows + 1
    rows: list[dict[str, str]] = []
    for values in reader:
        line = reader.line_num if hasattr(reader, "line_num") else data_start_line + len(rows)
        if len(values) > len(headers):
            rows.append({"__line__": str(line), "__row_error__": "Row has a different number of columns than the header"})
            continue
        rows.append({**dict(zip(headers, values + [""] * (len(headers) - len(values)))), "__line__": str(line)})
    return headers, rows


def normalize_source_row(source: dict[str, str], line: int, definition: CsvTemplateDefinition,
                         account_ids: dict[str, int], account_override: int | None, normalize_date) -> dict:
    mapped = {field: (source.get(header) or "").strip() for field, header in definition.field_map.items()}
    source_type = mapped.get("type", "").lower()
    type_map = {label.lower(): value for label, value in definition.type_map.items()}
    canonical_type = type_map.get(source_type, source_type if not definition.type_map else None)
    if canonical_type not in CANONICAL_TYPES:
        raise ValueError(f"Unknown transaction type: {mapped.get('type', '') or '(blank)'}")

    account_name = mapped.get("account", "")
    account_id = account_override if account_override is not None else account_ids.get(account_name)
    if account_override is not None:
        account_name = next((name for name, account_id_value in account_ids.items() if account_id_value == account_override), account_name)
    if "date" not in mapped or not mapped["date"]:
        raise ValueError("Date is required")
    transaction_date = _parse_date(mapped["date"], mapped.get("time", ""), definition, normalize_date)

    def decimal_field(name: str, default: str = "0") -> Decimal:
        value = mapped.get(name, "")
        return _parse_decimal(value if value else default, definition, name, canonical_type)

    quantity = decimal_field("quantity")
    fees = decimal_field("fees")
    taxes = decimal_field("taxes")
    if definition.trade_value:
        gross_value = decimal_field("gross_value", "")
        rule = definition.trade_value
        included_costs = (fees if rule.includes_fees else Decimal(0)) + (taxes if rule.includes_taxes else Decimal(0))
        if rule.includes_fees and not mapped.get("fees"):
            raise ValueError("The trade-value rule includes fees but this row has no fee value")
        if rule.includes_taxes and not mapped.get("taxes"):
            raise ValueError("The trade-value rule includes taxes but this row has no tax value")
        if rule.mode == "buy_debit":
            if canonical_type != "buy":
                raise ValueError("Buy-debit trade-value interpretation only applies to buy rows")
            principal = gross_value - included_costs
        elif rule.mode == "sell_proceeds":
            if canonical_type != "sell":
                raise ValueError("Sell-proceeds trade-value interpretation only applies to sell rows")
            principal = gross_value + included_costs
        else:
            principal = gross_value
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero to derive unit price")
        unit_price = principal / quantity
        if unit_price <= 0:
            raise ValueError("Derived unit price must be greater than zero")
    else:
        unit_price = decimal_field("price")

    currency = mapped.get("currency", "").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Currency must be a three-letter code")
    row = {
        "account_id": account_id,
        "account_name": account_name,
        "transaction_date": transaction_date,
        "type": canonical_type,
        "symbol": mapped.get("symbol", "").upper() or None,
        "isin": mapped.get("isin", "").upper() or None,
        "security_name": mapped.get("security_name", ""),
        "quantity": str(quantity),
        "price": str(unit_price),
        "currency": currency,
        "fees": str(fees),
        "taxes": str(taxes),
        "cash_amount": str(decimal_field("cash_amount")) if mapped.get("cash_amount") else None,
        "notes": mapped.get("notes", ""),
        "line": line,
    }
    return row
