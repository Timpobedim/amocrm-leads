"""OAuth 2.0 amoCRM: access-токен живёт сутки, refresh-токен — три месяца и обменивается только один раз.

Отсюда два правила. Обновление идёт под замком: два одновременных обновления отправили бы один и тот
же refresh-токен дважды, второе получило бы отказ, и интеграцию пришлось бы авторизовать заново.
Новая пара токенов сразу атомарно записывается на диск: старый refresh-токен уже недействителен, и
потерять новый при сбое процесса нельзя. Для частных интеграций есть долгосрочный токен — `StaticToken`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import httpx

EARLY_REFRESH = 60  # секунд: обновляем чуть раньше, чтобы токен не истёк на лету


class AmoAuthError(Exception):
    """Токены не обновились — нужна повторная авторизация интеграции."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # unix-время


class TokenSource(Protocol):
    async def access_token(self, http: httpx.AsyncClient) -> str: ...

    async def refresh_after_rejection(self, http: httpx.AsyncClient, rejected: str) -> str: ...


class StaticToken:
    """Долгосрочный токен частной интеграции: срок от суток до пяти лет задаётся при выпуске."""

    def __init__(self, token: str) -> None:
        self.token = token

    async def access_token(self, http: httpx.AsyncClient) -> str:
        return self.token

    async def refresh_after_rejection(self, http: httpx.AsyncClient, rejected: str) -> str:
        raise AmoAuthError("amoCRM отклонил долгосрочный токен — выпустите новый в настройках интеграции")


class TokenManager:
    def __init__(
        self,
        store: Path,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._clock = clock
        self._lock = asyncio.Lock()
        self._tokens: Tokens | None = None
        self.refreshes = 0  # сколько раз обновляли — видно в тестах и логах

    def _load(self) -> Tokens:
        if self._tokens is None:
            if not self.store.exists():
                raise AmoAuthError(f"Нет файла токенов {self.store}: выполните `amo-leads authorize <код>`")
            self._tokens = Tokens(**json.loads(self.store.read_text(encoding="utf-8")))
        return self._tokens

    def save(self, tokens: Tokens) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(tokens)), encoding="utf-8")
        os.replace(temporary, self.store)  # атомарно: либо старая пара, либо новая, но не половина
        self._tokens = tokens

    async def _grant(self, http: httpx.AsyncClient, **fields: str) -> Tokens:
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        }
        response = await http.post("/oauth2/access_token", json={**body, **fields})
        if response.status_code != 200:
            raise AmoAuthError(
                f"amoCRM не выдал токены ({response.status_code}): нужна повторная авторизация интеграции"
            )
        data = response.json()
        tokens = Tokens(data["access_token"], data["refresh_token"], self._clock() + int(data["expires_in"]))
        self.save(tokens)
        return tokens

    async def exchange_code(self, http: httpx.AsyncClient, code: str) -> Tokens:
        """Первая авторизация: код из ссылки установки интеграции действует 20 минут."""
        async with self._lock:
            return await self._grant(http, grant_type="authorization_code", code=code)

    async def _refresh(self, http: httpx.AsyncClient) -> Tokens:
        self.refreshes += 1
        return await self._grant(http, grant_type="refresh_token", refresh_token=self._load().refresh_token)

    async def access_token(self, http: httpx.AsyncClient) -> str:
        async with self._lock:
            tokens = self._load()
            if tokens.expires_at - EARLY_REFRESH <= self._clock():
                tokens = await self._refresh(http)
            return tokens.access_token

    async def refresh_after_rejection(self, http: httpx.AsyncClient, rejected: str) -> str:
        """401 на запрос: обновляем, если этого ещё не сделала соседняя задача, отправившая тот же токен."""
        async with self._lock:
            if self._load().access_token != rejected:
                return self._load().access_token
            return (await self._refresh(http)).access_token
