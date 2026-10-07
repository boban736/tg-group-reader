#!/usr/bin/env python3
"""Шлёт ежедневный отчёт из vault в Telegram («Избранное» или NOTIFY_TARGET) от твоего аккаунта.

Отчёт пишет плановая задача Claude в 11:25: <VAULT>/Log/Отчёты/<YYYY-MM-DD> отчёт.md.
launchd запускает этот скрипт несколько раз после 11:30; отправка идёт один раз в день
(отметка в report_sent.json). Сессия и ключи — те же, что у reader.py (.env, tg.session).

  .venv/bin/python send_report.py              # сегодняшний отчёт, если ещё не отправлен
  .venv/bin/python send_report.py --force      # отправить ещё раз
  .venv/bin/python send_report.py --dry-run    # показать текст, ничего не слать
"""
import argparse, asyncio, datetime as dt, json, os, sqlite3, sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SESSION_FILE = ROOT / "tg.session"
SENT_FILE = ROOT / "report_sent.json"
REPORTS_SUBDIR = os.getenv("VAULT_REPORTS_SUBDIR", "Log/Отчёты")
TARGET = os.getenv("REPORT_TARGET") or os.getenv("NOTIFY_TARGET", "me")


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} send_report: {msg}", flush=True)


def load_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def chunks(text: str, limit: int = 3900) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


async def send(parts: list[str]) -> None:
    api_id, api_hash = int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]
    for attempt in range(4):  # reader.py может держать tg.session прямо сейчас
        try:
            async with TelegramClient(str(SESSION_FILE), api_id, api_hash) as client:
                for p in parts:
                    await client.send_message(TARGET, p, link_preview=False)
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 3:
                raise
            log("tg.session занят, жду 30 с")
            await asyncio.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD, по умолчанию сегодня")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    day = a.date or dt.date.today().isoformat()
    vault = Path(os.environ["VAULT_DIR"]).expanduser()
    report = vault / REPORTS_SUBDIR / f"{day} отчёт.md"
    sent = json.loads(SENT_FILE.read_text()) if SENT_FILE.exists() else {}

    if sent.get("last_date") == day and not (a.force or a.dry_run):
        return  # уже отправлен сегодня — тихо выходим
    if not report.exists():
        log(f"нет отчёта {report.name} (задача Claude ещё не отработала или Mac спал)")
        return
    parts = chunks(load_text(report))
    if a.dry_run:
        print("\n----- next message -----\n".join(parts))
        return
    asyncio.run(send(parts))
    SENT_FILE.write_text(json.dumps({"last_date": day, "sent_at": dt.datetime.now().isoformat(timespec="seconds")}))
    log(f"отправлен {report.name} → {TARGET}, сообщений: {len(parts)}")


if __name__ == "__main__":
    sys.exit(main())
