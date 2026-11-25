"""The write-only model credentials in the operator's config directory."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from backend.llm import LLMError, endpoint, has_credential, is_loopback_host, public_endpoint

Scope = Literal["shared", "planner", "analyst", "supervisor"]
SCOPES = ("shared", "planner", "analyst", "supervisor")
_lock = threading.Lock()
MAX_BYTES = 128 * 1024
APP_ROOT = Path(__file__).resolve().parents[1]


def settings_home() -> Path:
    override = os.environ.get("REVIT_BRIDGE_WEB_HOME", "").strip()
    if override:
        home = Path(override).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        home = root / "revit-bridge-web"
    elif sys.platform == "darwin":
        home = Path.home() / "Library/Application Support/revit-bridge-web"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        home = root / "revit-bridge-web"
    home = home.resolve()
    if home.is_relative_to(APP_ROOT / "frontend") or (
            (APP_ROOT / ".git").exists() and home.is_relative_to(APP_ROOT)):
        raise StoreUnavailable
    return home


class StoreUnavailable(Exception):
    """Never attach the file contents or the parser's diagnostic."""


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


def _open_windows_read(path, flags):
    # A worker may replace the file while another reads the previous version.
    import msvcrt
    import pywintypes
    import win32file as wf
    try:
        handle = wf.CreateFile(path, wf.GENERIC_READ, wf.FILE_SHARE_READ | wf.FILE_SHARE_DELETE,
                               None, wf.OPEN_EXISTING, 0, None)
    except pywintypes.error as exc:
        if exc.winerror in (2, 3):
            raise FileNotFoundError from None
        raise StoreUnavailable from None
    return msvcrt.open_osfhandle(handle.Detach(), os.O_RDONLY | os.O_BINARY)


def read_profiles() -> dict[str, Profile]:
    try:
        path = settings_home() / "secrets.json"
        with open(path, "rb", opener=_open_windows_read if os.name == "nt" else None) as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        groups = json.loads(raw)
        if not isinstance(groups, dict) or not groups.keys() <= set(SCOPES):
            raise ValueError
        profiles = {scope: Profile.model_validate(entry) for scope, entry in groups.items()}
        _check_public_fields(profiles)
        return profiles
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        raise StoreUnavailable from None


def _windows_temp(home: Path):
    """Install a protected DACL at creation, before the first byte is written."""
    import msvcrt
    import ntsecuritycon
    import pywintypes
    import win32api
    import win32file
    import win32security as sec

    token = sec.OpenProcessToken(win32api.GetCurrentProcess(), ntsecuritycon.TOKEN_QUERY)
    user = sec.GetTokenInformation(token, sec.TokenUser)[0]
    acl = sec.ACL()
    acl.AddAccessAllowedAce(sec.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user)
    attr = pywintypes.SECURITY_ATTRIBUTES()
    attr.SECURITY_DESCRIPTOR.SetSecurityDescriptorDacl(True, acl, False)
    attr.SECURITY_DESCRIPTOR.SetSecurityDescriptorControl(sec.SE_DACL_PROTECTED, sec.SE_DACL_PROTECTED)
    path = str(home / (".model-" + uuid4().hex))
    try:
        handle = win32file.CreateFile(path, ntsecuritycon.GENERIC_WRITE | ntsecuritycon.READ_CONTROL,
                                      0, attr, win32file.CREATE_NEW, 0, None)
    except pywintypes.error:
        raise StoreUnavailable from None
    try:
        actual = sec.GetSecurityInfo(handle, sec.SE_FILE_OBJECT, sec.DACL_SECURITY_INFORMATION)
        dacl = actual.GetSecurityDescriptorDacl()
        if (not actual.GetSecurityDescriptorControl()[0] & sec.SE_DACL_PROTECTED or not dacl
                or dacl.GetAceCount() != 1 or dacl.GetAce(0)[2] != user):
            raise StoreUnavailable
    except (pywintypes.error, StoreUnavailable):
        handle.Close()
        Path(path).unlink(missing_ok=True)
        raise StoreUnavailable from None
    return msvcrt.open_osfhandle(handle.Detach(), os.O_WRONLY | os.O_BINARY), path


@contextmanager
def _writer(home: Path):
    # A stable lock file spans replace/unlink across host processes. The thread
    # lock also avoids the one-second retry interval of Windows byte-range locks.
    with _lock, (home / ".models.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield  # close releases either OS lock


def _replace(home: Path, profiles: dict[str, Profile]):
    payload = {k: {"base_url": p.base_url, "model": p.model, "api_key": p.key} for k, p in profiles.items()}
    fd, name = _windows_temp(home) if os.name == "nt" else tempfile.mkstemp(dir=home, prefix=".model-")
    try:
        with os.fdopen(fd, "wb") as dest:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            dest.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(name, home / "secrets.json")
    finally:
        Path(name).unlink(missing_ok=True)


def update_profiles(role: Scope | None, profile: ProfileWrite | None = None) -> dict[str, Profile]:
    try:
        home = settings_home()
        home.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
        with _writer(home):
            if role is None:
                (home / "secrets.json").unlink(missing_ok=True)
                return {}
            profiles = read_profiles()
            if profile is None:
                profiles.pop(role, None)
            else:
                old = profiles.get(role)
                # Omitting a key retains it only for the same saved endpoint.
                key = profile.api_key
                if key is None and old and old.base_url == profile.base_url:
                    key = old.api_key
                profiles[role] = Profile(base_url=profile.base_url, model=profile.model, api_key=key)
            _check_public_fields(profiles)
            _replace(home, profiles)
            return profiles
    except OSError:
        raise StoreUnavailable from None
