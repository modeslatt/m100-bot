"""Ручная проверка BitrixClient на настоящем портале.

    python scripts/try_bitrix.py check   # только чтение: вебхук, смарт-процесс, поля
    python scripts/try_bitrix.py send    # + создать тестовый элемент отчёта и обновить его

Настройки — из .env (BITRIX_WEBHOOK_URL, BITRIX_ENTITY_TYPE_ID).

send ИЗМЕНЯЕТ Битрикс: создаёт элемент «Отчет от <дата время> (проверка бота)» (мастер
«Тест бота», Telegram ID 0) и сразу обновляет его — так бот применяет исправления.
Каждый запуск создаёт новый элемент; удалить их можно вручную в смарт-процессе.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from m100bot.bitrix.client import FIELD_TITLES, BitrixClient, BitrixError, find_fields

ROOT = Path(__file__).resolve().parent.parent
TEST_MASTER_ID = 0
TEST_MASTER = "Тест бота"


class BitrixSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    bitrix_webhook_url: str = ""
    bitrix_entity_type_id: str = ""


def _label(meta: dict[str, Any]) -> str:
    for key in ("listLabel", "formLabel", "editFormLabel", "title"):
        v = meta.get(key)
        if isinstance(v, dict):
            v = v.get("ru") or next(iter(v.values()), "")
        if isinstance(v, str) and v.strip():
            return v
    return "?"


async def check(client: BitrixClient, out: Callable[[str], None]) -> bool:
    types = (await client.call("crm.type.list")).get("types", [])
    out("✅ Вебхук работает, права crm есть")
    out("Смарт-процессы на портале:")
    for t in types:
        mark = "→" if t.get("entityTypeId") == client.entity_type_id else " "
        out(f"  {mark} {t.get('entityTypeId')}: {t.get('title')}")
    if not any(t.get("entityTypeId") == client.entity_type_id for t in types):
        out(f"❌ Смарт-процесса с ID {client.entity_type_id} нет — проверьте BITRIX_ENTITY_TYPE_ID")
        return False

    fields = (await client.call("crm.item.fields", {"entityTypeId": client.entity_type_id}))[
        "fields"
    ]
    custom = {code: meta for code, meta in fields.items() if code.startswith("ufCrm")}
    out("Пользовательские поля:")
    for code, meta in custom.items():
        out(f"    {code}: «{_label(meta)}» ({meta.get('type')})")
    if not custom:
        out("    (нет)")
    found, missing = find_fields(fields)
    for title in FIELD_TITLES.values():
        out(f"  {'❌' if title in missing else '✅'} {title}")
    if found is None:
        out("Создайте недостающие поля с такими названиями (регистр и «ё» не важны).")
        return False
    out("✅ Все поля на месте — бот готов отправлять отчёты")
    return True


async def send(client: BitrixClient, d: date, out: Callable[[str], None]) -> bool:
    title = f"Отчет от {datetime.now():%d.%m.%Y %H:%M} (проверка бота)"
    report = {
        "title": title,
        "work_date": d,
        "master_id": TEST_MASTER_ID,
        "master": TEST_MASTER,
        "sheet_url": "https://example.com",
    }
    lines = ["Кабель силовой 3х2,5 — 1 м"]
    first = await client.save_report(None, lines=lines, **report)
    out(f"✅ Создан элемент «{title}»: id {first}")
    lines = ["Кабель силовой 3х2,5 — 2 м (исправлено, было 1 м)"]
    second = await client.save_report(first, lines=lines, **report)
    if second != first:
        out(f"❌ Обновление создало новый элемент (id {second}) вместо изменения {first}")
        return False
    out("✅ Тот же элемент обновлён — так бот применяет исправления")
    out(
        f"Откройте в Битриксе: CRM → смарт-процесс → элемент {first}: "
        "в «Объёмы» — «2 м (исправлено…)»"
    )
    return True


async def run(command: str, client: BitrixClient, d: date) -> int:
    try:
        ok = await check(client, print)
        if ok and command == "send":
            ok = await send(client, d, print)
        return 0 if ok else 1
    finally:
        await client.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("command", choices=["check", "send"])
    ap.add_argument(
        "--date", type=date.fromisoformat, default=None, help="YYYY-MM-DD, по умолчанию сегодня"
    )
    args = ap.parse_args()

    s = BitrixSettings()
    if not s.bitrix_webhook_url:
        print("BITRIX_WEBHOOK_URL в .env пуст", file=sys.stderr)
        return 2
    if not s.bitrix_entity_type_id.strip().isdigit():
        print("BITRIX_ENTITY_TYPE_ID в .env пуст или не число. ID смарт-процесса — число из "
              "адреса /crm/type/<число>/", file=sys.stderr)  # fmt: skip
        client = BitrixClient(s.bitrix_webhook_url, 0)
        # всё равно покажем список смарт-процессов, чтобы было из чего выбрать
        try:
            asyncio.run(run("check", client, date.today()))
        except BitrixError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
        return 2

    client = BitrixClient(s.bitrix_webhook_url, int(s.bitrix_entity_type_id))
    try:
        return asyncio.run(run(args.command, client, args.date or date.today()))
    except BitrixError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
