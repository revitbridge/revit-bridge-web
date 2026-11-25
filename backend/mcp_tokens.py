"""Device-bound bearer credentials in the host's private settings store."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from backend.model_settings import StoreUnavailable, edit_settings, read_settings

SECTION = "remote_mcp_tokens"
PREFIX = "rbmcp_"
FORMAT = re.compile(r"rbmcp_([0-9a-f]{32})\.[A-Za-z0-9_-]{43}")


class TokenInfo(BaseModel):
    id: str
    label: str
    device_id: str
    created_at: str
    last_used_at: str | None = None


class StoredToken(TokenInfo):
    salt: str = Field(pattern=r"^[0-9a-f]{32}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


TOKEN_RECORDS = TypeAdapter(dict[str, StoredToken])


def records(settings):
    try:
        return TOKEN_RECORDS.validate_python(settings.get(SECTION, {}))
    except ValidationError:
        raise StoreUnavailable from None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value, salt):
    # A fresh 256-bit random bearer secret, independent of the public management ID.
    return hashlib.sha256((salt + value).encode()).hexdigest()


def create(device_id, label):
    token_id = secrets.token_hex(16)
    bearer = PREFIX + token_id + "." + secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    entry = StoredToken(id=token_id, label=label, device_id=device_id, created_at=now(),
                        salt=salt, digest=digest(bearer, salt))
    with edit_settings() as settings:
        records(settings)
        settings.setdefault(SECTION, {})[token_id] = entry.model_dump()
    return bearer, entry


def list_tokens(device_id):
    return [entry for entry in records(read_settings()).values() if entry.device_id == device_id]


class InvalidToken(Exception):
    pass


def authenticate(value, cloud):
    match = FORMAT.fullmatch(value)
    if match is None:
        raise InvalidToken
    entry = records(read_settings()).get(match[1])
    expected = entry.digest if entry else "0" * 64
    actual = digest(value, entry.salt if entry else "0" * 32)
    if not hmac.compare_digest(actual, expected) or not entry or (entry.device_id == "local") == cloud:
        raise InvalidToken
    return entry.device_id, entry.id


def mark_used(device_id, token_id):
    """Recheck the credential under the writer lock when its message is dispatched."""
    with edit_settings() as settings:
        entry = records(settings).get(token_id)
        if not entry or entry.device_id != device_id:
            raise InvalidToken
        settings[SECTION][token_id]["last_used_at"] = now()


def revoke(device_id, token_id):
    with edit_settings() as settings:
        entry = records(settings).get(token_id)
        if entry is None or entry.device_id != device_id:
            raise InvalidToken
        del settings[SECTION][token_id]
        if not settings[SECTION]:
            del settings[SECTION]
