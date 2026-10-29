"""Telegram alerts and commands. Never raises and never blocks trading logic."""
from __future__ import annotations

import logging
import os
import threading

import requests

log = logging.getLogger("alerts")


class Alerts:
    def __init__(self, token: str | None = None, chat_id: str | None = None, timeout: float = 5.0):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self.offset = 0
        self.sent: list[str] = []          # last messages, handy for tests and status

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, blocking: bool = False) -> None:
        log.info("ALERT %s", text)
        self.sent.append(text)
        del self.sent[:-50]
        if not self.enabled:
            return
        t = threading.Thread(target=self._post, args=(text,), daemon=True)
        t.start()
        if blocking:
            t.join(self.timeout + 1)

    def _post(self, text: str) -> None:
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": text[:4000]}, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - alerts must never break the bot
            log.warning("telegram send failed: %s", type(e).__name__)

    def poll_commands(self) -> list[str]:
        """New /commands from the configured chat only."""
        if not self.enabled:
            return []
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                             params={"offset": self.offset, "timeout": 0}, timeout=self.timeout).json()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram poll failed: %s", type(e).__name__)
            return []
        cmds = []
        for u in r.get("result", []):
            self.offset = max(self.offset, u["update_id"] + 1)
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) == str(self.chat_id):
                text = (m.get("text") or "").strip().lower()
                if text.startswith("/"):
                    cmds.append(text.split()[0].split("@")[0])
        return cmds
