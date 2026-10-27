"""
memeterm.secrets_store - API credentials on disk, as safely as a phone allows.

Design notes
------------
* The file lives outside the git repository (default ~/.meme_terminal), in a
  0700 directory, written 0600 and replaced atomically.
* Nothing here ever prints, logs or returns the secret. `status()` returns a
  mask and a one-way fingerprint; `get()` exists for the signer only.
* If the platform cannot enforce file permissions (some Android FUSE setups
  ignore chmod), we report `permissions_enforced: false` instead of pretending
  the file is protected - the UI shows that warning.
* There is deliberately no "wallet" concept: no seed phrase, no private key,
  ever. Binance API keys are the only credential this program understands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .util import (atomic_write, ensure_dir, file_mode, fingerprint, mask_key,
                   read_json, utc_now, utc_str)

MIN_KEY_LEN = 16
MAX_KEY_LEN = 200


class CredentialError(ValueError):
    """Invalid or unavailable credentials - the message is safe to show."""


@dataclass
class Credentials:
    api_key: str
    api_secret: str
    created_at: float = 0.0

    def __repr__(self) -> str:            # never leak via repr()/tracebacks
        return "Credentials(api_key={}, secret=<redacted>)".format(mask_key(self.api_key))


class SecretsStore:
    """A single-key store: one Binance Spot key pair, nothing else."""

    def __init__(self, data_dir: str):
        self.dir = ensure_dir(os.path.abspath(os.path.expanduser(data_dir)), 0o700)
        self.path = os.path.join(self.dir, "credentials.json")
        self._cached: Optional[Credentials] = None
        self.last_error: str = ""

    # ------------------------------------------------------------------
    def _load_raw(self) -> Dict[str, Any]:
        raw = read_json(self.path, default={}) or {}
        return raw if isinstance(raw, dict) else {}

    def permissions_enforced(self) -> bool:
        mode = file_mode(self.path)
        if mode is None:
            return True
        return (mode & 0o077) == 0

    def _chmod(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _validate(api_key: str, api_secret: str) -> None:
        key = (api_key or "").strip()
        secret = (api_secret or "").strip()
        if not key or not secret:
            raise CredentialError("API key and secret are both required")
        if len(key) < MIN_KEY_LEN or len(secret) < MIN_KEY_LEN:
            raise CredentialError("that does not look like a Binance key pair "
                                  "(both parts are at least {} characters)".format(MIN_KEY_LEN))
        if len(key) > MAX_KEY_LEN or len(secret) > MAX_KEY_LEN:
            raise CredentialError("credential is implausibly long")
        if any(ch.isspace() for ch in key + secret):
            raise CredentialError("credentials must not contain spaces or newlines")
        # A seed phrase is 12/24 words - refuse anything with many spaces.
        if len(key.split()) > 1 or len(secret.split()) > 1:
            raise CredentialError("this program never accepts a seed phrase or private key")

    # ------------------------------------------------------------------
    def set(self, api_key: str, api_secret: str) -> None:
        self._validate(api_key, api_secret)
        payload = {
            "api_key": api_key.strip(),
            "api_secret": api_secret.strip(),
            "created_at": utc_now(),
            "created_utc": utc_str(),
            "warning": "spot-only key, withdrawals disabled, IP-restricted where supported",
        }
        atomic_write(self.path, _dumps(payload), mode=0o600)
        self._chmod()
        self._cached = Credentials(payload["api_key"], payload["api_secret"],
                                   payload["created_at"])
        self.last_error = ""

    def get(self) -> Optional[Credentials]:
        """Only the signer should call this."""
        if self._cached:
            return self._cached
        raw = self._load_raw()
        key, secret = raw.get("api_key"), raw.get("api_secret")
        if not key or not secret:
            return None
        self._cached = Credentials(str(key), str(secret), float(raw.get("created_at") or 0))
        return self._cached

    def delete(self) -> bool:
        self._cached = None
        existed = os.path.exists(self.path)
        try:
            if existed:
                os.unlink(self.path)
        except OSError as exc:                     # pragma: no cover - fs specific
            self.last_error = "could not delete credentials file: {}".format(exc.__class__.__name__)
            return False
        return existed

    # ------------------------------------------------------------------
    def status(self, last_test: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Everything the settings page may display about the connection."""
        raw = self._load_raw()
        key = str(raw.get("api_key") or "")
        secret = str(raw.get("api_secret") or "")
        connected = bool(key and secret)
        return {
            "connected": connected,
            "api_key_masked": mask_key(key) if connected else "",
            "fingerprint": fingerprint(key, secret) if connected else "",
            "stored_at_utc": utc_str(float(raw.get("created_at") or 0)) if connected else "",
            "file_mode": oct(file_mode(self.path) & 0o777) if file_mode(self.path) else None,
            "permissions_enforced": self.permissions_enforced() if connected else None,
            "warning": ("cannot confirm that file permissions are enforced on this "
                        "filesystem - treat the phone as the security boundary"
                        if connected and not self.permissions_enforced() else ""),
            "last_test": last_test or {},
        }


def _dumps(payload: Dict[str, Any]) -> str:
    import json
    return json.dumps(payload, indent=2, sort_keys=True)
