import logging
from datetime import timedelta
from typing import Any

import httpx

from talon.data.state import StateDB

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._has_token = bool(token)
        self._http = httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    @property
    def can_send(self) -> bool:
        return self._has_token and bool(self._chat_id)

    def send(self, text: str) -> bool:
        if not self.can_send:
            log.warning("telegram not configured, dropping message: %s", text[:120])
            return False
        try:
            response = self._http.post(
                "/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text[:MAX_MESSAGE_LENGTH],
                    "disable_web_page_preview": True,
                },
            )
            payload = response.json()
            if response.status_code != 200 or not payload.get("ok", False):
                log.error("telegram send failed: %s %s", response.status_code, str(payload)[:200])
                return False
            return True
        except Exception as exc:
            log.error("telegram send error: %s", exc)
            return False

    def get_me(self) -> dict[str, Any] | None:
        if not self._has_token:
            return None
        try:
            response = self._http.get("/getMe")
            payload = response.json()
            if payload.get("ok"):
                return dict(payload["result"])
        except Exception as exc:
            log.error("telegram getMe error: %s", exc)
        return None

    def list_chats(self) -> list[tuple[int, str]]:
        if not self._has_token:
            return []
        try:
            response = self._http.get("/getUpdates")
            payload = response.json()
        except Exception as exc:
            log.error("telegram getUpdates error: %s", exc)
            return []
        chats: dict[int, str] = {}
        for update in payload.get("result", []):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat")
            if chat and "id" in chat:
                label = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
                chats[int(chat["id"])] = str(label)
        return sorted(chats.items())


class Alerter:
    def __init__(self, notifier: TelegramNotifier, state: StateDB, cooldown: timedelta) -> None:
        self._notifier = notifier
        self._state = state
        self._cooldown = cooldown

    def alert(self, key: str, text: str) -> bool:
        if not self._state.should_alert(key, self._cooldown):
            log.info("alert suppressed by cooldown: %s", key)
            return False
        if self._notifier.send(f"[talon] {text}"):
            self._state.mark_alerted(key)
            return True
        return False
