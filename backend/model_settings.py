"""The write-only model credentials in the operator's config directory."""
from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from backend.llm import LLMError, endpoint, has_credential, is_loopback_host, public_endpoint
from backend.private_settings import PrivateSettingsFile, StoreUnavailable

Scope = Literal["shared", "planner", "analyst", "supervisor"]
SCOPES = ("shared", "planner", "analyst", "supervisor")
MODEL_FILE = PrivateSettingsFile("secrets.json", ".models.lock", 128 * 1024)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
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


def _check_public_fields(profiles: dict[str, Profile]):
    fields = [v for p in profiles.values() for v in (p.base_url, p.model)]
    if any(p.key and any(p.key in v for v in fields) for p in profiles.values()):
        raise ValueError("A public field contains a credential")


def _profiles(groups: dict) -> dict[str, Profile]:
    try:
        if not groups.keys() <= set(SCOPES):
            raise ValueError
        profiles = {scope: Profile.model_validate(groups[scope]) for scope in SCOPES if scope in groups}
        _check_public_fields(profiles)
        return profiles
    except ValueError:
        raise StoreUnavailable from None


def read_profiles() -> dict[str, Profile]:
    return _profiles(MODEL_FILE.read())


def update_profiles(role: Scope | None, profile: ProfileWrite | None = None) -> dict[str, Profile]:
    with MODEL_FILE.edit(clear_corrupt=role is None) as groups:
        profiles = _profiles(groups) if role is not None else {}
        if role is None:
            groups.clear()
        elif profile is None:
            profiles.pop(role, None)
            groups.pop(role, None)
        else:
            old = profiles.get(role)
            # Omitting a key retains it only for the same saved endpoint.
            key = profile.key if profile.api_key is not None else (
                old.key if old and old.base_url == profile.base_url else "")
            groups[role] = {"base_url": profile.base_url, "model": profile.model, "api_key": key}
            profiles[role] = Profile.model_validate(groups[role])
            _check_public_fields(profiles)
        return profiles
