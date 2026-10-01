"""Форма «Записаться на пробный урок» языковой школы: проверка полей и то, что уйдёт в amoCRM."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

COURSES = {"english": "Английский", "german": "Немецкий", "chinese": "Китайский", "spanish": "Испанский"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_phone(value: str) -> str | None:
    """«8 (900) 123-45-67», «+7 900 1234567», «9001234567» → «+79001234567»."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return f"+7{digits}" if len(digits) == 10 and digits[0] == "9" else None


class TrialForm(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str = Field(min_length=1, max_length=100)
    phone: str
    email: str | None = None
    course: Literal["english", "german", "chinese", "spanish"]
    comment: str = Field(default="", max_length=1000)
    consent: bool  # согласие на обработку персональных данных (152-ФЗ)
    utm_source: str | None = Field(default=None, max_length=100)
    utm_medium: str | None = Field(default=None, max_length=100)
    utm_campaign: str | None = Field(default=None, max_length=100)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        phone = normalize_phone(value)
        if phone is None:
            raise ValueError("Телефон нужен российский мобильный, например +7 900 123-45-67")
        return phone

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value and not EMAIL.match(value):
            raise ValueError("Проверьте e-mail — в нём должна быть «@» и домен")
        return value or None

    @field_validator("consent")
    @classmethod
    def _consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Без согласия на обработку персональных данных заявку принять нельзя")
        return value

    def idempotency_key(self, day: date) -> str:
        """Та же заявка тем же днём (двойной клик, повторная отправка) — это одна заявка."""
        return hashlib.sha256(f"{self.phone}|{self.course}|{day.isoformat()}".encode()).hexdigest()[:32]


def lead_payload(form: TrialForm, contact_id: int | None, pipeline_id: int | None) -> dict[str, Any]:
    """Сделка с контактом для POST /api/v4/leads/complex: существующий контакт — по id, новый — с полями."""
    if contact_id is not None:
        contact: dict[str, Any] = {"id": contact_id}
    else:
        fields = [{"field_code": "PHONE", "values": [{"value": form.phone, "enum_code": "MOB"}]}]
        if form.email:
            fields.append({"field_code": "EMAIL", "values": [{"value": form.email, "enum_code": "WORK"}]})
        contact = {"first_name": form.name, "custom_fields_values": fields}
    lead: dict[str, Any] = {
        "name": f"Пробный урок: {COURSES[form.course]}",
        "_embedded": {"contacts": [contact], "tags": [{"name": "Сайт"}, {"name": COURSES[form.course]}]},
    }
    if pipeline_id is not None:
        lead["pipeline_id"] = pipeline_id
    return lead


def note_text(form: TrialForm) -> str:
    utm = ", ".join(
        f"{name}={value}"
        for name, value in (
            ("source", form.utm_source),
            ("medium", form.utm_medium),
            ("campaign", form.utm_campaign),
        )
        if value
    )
    return "\n".join(
        [
            f"Заявка с сайта: пробный урок, {COURSES[form.course].lower()}",
            f"Имя: {form.name}; телефон: {form.phone}; e-mail: {form.email or '—'}",
            f"Комментарий: {form.comment or '—'}",
            f"UTM: {utm or '—'}",
        ]
    )
