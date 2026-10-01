from __future__ import annotations

from pathlib import Path

import pytest

from amo_leads.client import AmoClient, Pace
from amo_leads.testing import BASE, AmoEmulator
from amo_leads.tokens import TokenManager, TokenSource


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def amo() -> AmoEmulator:
    return AmoEmulator()


def manager(amo: AmoEmulator, store: Path) -> TokenManager:
    tokens = TokenManager(
        store, client_id="id", client_secret="secret", redirect_uri="https://school.example/amo"
    )
    tokens.save(amo.first)
    return tokens


@pytest.fixture
def tokens(amo: AmoEmulator, tmp_path: Path) -> TokenManager:
    return manager(amo, tmp_path / "tokens.json")


def client(amo: AmoEmulator, tokens: TokenSource, *, retries: int = 4, pace: Pace | None = None) -> AmoClient:
    """Клиент к эмулятору без пауз: темп и повторы проверяются отдельными тестами."""
    return AmoClient(
        BASE,
        tokens,
        transport=amo.transport(),
        sleep=no_sleep,
        pace=pace or Pace(rate=1000, sleep=no_sleep),
        retries=retries,
    )
