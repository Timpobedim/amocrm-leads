"""Клиент amoCRM: параметры, темп, страницы, повторы, ошибки."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import client

from amo_leads.client import AmoError, Pace, flatten
from amo_leads.testing import AmoEmulator
from amo_leads.tokens import TokenManager


def test_params_are_nested_the_amocrm_way() -> None:
    params = {"filter": {"id": [1, 2], "name": "Рога & копыта"}, "with": "contacts", "order": {"id": "desc"}}

    assert flatten(params) == [
        ("filter[id][]", "1"),
        ("filter[id][]", "2"),
        ("filter[name]", "Рога & копыта"),
        ("with", "contacts"),
        ("order[id]", "desc"),
    ]


def test_ampersand_stays_inside_the_value() -> None:
    request = httpx.Request(
        "GET", "https://school.amocrm.ru/api/v4/leads", params=flatten({"query": "Рога & копыта"})
    )

    # строка запроса, собранная вручную, дала бы два параметра: «Рога » и « копыта»
    assert dict(request.url.params) == {"query": "Рога & копыта"}


def test_empty_list_in_a_filter_is_an_error() -> None:
    with pytest.raises(ValueError, match="вернул бы все записи"):
        flatten({"filter": {"id": []}})


async def test_pace_keeps_amocrm_from_refusing(amo: AmoEmulator, tokens: TokenManager) -> None:
    async with client(amo, tokens, pace=Pace()) as amo_client:  # настоящий темп 6,5 запроса в секунду
        await asyncio.gather(
            *(amo_client.request("GET", "/api/v4/contacts", params={"query": "900"}) for _ in range(10))
        )

    assert amo.too_many == 0


async def test_list_is_walked_page_by_page(amo: AmoEmulator, tokens: TokenManager) -> None:
    for number in range(600):
        amo.leads[number] = {"id": number, "name": f"Сделка {number}"}

    async with client(amo, tokens) as amo_client:
        leads = await amo_client.get_all("/api/v4/leads", "leads")

    assert [lead["id"] for lead in leads] == list(range(600))
    assert sum(request.url.path == "/api/v4/leads" for request in amo.requests) == 3  # по 250 на страницу


async def test_empty_list_answer_is_204(amo: AmoEmulator, tokens: TokenManager) -> None:
    async with client(amo, tokens) as amo_client:
        assert await amo_client.get_all("/api/v4/leads", "leads") == []


async def test_overload_is_retried_but_not_forever(amo: AmoEmulator, tokens: TokenManager) -> None:
    amo.fail_next = [503, 429]
    async with client(amo, tokens) as amo_client:
        await amo_client.request("GET", "/api/v4/contacts", params={"query": "900"})

    amo.fail_next = [503] * 10
    async with client(amo, tokens, retries=2) as amo_client:
        with pytest.raises(AmoError, match="amoCRM 503"):
            await amo_client.request("GET", "/api/v4/contacts", params={"query": "900"})


async def test_validation_errors_are_readable(amo: AmoEmulator, tokens: TokenManager) -> None:
    async with client(amo, tokens) as amo_client:
        with pytest.raises(AmoError, match=r"0\.name: This value should not be blank"):
            await amo_client.add_lead_complex({"name": " "})


async def test_client_outside_context_fails_clearly(amo: AmoEmulator, tokens: TokenManager) -> None:
    with pytest.raises(RuntimeError, match="вне `async with`"):
        await client(amo, tokens).request("GET", "/api/v4/leads")
