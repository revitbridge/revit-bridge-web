"""Device-bound bearer credentials in the host's private settings store."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from backend.private_settings import PrivateSettingsFile, StoreFull, StoreUnavailable

TOKEN_FILE = PrivateSettingsFile("mcp-tokens.json", ".mcp-tokens.lock", 128 * 1024)
MAX_PER_DEVICE = 5
MAX_LABEL_LENGTH = 64
PREFIX = "rbmcp_"
TOKEN_ID = re.compile(r"rbmcp_([0-9a-f]{32})\.")
FORMAT = re.compile(TOKEN_ID.pattern + r"[A-Za-z0-9_-]{43}")


class TokenInfo(BaseModel):
    id: str
    label: str = Field(max_length=MAX_LABEL_LENGTH)
    device_id: str
    created_at: str
    last_used_at: str | None = None


class StoredToken(TokenInfo):
    salt: str = Field(pattern=r"^[0-9a-f]{32}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


TOKEN_RECORDS = TypeAdapter(dict[str, StoredToken])


def records(stored):
    try:
        return TOKEN_RECORDS.validate_python(stored)
    except ValidationError:
        raise StoreUnavailable from None


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value, salt):
    # A fresh 256-bit random bearer secret, independent of the public management ID.
    return hashlib.sha256((salt + value).encode()).hexdigest()


class TokenLimit(Exception):
    pass


class InvalidToken(Exception):
    pass


def create(device_id, label, paired_ids=None):
    token_id = secrets.token_hex(16)
    bearer = PREFIX + token_id + "." + secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    entry = StoredToken(id=token_id, label=label, device_id=device_id, created_at=now(),
                        salt=salt, digest=digest(bearer, salt))
    with TOKEN_FILE.edit() as stored:
        entries = records(stored)
        if paired_ids is not None:
            # Recheck under the token writer lock in case unpair raced authorization.
            active = paired_ids()
            if device_id not in active:
                raise InvalidToken
            for key, existing in entries.items():
                if existing.device_id != "local" and existing.device_id not in active:
                    del stored[key]
        if sum(t.device_id == device_id for t in entries.values()) >= MAX_PER_DEVICE:
            raise TokenLimit
        stored[token_id] = entry.model_dump()
    return bearer, entry


def list_tokens(device_id):
    return [entry for entry in records(TOKEN_FILE.read()).values() if entry.device_id == device_id]


def authenticate(value, cloud):
    match = FORMAT.fullmatch(value)
    if match is None:
        raise InvalidToken
    entry = records(TOKEN_FILE.read()).get(match[1])
    expected = entry.digest if entry else "0" * 64
    actual = digest(value, entry.salt if entry else "0" * 32)
    if not hmac.compare_digest(actual, expected) or not entry or (entry.device_id == "local") == cloud:
        raise InvalidToken
    return entry.device_id, entry.id


def mark_used(device_id, token_id):
    """Recheck the credential under the writer lock when its message is dispatched."""
    try:
        with TOKEN_FILE.edit() as stored:
            entry = records(stored).get(token_id)
            if not entry or entry.device_id != device_id:
                raise InvalidToken
            moment = datetime.now(timezone.utc)
            if entry.last_used_at and moment - datetime.fromisoformat(entry.last_used_at) < timedelta(minutes=1):
                return
            stored[token_id]["last_used_at"] = moment.isoformat()
    except StoreFull:
        # The first timestamp can be larger than null. Metadata is best effort
        # once the verified credential's file reaches its byte limit.
        pass
    except (ValueError, TypeError):
        raise StoreUnavailable from None


def revoke(device_id, token_id):
    with TOKEN_FILE.edit() as stored:
        entry = records(stored).get(token_id)
        if entry is None or entry.device_id != device_id:
            raise InvalidToken
        del stored[token_id]


def revoke_device(device_id):
    with TOKEN_FILE.edit() as stored:
        for key, entry in records(stored).items():
            if entry.device_id == device_id:
                del stored[key]
