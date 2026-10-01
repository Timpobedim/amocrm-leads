"""Форма заявки: телефон, сделка для amoCRM, примечание, ключ идемпотентности."""

from __future__ import annotations

from datetime import date

import pytest

from amo_leads.forms import TrialForm, lead_payload, normalize_phone, note_text

FORM = TrialForm(
    name="Мария",
    phone="8 (900) 123-45-67",
    email="maria@example.ru",
    course="english",
    comment="Удобно по вечерам",
    consent=True,
    utm_source="yandex",
    utm_campaign="autumn",
)


@pytest.mark.parametrize(
    ("raw", "phone"),
    [
        ("8 (900) 123-45-67", "+79001234567"),
        ("+7 900 1234567", "+79001234567"),
        ("9001234567", "+79001234567"),
        ("123-45-67", None),
        ("+1 555 123 4567", None),
    ],
)
def test_phone_is_normalized(raw: str, phone: str | None) -> None:
    assert normalize_phone(raw) == phone


def test_new_contact_goes_with_phone_email_and_tags() -> None:
    lead = lead_payload(FORM, None, pipeline_id=42)

    assert lead["name"] == "Пробный урок: Английский"
    assert lead["pipeline_id"] == 42
    [contact] = lead["_embedded"]["contacts"]
    assert contact == {
        "first_name": "Мария",
        "custom_fields_values": [
            {"field_code": "PHONE", "values": [{"value": "+79001234567", "enum_code": "MOB"}]},
            {"field_code": "EMAIL", "values": [{"value": "maria@example.ru", "enum_code": "WORK"}]},
        ],
    }
    assert lead["_embedded"]["tags"] == [{"name": "Сайт"}, {"name": "Английский"}]


def test_known_contact_is_linked_by_id() -> None:
    lead = lead_payload(FORM, 1001, pipeline_id=None)

    assert lead["_embedded"]["contacts"] == [{"id": 1001}]
    assert "pipeline_id" not in lead


def test_note_keeps_comment_and_utm() -> None:
    assert note_text(FORM).splitlines() == [
        "Заявка с сайта: пробный урок, английский",
        "Имя: Мария; телефон: +79001234567; e-mail: maria@example.ru",
        "Комментарий: Удобно по вечерам",
        "UTM: source=yandex, campaign=autumn",
    ]


def test_same_form_same_day_is_one_submission() -> None:
    today, tomorrow = date(2026, 9, 14), date(2026, 9, 15)

    assert FORM.idempotency_key(today) == FORM.model_copy(update={"comment": "другой текст"}).idempotency_key(
        today
    )
    assert FORM.idempotency_key(today) != FORM.idempotency_key(tomorrow)
