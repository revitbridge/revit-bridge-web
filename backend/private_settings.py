"""Bounded, atomic JSON files in the server's private settings directory."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

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


class StoreCorrupt(StoreUnavailable):
    pass


class StoreFull(StoreUnavailable):
    """A proposed write exceeds this file's byte limit."""


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
    path = str(home / (".settings-" + uuid4().hex))
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


class PrivateSettingsFile:
    def __init__(self, filename: str, lockfile: str, max_bytes: int):
        self.filename = filename
        self.lockfile = lockfile
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def read(self) -> dict:
        try:
            path = settings_home() / self.filename
            with open(path, "rb", opener=_open_windows_read if os.name == "nt" else None) as source:
                raw = source.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise ValueError
            groups = json.loads(raw)
            if not isinstance(groups, dict):
                raise ValueError
            return groups
        except FileNotFoundError:
            return {}
        except (ValueError, RecursionError):
            raise StoreCorrupt from None
        except OSError:
            raise StoreUnavailable from None

    @contextmanager
    def _writer(self, home: Path):
        # A stable lock file spans replace/unlink across host processes. The thread
        # lock also avoids the one-second retry interval of Windows byte-range locks.
        with self._lock, (home / self.lockfile).open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            yield  # close releases either OS lock

    def _replace(self, home: Path, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(raw) > self.max_bytes:
            raise StoreFull
        fd, name = _windows_temp(home) if os.name == "nt" else tempfile.mkstemp(dir=home, prefix=".settings-")
        try:
            with os.fdopen(fd, "wb") as dest:
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                dest.write(raw)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(name, home / self.filename)
        finally:
            Path(name).unlink(missing_ok=True)

    @contextmanager
    def edit(self, *, clear_corrupt=False):
        """Serialize changes across workers. A read-only transaction performs no disk write."""
        try:
            home = settings_home()
            home.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
            with self._writer(home):
                corrupt = False
                try:
                    groups = self.read()
                except StoreCorrupt:
                    if not clear_corrupt:
                        raise
                    groups = {}
                    corrupt = True
                before = deepcopy(groups)
                yield groups
                if not corrupt and groups == before:
                    return
                if groups:
                    self._replace(home, groups)
                else:
                    (home / self.filename).unlink(missing_ok=True)
        except OSError:
            raise StoreUnavailable from None
