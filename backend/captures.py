"""Authenticate browser-held view exports without retaining their pixels on the host."""
import hashlib
import hmac
import secrets

from pydantic import BaseModel, Field, SecretStr

_signing_key = secrets.token_bytes(32)
MAX_ENCODED = 4 * ((1024 * 1024 + 2) // 3)


def _signature(session_id: str, scope: str, nonce: str, png: str) -> str:
    # Only called on validated package output or bounded ASCII input. Session IDs are
    # server-minted UUIDs. The PNG string cannot contain the NUL field delimiter.
    mac = hmac.new(_signing_key, digestmod=hashlib.sha256)
    mac.update(f"{session_id}\0{scope}\0{nonce}\0".encode())
    mac.update(png.encode("ascii"))
    return mac.hexdigest()


def capture_id(session_id: str, scope: str, png: str) -> str:
    nonce = secrets.token_hex(8)
    return nonce + "." + _signature(session_id, scope, nonce, png)


class CaptureAttachment(BaseModel):
    capture_id: str = Field(pattern=r"^[0-9a-f]{16}\.[0-9a-f]{64}$")
    png_base64: SecretStr = Field(max_length=MAX_ENCODED, repr=False)

    def verified_png(self, session_id: str, scope: str) -> str | None:
        png = self.png_base64.get_secret_value()
        nonce, signature = self.capture_id.split(".")
        if png.isascii() and hmac.compare_digest(signature, _signature(session_id, scope, nonce, png)):
            return png
        return None
