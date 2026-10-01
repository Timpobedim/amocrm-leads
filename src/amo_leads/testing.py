"""Эмулятор нужной части amoCRM для тестов — через httpx.MockTransport, без сети.

Повторяет то, от чего зависит приёмник заявок: OAuth с одноразовым refresh-токеном (повторно
отправленный токен отклоняется), истечение access-токена (401), лимит 7 запросов в секунду (429),
поиск контакта, сделку с контактом одним запросом, примечания, постраничный список сделок.
"""

from __future__ import annotations

import itertools
import json
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

import httpx

from amo_leads.tokens import Tokens

BASE = "https://school.amocrm.ru"
VALID_CODE = "valid-code"


def _digits(text: object) -> str:
    return "".join(filter(str.isdigit, str(text)))


class AmoEmulator:
    def __init__(self, *, rate: int = 7, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = rate
        self._clock = clock
        self._ids = itertools.count(1001)
        self._tokens = itertools.count(1)
        self.access_tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.contacts: dict[int, dict[str, Any]] = {}
        self.leads: dict[int, dict[str, Any]] = {}
        self.notes: defaultdict[int, list[str]] = defaultdict(list)
        self.requests: list[httpx.Request] = []
        self.fail_next: list[int] = []
        self.too_many = 0  # сколько раз ответили 429
        self._recent: deque[float] = deque()
        self.first = self.issue()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def issue(self) -> Tokens:
        number = next(self._tokens)
        tokens = Tokens(f"access-{number}", f"refresh-{number}", time.time() + 86400)
        self.access_tokens.add(tokens.access_token)
        self.refresh_tokens.add(tokens.refresh_token)
        return tokens

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    def add_contact(self, name: str, phone: str) -> int:
        contact_id = next(self._ids)
        self.contacts[contact_id] = {
            "id": contact_id,
            "name": name,
            "custom_fields_values": [
                {"field_code": "PHONE", "values": [{"value": phone, "enum_code": "WORK"}]}
            ],
        }
        return contact_id

    @staticmethod
    def _json(status: int, payload: Any) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def _problem(
        self, status: int, title: str, detail: str = "", errors: list[dict[str, str]] | None = None
    ) -> httpx.Response:
        body: dict[str, Any] = {"title": title, "status": status, "detail": detail}
        if errors:
            body["validation-errors"] = [{"request_id": "0", "errors": errors}]
        return self._json(status, body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content) if request.content else None
        if request.url.path == "/oauth2/access_token":
            return self._oauth(body or {})
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.access_tokens:
            return self._problem(401, "Unauthorized")
        now = self._clock()
        while self._recent and self._recent[0] <= now - 1:
            self._recent.popleft()
        if len(self._recent) >= self.rate:
            self.too_many += 1
            return self._problem(429, "Too Many Requests")
        self._recent.append(now)
        if self.fail_next:
            return self._problem(self.fail_next.pop(0), "Service Unavailable")
        path, params = request.url.path, request.url.params
        if request.method == "GET" and path == "/api/v4/contacts":
            query = _digits(params.get("query", ""))
            found = [
                contact
                for contact in self.contacts.values()
                if query
                and any(
                    query in _digits(v["value"]) for f in contact["custom_fields_values"] for v in f["values"]
                )
            ]
            return httpx.Response(204) if not found else self._json(200, {"_embedded": {"contacts": found}})
        if request.method == "GET" and path == "/api/v4/leads":
            return self._leads_page(int(params.get("page", 1)), int(params.get("limit", 50)))
        if request.method == "POST" and path == "/api/v4/leads/complex":
            return self._complex(body or [])
        if request.method == "POST" and path.startswith("/api/v4/leads/") and path.endswith("/notes"):
            lead_id = int(path.split("/")[4])
            if lead_id not in self.leads:
                return self._problem(404, "Not Found", "Сделка не найдена")
            self.notes[lead_id] += [note["params"]["text"] for note in body or []]
            return self._json(200, {"_embedded": {"notes": [{"id": next(self._ids), "entity_id": lead_id}]}})
        return self._problem(404, "Not Found", f"{request.method} {path}")

    def _oauth(self, body: dict[str, Any]) -> httpx.Response:
        if body.get("grant_type") == "authorization_code" and body.get("code") == VALID_CODE:
            tokens = self.issue()
        elif body.get("grant_type") == "refresh_token" and body.get("refresh_token") in self.refresh_tokens:
            self.refresh_tokens.discard(body["refresh_token"])  # refresh-токен одноразовый
            tokens = self.issue()
        else:
            return self._problem(400, "Bad Request", "Refresh token has been revoked")
        return self._json(
            200,
            {
                "token_type": "Bearer",
                "expires_in": 86400,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
            },
        )

    def _complex(self, items: list[dict[str, Any]]) -> httpx.Response:
        if not items or len(items) > 50:
            return self._problem(400, "Bad Request", "Нужно от 1 до 50 сделок")
        results = []
        for index, lead in enumerate(items):
            if not str(lead.get("name", "")).strip():
                return self._problem(
                    400,
                    "Bad Request",
                    "Request validation failed",
                    [{"path": f"{index}.name", "detail": "This value should not be blank."}],
                )
            contact = (lead.get("_embedded", {}).get("contacts") or [{}])[0]
            if "id" in contact:
                if contact["id"] not in self.contacts:
                    return self._problem(
                        400,
                        "Bad Request",
                        "Request validation failed",
                        [{"path": f"{index}._embedded.contacts.0.id", "detail": "Contact not found"}],
                    )
                contact_id = int(contact["id"])
            else:
                contact_id = next(self._ids)
                self.contacts[contact_id] = {
                    "id": contact_id,
                    "name": contact.get("first_name", ""),
                    "custom_fields_values": contact.get("custom_fields_values", []),
                }
            lead_id = next(self._ids)
            self.leads[lead_id] = {**lead, "id": lead_id, "contact_id": contact_id}
            results.append(
                {
                    "id": lead_id,
                    "contact_id": contact_id,
                    "company_id": None,
                    "request_id": [str(index)],
                    "merged": False,
                }
            )
        return self._json(200, results)

    def _leads_page(self, page: int, limit: int) -> httpx.Response:
        leads = list(self.leads.values())
        chunk = leads[(page - 1) * limit : page * limit]
        if not chunk:
            return httpx.Response(204)
        links: dict[str, Any] = {"self": {"href": f"{BASE}/api/v4/leads?page={page}&limit={limit}"}}
        if page * limit < len(leads):
            links["next"] = {"href": f"{BASE}/api/v4/leads?page={page + 1}&limit={limit}"}
        return self._json(200, {"_page": page, "_links": links, "_embedded": {"leads": chunk}})
