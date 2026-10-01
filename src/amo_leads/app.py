"""Приёмник заявок с сайта: POST /forms/trial → очередь → amoCRM.

    amo-leads serve --port 8000

Ответы сайту: 201 — заявка уже в amoCRM; 202 — принята и будет отправлена позже (amoCRM недоступен
или отказал — посетителю сайта знать это незачем); 200 — повтор уже принятой заявки; 422 — ошибки
в полях, по-русски и по каждому полю.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from amo_leads.client import AmoClient
from amo_leads.delivery import Delivery
from amo_leads.forms import TrialForm
from amo_leads.outbox import Outbox
from amo_leads.settings import Settings

logger = logging.getLogger(__name__)

QUEUED = "Заявка принята — менеджер свяжется с вами в течение рабочего дня"
MESSAGES = {  # встроенные ошибки pydantic — по-русски
    "missing": "Обязательное поле",
    "string_too_short": "Заполните поле",
    "string_too_long": "Слишком длинное значение",
    "string_type": "Нужен текст",
    "literal_error": "Выберите значение из списка",
    "bool_parsing": "Нужно «да» или «нет»",
    "bool_type": "Нужно «да» или «нет»",
}


def _field_errors(exc: RequestValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"] if part != "body") or "form"
        text = str(error["msg"])
        message = (
            text.removeprefix("Value error, ")
            if error["type"] == "value_error"
            else MESSAGES.get(error["type"], text)
        )
        errors.setdefault(field, message)
    return errors


async def _retry_forever(delivery: Delivery, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            sent = await delivery.deliver_due()
            if sent:
                logger.info("Досланы заявки из очереди: %s", sent)
        except Exception:  # граница: фоновая задача не должна умирать от одной ошибки
            logger.exception("Фоновая отправка заявок не удалась — повторим через %s с", every)


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    start_worker: bool = True,
) -> FastAPI:
    zone = ZoneInfo(settings.timezone)
    clock = now or (lambda: datetime.now(zone).replace(tzinfo=None))
    outbox = Outbox(settings.database)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AmoClient(
            settings.base_url, settings.token_source(), transport=transport, sleep=sleep
        ) as amo:
            delivery = Delivery(amo, outbox, pipeline_id=settings.pipeline_id, now=clock)
            app.state.delivery = delivery
            worker = (
                asyncio.create_task(_retry_forever(delivery, settings.retry_every)) if start_worker else None
            )
            try:
                yield
            finally:
                if worker is not None:
                    worker.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker

    app = FastAPI(title="Заявки с сайта → amoCRM", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"errors": _field_errors(exc)})

    @app.post("/forms/trial")
    async def trial(
        form: TrialForm,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        delivery: Delivery = request.app.state.delivery
        key = idempotency_key or form.idempotency_key(clock().date())
        submission, created = outbox.add(key, form.model_dump_json(), clock())
        if created:  # повтор ждущей заявки не отправляем параллельно — её дошлёт фоновая задача
            submission = await delivery.deliver(submission)
        if submission.status == "sent":
            response.status_code = 201 if created else 200
            return {"status": "sent", "lead_id": submission.lead_id, "duplicate": not created}
        response.status_code = 202 if created else 200
        return {"status": "queued", "message": QUEUED, "duplicate": not created}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "queue": outbox.counts()}

    return app
