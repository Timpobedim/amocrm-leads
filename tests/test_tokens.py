"""OAuth amoCRM: одноразовый refresh-токен, одно обновление на всех, атомарная запись, долгосрочный токен."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from conftest import client, manager

from amo_leads.testing import BASE, VALID_CODE, AmoEmulator
from amo_leads.tokens import AmoAuthError, StaticToken, TokenManager


async def test_expired_token_is_refreshed_and_the_new_pair_saved(
    amo: AmoEmulator, tokens: TokenManager
) -> None:
    amo.expire_access_tokens()

    async with client(amo, tokens) as amo_client:
        await amo_client.request("GET", "/api/v4/contacts", params={"query": "900"})

    saved = json.loads(tokens.store.read_text(encoding="utf-8"))
    assert tokens.refreshes == 1
    assert saved["refresh_token"] != amo.first.refresh_token
    assert amo.first.refresh_token not in amo.refresh_tokens  # старый refresh-токен уже сгорел
    assert not list(tokens.store.parent.glob("*.tmp"))


async def test_concurrent_rejections_refresh_the_token_once(amo: AmoEmulator, tokens: TokenManager) -> None:
    amo.expire_access_tokens()

    async with client(amo, tokens) as amo_client:
        await asyncio.gather(
            *(amo_client.request("GET", "/api/v4/contacts", params={"query": "900"}) for _ in range(5))
        )

    assert tokens.refreshes == 1  # без замка пять задач отправили бы один refresh-токен пять раз


async def test_token_is_refreshed_before_it_expires(amo: AmoEmulator, tmp_path: Path) -> None:
    tokens = manager(amo, tmp_path / "tokens.json")
    tokens._clock = lambda: amo.first.expires_at - 30  # до истечения полминуты — пора обновить

    async with client(amo, tokens) as amo_client:
        await amo_client.request("GET", "/api/v4/contacts", params={"query": "900"})

    assert tokens.refreshes == 1
    first_api_call = next(request for request in amo.requests if request.url.path.startswith("/api/"))
    assert first_api_call.headers["Authorization"] != f"Bearer {amo.first.access_token}"


async def test_second_process_with_a_stale_token_file_is_told_to_reauthorize(
    amo: AmoEmulator, tmp_path: Path
) -> None:
    first, second = manager(amo, tmp_path / "a.json"), manager(amo, tmp_path / "b.json")

    async with httpx.AsyncClient(base_url=BASE, transport=amo.transport()) as http:
        await first.refresh_after_rejection(http, amo.first.access_token)
        with pytest.raises(AmoAuthError, match="повторная авторизация"):
            await second.refresh_after_rejection(http, amo.first.access_token)


async def test_first_authorization_by_code(amo: AmoEmulator, tmp_path: Path) -> None:
    tokens = TokenManager(
        tmp_path / "tokens.json", client_id="id", client_secret="secret", redirect_uri="https://s"
    )

    async with httpx.AsyncClient(base_url=BASE, transport=amo.transport()) as http:
        issued = await tokens.exchange_code(http, VALID_CODE)

    assert issued.access_token in amo.access_tokens
    assert json.loads(tokens.store.read_text(encoding="utf-8"))["access_token"] == issued.access_token


async def test_missing_token_file_is_explained(tmp_path: Path) -> None:
    tokens = TokenManager(
        tmp_path / "нет.json", client_id="id", client_secret="secret", redirect_uri="https://s"
    )

    async with httpx.AsyncClient(base_url=BASE) as http:
        with pytest.raises(AmoAuthError, match="amo-leads authorize"):
            await tokens.access_token(http)


async def test_long_lived_token_is_used_as_is(amo: AmoEmulator) -> None:
    async with client(amo, StaticToken(amo.first.access_token)) as amo_client:
        await amo_client.request("GET", "/api/v4/contacts", params={"query": "900"})
        amo.expire_access_tokens()
        with pytest.raises(AmoAuthError, match="долгосрочный токен"):
            await amo_client.request("GET", "/api/v4/contacts", params={"query": "900"})
