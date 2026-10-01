"""Отправка заявки в amoCRM: контакт по телефону (без дублей), сделка с контактом, примечание.

Ошибки делятся на временные и окончательные. Сеть, 429, 5xx и проблемы с токеном — временные:
заявка остаётся в очереди и уйдёт позже (после повторной авторизации — тоже). Отказ amoCRM
по данным (4xx) — окончательный: заявка помечается `failed`, её разбирает человек.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from amo_leads.client import AmoClient, AmoError
from amo_leads.forms import TrialForm, lead_payload, note_text
from amo_leads.outbox import Outbox, Submission
from amo_leads.tokens import AmoAuthError

logger = logging.getLogger(__name__)


class Delivery:
    def __init__(
        self, amo: AmoClient, outbox: Outbox, *, pipeline_id: int | None, now: Callable[[], datetime]
    ) -> None:
        self.amo = amo
        self.outbox = outbox
        self.pipeline_id = pipeline_id
        self.now = now

    async def deliver(self, submission: Submission) -> Submission:
        form = TrialForm.model_validate_json(submission.payload)
        try:
            if submission.lead_id is None:
                contact = await self.amo.find_contact_by_phone(form.phone)
                created = await self.amo.add_lead_complex(
                    lead_payload(form, contact["id"] if contact else None, self.pipeline_id)
                )
                # номер сделки — сразу в базу: если упадёт примечание, повтор не создаст вторую сделку
                self.outbox.set_lead(submission.id, int(created["id"]), int(created["contact_id"]))
                submission = self.outbox.get(submission.id)
            await self.amo.add_note(int(submission.lead_id or 0), note_text(form))
        except AmoAuthError as exc:
            logger.error("Заявка %s ждёт: %s", submission.id, exc)
            self.outbox.mark_retry(submission.id, str(exc), self.now())
        except AmoError as exc:
            if exc.status in (0, 429) or exc.status >= 500:
                logger.warning("amoCRM недоступен, заявка %s уйдёт позже: %s", submission.id, exc)
                self.outbox.mark_retry(submission.id, str(exc), self.now())
            else:
                logger.error("amoCRM отказал в заявке %s: %s", submission.id, exc)
                self.outbox.mark_failed(submission.id, str(exc))
        else:
            self.outbox.mark_sent(submission.id)
        return self.outbox.get(submission.id)

    async def deliver_due(self) -> int:
        """Дослать всё, чему пришёл срок; возвращает, сколько ушло в amoCRM."""
        sent = 0
        for submission in self.outbox.due(self.now()):
            sent += (await self.deliver(submission)).status == "sent"
        return sent
