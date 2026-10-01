"""Асинхронный клиент amoCRM API v4 на httpx.

    async with AmoClient("https://school.amocrm.ru", tokens) as amo:
        lead = await amo.add_lead_complex({...})

Особенности:
- параметры кодирует httpx: «Рога & копыта» остаётся одним значением, а пустой список в фильтре —
  явная ошибка;
- записи — POST/PATCH с JSON-телом, а не GET со строкой запроса;
- страницы идут по `_links.next` по очереди и не чаще 7 запросов в секунду;
- повторы ограничены (QUERY_LIMIT_EXCEEDED не повторяется без предела);
- одно соединение на весь клиент;
- 401 — одно обновление токена и повтор запроса.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Self

import httpx

from amo_leads.tokens import AmoAuthError, TokenSource

RATE = 6.5  # amoCRM пускает 7 запросов в секунду; держим чуть меньше — запас на задержки сети
PAGE_LIMIT = 250

Sleep = Callable[[float], Awaitable[None]]


class AmoError(Exception):
    def __init__(self, status: int, title: str, detail: str = "", problems: list[str] | None = None) -> None:
        text = f"amoCRM {status}: {title}" + (f" — {detail}" if detail else "")
        super().__init__(text + ("; " + "; ".join(problems) if problems else ""))
        self.status = status
        self.problems = problems or []


def _error(response: httpx.Response) -> AmoError:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    problems = [
        f"{error.get('path', '?')}: {error.get('detail', error.get('code', ''))}"
        for group in data.get("validation-errors", [])
        for error in group.get("errors", [])
    ]
    return AmoError(
        response.status_code,
        str(data.get("title") or response.reason_phrase),
        str(data.get("detail", "")),
        problems,
    )


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten(params: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Параметры в формате amoCRM: вложенные словари и списки → пары «имя[ключ][]» и значение.

    {"filter": {"id": [1, 2]}} → [("filter[id][]", "1"), ("filter[id][]", "2")].

    Значения не кодируются вручную — это сделает httpx. Пустой список — ошибка: без него фильтр
    исчез бы, и запрос вернул бы все записи вместо ни одной.
    """
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, Mapping):
            pairs += flatten(value, name)
        elif isinstance(value, list | tuple):
            if not value:
                raise ValueError(f"Пустой список в параметре {name}: такой фильтр вернул бы все записи")
            pairs += [(f"{name}[]", _scalar(item)) for item in value]
        elif value is not None:
            pairs.append((name, _scalar(value)))
    return pairs


class Pace:
    """Не чаще `rate` запросов в секунду, равномерно. Резерв времени — без await, поэтому атомарен."""

    def __init__(
        self, rate: float = RATE, *, clock: Callable[[], float] = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        self.rate = rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    async def wait(self) -> None:
        now = self._clock()
        start = max(now, self._next)
        self._next = start + 1 / self.rate
        if start > now:
            await self._sleep(start - now)


class AmoClient:
    def __init__(
        self,
        base_url: str,
        tokens: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pace: Pace | None = None,
        retries: int = 4,
        backoff: float = 0.5,
        sleep: Sleep = asyncio.sleep,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.pace = pace or Pace(sleep=sleep)
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._transport = transport
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._http = httpx.AsyncClient(
            base_url=self.base_url, transport=self._transport, timeout=self._timeout
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Клиент используется вне `async with`")
        return self._http

    async def request(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None
    ) -> Any:
        query = tuple(flatten(params)) if params else None
        token = await self.tokens.access_token(self.http)
        refreshed = False
        for attempt in range(self.retries + 1):
            await self.pace.wait()
            try:
                response = await self.http.request(
                    method, path, params=query, json=json, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise AmoError(0, "нет связи с amoCRM", str(exc)) from exc
                await self._sleep(self.backoff * 2**attempt)
                continue
            if response.status_code == 401 and not refreshed:
                token = await self.tokens.refresh_after_rejection(self.http, token)
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.retries:
                    raise _error(response)
                await self._sleep(self.backoff * 2**attempt)
                continue
            if response.status_code == 401:
                raise AmoAuthError("amoCRM отклонил и обновлённый токен — проверьте интеграцию")
            if response.status_code >= 400:
                raise _error(response)
            return None if response.status_code == 204 or not response.content else response.json()
        raise AssertionError("недостижимо")  # pragma: no cover

    async def get_all(
        self, path: str, key: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Все страницы списка по `_links.next`, по очереди. Пустой список amoCRM отдаёт ответом 204."""
        items: list[dict[str, Any]] = []
        page: Any = await self.request("GET", path, params={**(params or {}), "limit": PAGE_LIMIT})
        while page:
            items += page.get("_embedded", {}).get(key, [])
            next_href = page.get("_links", {}).get("next", {}).get("href")
            page = await self.request("GET", next_href) if next_href else None
        return items

    # --- то, что нужно приёмнику заявок ------------------------------------------------------------

    async def find_contact_by_phone(self, phone: str) -> dict[str, Any] | None:
        """Поиск идёт по подстроке, поэтому совпадение номера проверяется ещё раз по цифрам."""
        digits = "".join(filter(str.isdigit, phone))
        found = await self.request("GET", "/api/v4/contacts", params={"query": digits[-10:]})
        for contact in (found or {}).get("_embedded", {}).get("contacts", []):
            for field in contact.get("custom_fields_values") or []:
                if field.get("field_code") == "PHONE":
                    numbers = [
                        "".join(filter(str.isdigit, str(v.get("value", "")))) for v in field.get("values", [])
                    ]
                    if any(number[-10:] == digits[-10:] for number in numbers):
                        return dict(contact)
        return None

    async def add_lead_complex(self, lead: dict[str, Any]) -> dict[str, Any]:
        """Сделка с контактом одним запросом (POST /api/v4/leads/complex)."""
        result = await self.request("POST", "/api/v4/leads/complex", json=[lead])
        return dict(result[0])

    async def add_note(self, lead_id: int, text: str) -> None:
        await self.request(
            "POST", f"/api/v4/leads/{lead_id}/notes", json=[{"note_type": "common", "params": {"text": text}}]
        )
