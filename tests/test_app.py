"""Приёмник заявок целиком: сайт отправляет форму, заявка оказывается в amoCRM (эмуляторе)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import no_sleep
from fastapi.testclient import TestClient

from amo_leads.app import create_app
from amo_leads.client import AmoClient
from amo_leads.delivery import Delivery
from amo_leads.outbox import Outbox
from amo_leads.settings import Settings
from amo_leads.testing import BASE, AmoEmulator

NOW = datetime(2026, 9, 14, 10, 0)
FORM = {
    "name": "Мария",
    "phone": "8 (900) 123-45-67",
    "email": "maria@example.ru",
    "course": "english",
    "comment": "Удобно по вечерам",
    "consent": True,
    "utm_source": "yandex",
    "utm_campaign": "autumn",
}


@pytest.fixture
def settings(amo: AmoEmulator, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        base_url=BASE,
        long_lived_token=amo.first.access_token,
        database=tmp_path / "db.sqlite3",
    )  # type: ignore[call-arg]


@pytest.fixture
def site(settings: Settings, amo: AmoEmulator) -> Iterator[TestClient]:
    app = create_app(settings, transport=amo.transport(), now=lambda: NOW, sleep=no_sleep, start_worker=False)
    with TestClient(app) as client:
        yield client


def test_new_client_becomes_a_lead_with_contact_and_note(site: TestClient, amo: AmoEmulator) -> None:
    response = site.post("/forms/trial", json=FORM)

    assert response.status_code == 201
    lead = amo.leads[response.json()["lead_id"]]
    assert lead["name"] == "Пробный урок: Английский"
    contact = amo.contacts[lead["contact_id"]]
    assert contact["name"] == "Мария"
    assert contact["custom_fields_values"][0]["values"][0]["value"] == "+79001234567"
    [note] = amo.notes[lead["id"]]
    assert "Комментарий: Удобно по вечерам" in note
    assert "UTM: source=yandex, campaign=autumn" in note


def test_known_client_is_not_duplicated(site: TestClient, amo: AmoEmulator) -> None:
    known = amo.add_contact("Мария Иванова", "+7 (900) 123-45-67")

    lead_id = site.post("/forms/trial", json=FORM).json()["lead_id"]

    assert amo.leads[lead_id]["contact_id"] == known
    assert len(amo.contacts) == 1


def test_double_click_creates_one_lead(site: TestClient, amo: AmoEmulator) -> None:
    first = site.post("/forms/trial", json=FORM)
    second = site.post("/forms/trial", json=FORM)

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["duplicate"] is True
    assert len(amo.leads) == 1


async def _deliver_later(settings: Settings, amo: AmoEmulator, now: datetime) -> int:
    async with AmoClient(
        settings.base_url, settings.token_source(), transport=amo.transport(), sleep=no_sleep
    ) as client:
        return await Delivery(
            client, Outbox(settings.database), pipeline_id=None, now=lambda: now
        ).deliver_due()


def test_crm_outage_keeps_the_lead_and_the_queue_delivers_it(
    site: TestClient, amo: AmoEmulator, settings: Settings
) -> None:
    amo.fail_next = [503] * 5  # все попытки клиента
    # лимит частоты здесь не проверяется (это делают тесты клиента): тест идёт без пауз, и пять
    # неудачных попыток приёмника иначе съели бы секундный бюджет эмулятора, нужный досылке
    amo.rate = 1000

    response = site.post("/forms/trial", json=FORM)

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert not amo.leads
    assert site.get("/health").json()["queue"] == {"pending": 1, "sent": 0, "failed": 0}

    assert asyncio.run(_deliver_later(settings, amo, NOW)) == 0  # рано: следующая попытка через минуту
    assert asyncio.run(_deliver_later(settings, amo, NOW + timedelta(minutes=2))) == 1
    assert len(amo.leads) == 1
    assert site.get("/health").json()["queue"] == {"pending": 0, "sent": 1, "failed": 0}


def test_rejected_submission_is_left_for_a_human(site: TestClient, amo: AmoEmulator) -> None:
    amo.fail_next = [400]

    response = site.post("/forms/trial", json=FORM)

    assert response.status_code == 202  # посетителю сайта — «принято», разбираться будет менеджер
    assert site.get("/health").json()["queue"]["failed"] == 1


def test_form_errors_are_explained_in_russian(site: TestClient) -> None:
    response = site.post(
        "/forms/trial", json={**FORM, "name": "", "phone": "123", "course": "french", "consent": False}
    )

    assert response.status_code == 422
    assert response.json()["errors"] == {
        "name": "Заполните поле",
        "phone": "Телефон нужен российский мобильный, например +7 900 123-45-67",
        "course": "Выберите значение из списка",
        "consent": "Без согласия на обработку персональных данных заявку принять нельзя",
    }
