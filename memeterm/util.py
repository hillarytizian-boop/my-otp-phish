"""
memeterm.util - small, dependency-free helpers shared by the terminal.

Nothing here talks to the network and nothing here logs a secret. The
redaction helpers are deliberately paranoid: they run on every string that
can reach a log file, an API response or the browser.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional

VERSION = "2.0.0"

# Patterns that look like credentials. Binance keys are 64-char base64-ish
# strings; Telegram bot tokens are "<digits>:<35 chars>".
_SECRET_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9_-]{35,}:[A-Za-z0-9_-]{30,}\b"),          # telegram bot token
    re.compile(r"\b[A-Za-z0-9+/]{64}={0,2}\b"),                         # binance key/secret
    re.compile(r"(?i)\b(api[_-]?key|api[_-]?secret|secret|token|password)"
               r"(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,&}]{6,})"),
]


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    """Return *text* with known and pattern-detected secrets replaced."""
    out = str(text)
    for secret in secrets:
        if secret and len(str(secret)) >= 6:
            out = out.replace(str(secret), "<redacted>")
    for pattern in _SECRET_PATTERNS[:2]:
        out = pattern.sub("<redacted>", out)
    out = _SECRET_PATTERNS[2].sub(lambda m: m.group(1) + m.group(2) + "<redacted>", out)
    return out


def mask_key(api_key: str) -> str:
    """First four and last four characters - enough to recognise your own key."""
    key = str(api_key or "")
    if len(key) <= 10:
        return "•" * max(0, len(key))
    return "{}…{}".format(key[:4], key[-4:])


def fingerprint(api_key: str, api_secret: str) -> str:
    """Stable, non-reversible id for a key pair (shown in the UI, never the key)."""
    import hashlib
    digest = hashlib.sha256((str(api_key) + "\0" + str(api_secret)).encode()).hexdigest()
    return digest[:12]


def utc_now() -> float:
    return time.time()


def utc_str(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(utc_now() if ts is None else ts))


def fmt_money(x: Optional[float], places: int = 2, symbol: str = "$") -> str:
    if x is None:
        return "?"
    try:
        a = abs(float(x))
    except (TypeError, ValueError):
        return "?"
    sign = "-" if x < 0 else ""
    if a >= 1_000_000:
        return "{}{}{:.2f}M".format(sign, symbol, a / 1_000_000.0)
    if a >= 1_000:
        return "{}{}{:.1f}K".format(sign, symbol, a / 1_000.0)
    if a and a < 0.01:
        return "{}{}{:.6f}".format(sign, symbol, a)
    return "{}{}{:.{}f}".format(sign, symbol, a, places)


def fmt_pct(x: Optional[float], places: int = 2) -> str:
    if x is None:
        return "?"
    try:
        return "{:+.{p}f}%".format(float(x), p=places)
    except (TypeError, ValueError):
        return "?"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def ensure_dir(path: str, mode: int = 0o700) -> str:
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def file_mode(path: str) -> Optional[int]:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def atomic_write(path: str, data: str, mode: int = 0o600) -> None:
    """Write-then-rename so a half-written state file can never be restored."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_json(path: str, payload: Any, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True, default=str), mode)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def append_line(path: str, line: str, mode: int = 0o600) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".", 0o700)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")
    if new:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def bps(value: float) -> float:
    """basis points -> fraction"""
    return float(value) / 10_000.0


def iso_age(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    age = max(0.0, time.time() - ts)
    if age < 60:
        return "{:.0f}s ago".format(age)
    if age < 3600:
        return "{:.0f}m ago".format(age / 60)
    return "{:.1f}h ago".format(age / 3600)


def chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = clamp(q, 0.0, 1.0) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(max(0.0, var))


def dedupe_sorted(values: Iterable[str]) -> List[str]:
    seen: Dict[str, bool] = {}
    out: List[str] = []
    for value in values:
        if value and value not in seen:
            seen[value] = True
            out.append(value)
    return out
