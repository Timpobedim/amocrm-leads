"""Командная строка.

amo-leads serve --port 8000     # приёмник заявок (FastAPI + uvicorn)
amo-leads authorize <код>       # первая авторизация OAuth: код из окна установки интеграции
amo-leads retry                 # дослать заявки из очереди один раз (например, из cron)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from amo_leads.client import AmoClient
from amo_leads.delivery import Delivery
from amo_leads.outbox import Outbox
from amo_leads.settings import Settings
from amo_leads.tokens import AmoAuthError, TokenManager


async def _authorize(settings: Settings, code: str) -> None:
    manager = settings.token_source()
    if not isinstance(manager, TokenManager):
        raise AmoAuthError("Задан долгосрочный токен — авторизация кодом не нужна")
    async with httpx.AsyncClient(base_url=settings.base_url) as http:
        await manager.exchange_code(http, code)


async def _retry(settings: Settings) -> int:
    zone = ZoneInfo(settings.timezone)
    async with AmoClient(settings.base_url, settings.token_source()) as amo:
        delivery = Delivery(
            amo,
            Outbox(settings.database),
            pipeline_id=settings.pipeline_id,
            now=lambda: datetime.now(zone).replace(tzinfo=None),
        )
        return await delivery.deliver_due()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amo-leads", description="Заявки с сайта в amoCRM")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="запустить приёмник заявок")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    authorize = commands.add_parser("authorize", help="обменять код авторизации на токены")
    authorize.add_argument("code")
    commands.add_parser("retry", help="дослать заявки из очереди")
    args = parser.parse_args(argv)
    settings = Settings()
    try:
        if args.command == "serve":
            import uvicorn

            from amo_leads.app import create_app

            # один процесс: refresh-токен одноразовый, и два воркера сожгли бы его одновременным обновлением
            uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1)
        elif args.command == "authorize":
            asyncio.run(_authorize(settings, args.code))
            print(f"Токены сохранены: {settings.tokens_file}")
        else:
            print(f"Отправлено в amoCRM: {asyncio.run(_retry(settings))}")
    except AmoAuthError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0
