import json
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken


logger = logging.getLogger("uvicorn.error")
SUPERVISOR_URL = "http://supervisor"
OPTIONS_ENDPOINT = f"{SUPERVISOR_URL}/addons/self/options"
OPTIONS_CONFIG_ENDPOINT = f"{OPTIONS_ENDPOINT}/config"
ENCRYPTED_VALUE_PREFIX = "fernet:v1:"


def _supervisor_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    started_at = time.perf_counter()
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            status_code = getattr(response, "status", 200)
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.error(
            "External call failed provider=Home Assistant Supervisor operation=%s duration_ms=%.2f error_type=%s status_code=%s",
            "read_options" if method == "GET" else "update_options",
            (time.perf_counter() - started_at) * 1000,
            type(error).__name__,
            getattr(error, "code", None),
        )
        raise RuntimeError("Could not read or persist Home Assistant add-on options") from error
    logger.info(
        "External call completed provider=Home Assistant Supervisor operation=%s status_code=%s duration_ms=%.2f",
        "read_options" if method == "GET" else "update_options",
        status_code,
        (time.perf_counter() - started_at) * 1000,
    )
    if not isinstance(result, dict) or result.get("result") == "error":
        raise RuntimeError("Home Assistant Supervisor returned invalid add-on options")
    return result


def _load_supervisor_options(token: str) -> dict:
    response = _supervisor_request("GET", OPTIONS_CONFIG_ENDPOINT, token)
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Home Assistant Supervisor returned invalid add-on options")
    options = data.get("options")
    return options if isinstance(options, dict) else data


def _load_or_create_local_key(data_dir: Path, has_encrypted_secrets: bool) -> bytes:
    key_path = data_dir / "folio-encryption.key"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        key = key_path.read_bytes().strip()
    except FileNotFoundError:
        if has_encrypted_secrets:
            raise RuntimeError("The encryption key file is missing; restore it to decrypt saved OpenFIGI credentials")
        key = Fernet.generate_key()
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(key)
    os.chmod(key_path, 0o600)
    return key


def load_or_create_fernet(data_dir: Path, has_encrypted_secrets: bool = False) -> Fernet:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        logger.warning(
            "Home Assistant Supervisor token unavailable; using a separate local key file for standalone development"
        )
        key = _load_or_create_local_key(data_dir, has_encrypted_secrets)
        return Fernet(key)

    options = _load_supervisor_options(token)
    configured_key = options.get("encryption_key")
    if not isinstance(configured_key, str) or not configured_key.strip():
        if has_encrypted_secrets:
            raise RuntimeError(
                "The encryption key is missing from Home Assistant add-on options; restore the original key to decrypt saved OpenFIGI credentials"
            )
        configured_key = Fernet.generate_key().decode("ascii")
        options["encryption_key"] = configured_key
        _supervisor_request("POST", OPTIONS_ENDPOINT, token, {"options": options})
        logger.info("Generated and saved encryption key in Home Assistant add-on options")
    try:
        return Fernet(configured_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("The configured encryption key is invalid; replace it in Home Assistant add-on options") from error


def encrypt_secret(fernet: Fernet, value: str) -> str:
    encrypted_value = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_VALUE_PREFIX}{encrypted_value}"


def decrypt_secret(fernet: Fernet, value: str) -> str:
    if not value.startswith(ENCRYPTED_VALUE_PREFIX):
        return value
    try:
        token = value.removeprefix(ENCRYPTED_VALUE_PREFIX).encode("ascii")
        return fernet.decrypt(token).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as error:
        raise RuntimeError(
            "The saved OpenFIGI key cannot be decrypted; restore the original encryption key in Home Assistant add-on options"
        ) from error


def migrate_openfigi_key(connection, fernet: Fernet) -> None:
    stored_key = connection.execute(
        "SELECT value FROM settings WHERE key='openfigi_api_key'"
    ).fetchone()
    if not stored_key:
        return
    value = stored_key["value"]
    if value.startswith(ENCRYPTED_VALUE_PREFIX):
        decrypt_secret(fernet, value)
        return
    connection.execute(
        "UPDATE settings SET value=? WHERE key='openfigi_api_key'",
        (encrypt_secret(fernet, value),),
    )
    connection.commit()
    connection.execute("VACUUM")
