"""The write-only model credentials in the operator's config directory."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator

from backend.llm import LLMError, endpoint, has_credential, is_loopback_host, public_endpoint
from backend.private_settings import PrivateSettingsFile, StoreCorrupt, StoreUnavailable, settings_home

Scope = Literal["shared", "planner", "analyst", "supervisor"]
SCOPES = ("shared", "planner", "analyst", "supervisor")
_GROUP_FILE = PrivateSettingsFile("secrets.json", ".models.lock", 128 * 1024)


class ModelSettingsFile(PrivateSettingsFile):
    def read(self) -> dict:
        try:
            return _GROUP_FILE.read()
        except StoreCorrupt:
            # Twenty full profiles can exceed the original four-group byte limit.
            groups = super().read()
            if "library" in groups:
                return groups
            # A writer may have replaced the oversized file with a legacy one.
            return _GROUP_FILE.read()


# UTF-8 JSON needs at most 6 bytes per URL character (escapes), 4 per printable
# model character and 2 per printable ASCII key character. Allow 256 bytes per
# row for its name, field names, options and punctuation, and 512 for selections
# and outer JSON: (20 + 4) * (2048*6 + 200*4 + 4096*2 + 256) + 512 < 512 KiB.
MODEL_FILE = ModelSettingsFile(_GROUP_FILE.filename, _GROUP_FILE.lockfile, 512 * 1024)


SelectionRole = Literal["analyst", "planner", "supervisor", "optimizer"]
LIBRARY_LIMIT = 20
SAVED_NAME = r"[A-Za-z][A-Za-z0-9_-]{0,39}"
# File entry names also allow dots and an initial digit or punctuation.
ENTRY_NAME = rf"(?:{SAVED_NAME}|[A-Za-z0-9_.-]{{1,32}})"
SavedName = Annotated[str, Field(pattern=rf"^{SAVED_NAME}$")]
EntryName = Annotated[str, Field(pattern=rf"^{ENTRY_NAME}$")]


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, max_length=4096)

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value):
        from backend.config import get_settings
        try:
            scheme, host, _ = endpoint(value)
            url = httpx.URL(value)
        except (LLMError, ValueError, httpx.InvalidURL, UnicodeError):
            raise ValueError("Invalid model endpoint") from None
        if (url.userinfo or url.query or url.fragment or
                not (scheme == "https" or (scheme == "http" and (is_loopback_host(host) or get_settings().llm_allow_http)))):
            raise ValueError("Invalid model endpoint")
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def valid_name(cls, value):
        if not value.isprintable():
            raise ValueError("Invalid model name")
        return value

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value and not all(33 <= ord(c) <= 126 for c in value.get_secret_value()):
            raise ValueError("Invalid key")
        return value

    @property
    def key(self) -> str:
        return self.api_key.get_secret_value() if self.api_key else ""


class ProfileWrite(Profile):
    role: Scope = "shared"


def masked(base_url: str, model: str, key: str) -> dict:
    return {"base_url": public_endpoint(base_url, key), "model": "" if has_credential(model, key) else model,
            "key_set": bool(key),
            "key_tail": key[-4:] if len(key) >= 16 else None}


class LibraryProfile(Profile):
    api: Literal["chat", "responses"] = "chat"
    vision: StrictBool = False


class SelectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    role: SelectionRole
    entry: str | None


class ModelStore(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    profiles: dict[Scope, Profile] = Field(default_factory=dict)
    library: dict[SavedName, LibraryProfile] = Field(default_factory=dict, max_length=LIBRARY_LIMIT)
    selection: dict[SelectionRole, EntryName] = Field(default_factory=dict)

    def entries(self):
        return (*self.profiles.values(), *self.library.values())


def _check_public_fields(store: ModelStore):
    # A selection refers to an existing entry. File-owned names are masked on display.
    fields = [*store.library,
              *(v for p in store.entries() for v in (p.base_url, p.model)),
              *(v for p in store.library.values() for v in (p.api, "saved"))]
    keys = [p.key for p in store.entries() if p.key]
    if any(key in v for key in keys for v in fields):
        raise ValueError("A public field contains a credential")


def _profiles(groups: dict) -> ModelStore:
    try:
        if groups.keys() - set(SCOPES) - {"library", "selection"}:
            raise ValueError
        store = ModelStore(profiles={s: groups[s] for s in SCOPES if s in groups},
                           library=groups.get("library", {}), selection=groups.get("selection", {}))
        _check_public_fields(store)
        return store
    except ValueError:
        raise StoreUnavailable from None


def read_profiles() -> ModelStore:
    return _profiles(MODEL_FILE.read())


def store_exists() -> bool:
    return (settings_home() / MODEL_FILE.filename).exists()


@contextmanager
def edit_profiles(*, clear=False):
    with MODEL_FILE.edit(clear_corrupt=clear) as groups:
        store = ModelStore() if clear else _profiles(groups)
        yield store
        _check_public_fields(store)
        groups.clear()
        groups.update({r: _stored(p) for r, p in store.profiles.items()})
        if store.library:
            groups["library"] = {name: _stored(p) for name, p in store.library.items()}
        if store.selection:
            groups["selection"] = store.selection


def _stored(profile: Profile) -> dict:
    return {**profile.model_dump(exclude={"api_key"}), "api_key": profile.key}


def replace_profile(profile: Profile, old: Profile | None):
    if profile.api_key is None and old and old.base_url == profile.base_url:
        return profile.model_copy(update={"api_key": old.api_key})
    return profile
