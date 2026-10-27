"""
memeterm.notify - optional Telegram alerts with de-duplication.

Credentials come from the environment only (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`), exactly like the existing bot. If they are absent the
whole notifier is a no-op and the terminal behaves identically.

Every message is redacted before it leaves the process, and messages are
queued with a (kind, key) de-duplication window so a refresh loop cannot spam
the chat.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .util import mask_key, redact, utc_str


class TelegramNotifier:
    def __init__(self, *, token: Optional[str] = None, chat_id: Optional[str] = None,
                 timeout: float = 8.0, min_interval_s: float = 1.2,
                 transport: Optional[Any] = None):
        self.token = (token if token is not None
                      else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None
                        else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        self.timeout = timeout
        self.min_interval_s = min_interval_s
        self.transport = transport
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_send = 0.0
        self.sent = 0
        self.skipped = 0
        self.failed = 0
        self.last_error = ""

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "sent": self.sent, "skipped": self.skipped,
                "failed": self.failed, "last_error": self.last_error,
                "chat_configured": bool(self.chat_id),
                "token_fingerprint": mask_key(self.token) if self.token else ""}

    # ------------------------------------------------------------------
    def _duplicate(self, key: str, cooldown_s: float) -> bool:
        now = time.time()
        with self._lock:
            last = self._seen.get(key)
            # opportunistic cleanup so the dict cannot grow forever
            if len(self._seen) > 500:
                self._seen = {k: v for k, v in self._seen.items() if now - v < 86_400}
            if last is not None and (now - last) < cooldown_s:
                return True
            self._seen[key] = now
            return False

    def send(self, text: str, *, key: str = "", cooldown_s: float = 60.0,
             silent: bool = False) -> bool:
        if not self.enabled:
            return False
        if key and self._duplicate(key, cooldown_s):
            self.skipped += 1
            return False
        body = redact(text)[:3800]
        payload = json.dumps({"chat_id": self.chat_id, "text": body,
                              "disable_web_page_preview": True,
                              "disable_notification": silent}).encode()
        url = "https://api.telegram.org/bot{}/sendMessage".format(self.token)
        gap = time.time() - self._last_send
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        try:
            self._post(url, payload)
            self._last_send = time.time()
            self.sent += 1
            return True
        except Exception as exc:                 # never break trading for a chat message
            self.failed += 1
            self.last_error = redact("{}: {}".format(exc.__class__.__name__, exc))[:160]
            return False

    def _post(self, url: str, payload: bytes) -> None:
        if self.transport is not None:
            self.transport.request("POST", url, {"Content-Type": "application/json"},
                                   payload)
            return
        request = urllib.request.Request(url, data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if int(getattr(response, "status", 200)) >= 400:
                raise RuntimeError("HTTP {}".format(response.status))

    # ------------------------------------------------------------------
    # message builders
    # ------------------------------------------------------------------
    def order_event(self, result: Any, *, mode: str) -> None:
        if not self.enabled:
            return
        order = getattr(result, "order", None)
        symbol = getattr(order, "symbol", "")
        side = getattr(order, "side", "")
        status = getattr(result, "status", "")
        if not getattr(result, "ok", False):
            self.order_rejected(symbol=symbol or "?", side=side,
                                reason=getattr(result, "message", "rejected"))
            return
        text = "\n".join([
            "[{}] {} {}".format(mode, status, symbol),
            "side: {}   qty: {:.8g}".format(side, float(getattr(result, "qty", 0) or 0)),
            "price: {:.8g}   fee: {:.6f}".format(float(getattr(result, "fill_price", 0) or 0),
                                                 float(getattr(result, "fee", 0) or 0)),
            "order id: {}".format(getattr(order, "client_order_id", "")),
            "{} UTC".format(utc_str()),
            "Not financial advice.",
        ])
        self.send(text, key="order:{}:{}".format(symbol, status), cooldown_s=5.0)

    def order_rejected(self, *, symbol: str, side: str, reason: str) -> None:
        if not self.enabled:
            return
        text = "\n".join([
            "[REJECTED] {} {}".format(side or "?", symbol or "?"),
            "reason: {}".format(reason),
            "no order was sent to the exchange.",
            "{} UTC".format(utc_str()),
        ])
        self.send(text, key="reject:{}:{}".format(symbol, reason[:40]), cooldown_s=120.0)

    def risk_warning(self, *, kind: str, detail: str) -> None:
        if not self.enabled:
            return
        text = "\n".join(["[RISK] {}".format(kind), detail,
                          "{} UTC".format(utc_str())])
        self.send(text, key="risk:{}".format(kind), cooldown_s=900.0)

    def signal(self, *, symbol: str, verdict: str, score: float, price: float,
               detail: str = "") -> None:
        if not self.enabled:
            return
        text = "\n".join([
            "[SIGNAL] {} {}".format(verdict, symbol),
            "score: {:.1f}/100   price: {:.8g}".format(score, price),
            detail,
            "PAPER MODE BY DEFAULT - no real order is placed by a signal.",
            "{} UTC".format(utc_str()),
        ])
        self.send(text, key="signal:{}:{}".format(symbol, verdict), cooldown_s=1800.0)

    def daily_summary(self, *, equity: float, day_pnl: float, trades: int,
                      wins: int, mode: str) -> None:
        if not self.enabled:
            return
        text = "\n".join([
            "[DAILY] {} summary".format(mode),
            "equity: {:.2f}   day PnL: {:+.2f}".format(equity, day_pnl),
            "closed trades: {}   wins: {}".format(trades, wins),
            "{} UTC".format(utc_str()),
        ])
        self.send(text, key="daily:{}".format(time.strftime("%Y-%m-%d")), cooldown_s=3600.0)
