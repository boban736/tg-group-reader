"""Hourly Telegram group digest: read new messages, let an LLM pick what matters,
append it to a daily note in an Obsidian vault and ping Saved Messages about urgent items."""

import argparse
import asyncio
import base64
import datetime as dt
import io
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import openai
from dotenv import load_dotenv
from openai import OpenAI
from telethon import TelegramClient, functions
from telethon.tl.types import (
    ChannelParticipantsAdmins,
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
)

import vault_out as vo

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

log = logging.getLogger("reader")


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        sys.exit(f"Missing required setting {name} in .env")
    return value


API_ID = int(env("TG_API_ID"))
API_HASH = env("TG_API_HASH")
GROUP = env("TG_GROUP")  # @username, numeric id (-100...) or exact chat title
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or None
OPENAI_REASONING = os.getenv("OPENAI_REASONING", "low")  # none|low|medium|high, "" = model default

VAULT_DIR = Path(env("VAULT_DIR")).expanduser()
LAYOUT = vo.Layout(
    vault=VAULT_DIR,
    digest_dir=VAULT_DIR / os.getenv("VAULT_DIGEST_SUBDIR", "Log/Telegram"),
    log_dir=VAULT_DIR / os.getenv("VAULT_LOG_SUBDIR", "Log"),
    courses_dir=VAULT_DIR / os.getenv("VAULT_COURSES_SUBDIR", "Courses"),
    inbox_attach_dir=VAULT_DIR / os.getenv("VAULT_INBOX_ATTACH_SUBDIR", "Inbox/Telegram"),
    deadlines_note=VAULT_DIR / os.getenv("VAULT_DEADLINES_NOTE", "Дедлайны.md"),
    course_attach_sub=os.getenv("COURSE_ATTACH_SUBDIR", "Materials/Telegram"),
    course_section=os.getenv("COURSE_SECTION", "## Из Telegram"),
    tags=[t.strip() for t in os.getenv("NOTE_TAGS", "telegram").split(",") if t.strip()],
)
WRITE_LOG = os.getenv("WRITE_LOG", "1") == "1"
WRITE_DEADLINES = os.getenv("WRITE_DEADLINES", "1") == "1"
WRITE_COURSE_SECTIONS = os.getenv("WRITE_COURSE_SECTIONS", "1") == "1"
SAVE_FILES_FOR = set(os.getenv("SAVE_FILES_FOR", "deadline,material,schedule,announcement").split(","))
STATE_FILE = ROOT / "state.json"
SESSION_FILE = ROOT / "tg.session"
PROMPT_FILE = ROOT / "prompts" / "system.md"

FIRST_RUN_HOURS = int(os.getenv("FIRST_RUN_HOURS", "24"))
MAX_MESSAGES = int(os.getenv("MAX_MESSAGES", "500"))
BATCH_MESSAGES = int(os.getenv("BATCH_MESSAGES", "150"))
BATCH_IMAGES = int(os.getenv("BATCH_IMAGES", "10"))
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "20"))
DOC_TEXT_CHARS = int(os.getenv("DOC_TEXT_CHARS", "12000"))
IMAGE_DETAIL = os.getenv("IMAGE_DETAIL", "high")  # low | high | auto
NOTIFY_URGENT = os.getenv("NOTIFY_URGENT", "1") == "1"
NOTIFY_TARGET = os.getenv("NOTIFY_TARGET", "me")  # "me" = Saved Messages

IMAGE_EXTS = vo.IMAGE_EXTS
TEXT_EXTS = {".txt", ".md", ".csv"}

ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "subject", "title", "summary", "when", "due", "urgent", "source_ids"],
        "properties": {
            "category": {"type": "string", "enum": list(vo.CATEGORY_TITLES)},
            "subject": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "summary": {"type": "string"},
            "when": {"type": ["string", "null"]},
            "due": {"type": ["string", "null"]},
            "urgent": {"type": "boolean"},
            "source_ids": {"type": "array", "items": {"type": "integer"}},
        },
    }}},
}


@dataclass
class Msg:
    id: int
    date: dt.datetime
    sender: str
    is_admin: bool
    topic: str | None
    text: str
    reply_to: str | None = None
    attachments: list[str] = field(default_factory=list)  # human-readable notes / extracted text
    images: list[str] = field(default_factory=list)  # data: URIs
    files: list[tuple[str, bytes]] = field(default_factory=list)  # (name, data) to save into vault if useful
    link: str = ""


# ---------- state ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ---------- telegram ----------

async def resolve_chat(client: TelegramClient):
    try:
        ident = int(GROUP) if GROUP.lstrip("-").isdigit() else GROUP
        return await client.get_entity(ident)
    except ValueError:
        async for d in client.iter_dialogs():
            if d.name == GROUP:
                return d.entity
    sys.exit(f"Chat {GROUP!r} not found. Use @username, -100... id or the exact title.")


async def fetch_admin_ids(client, chat) -> set[int]:
    try:
        return {u.id async for u in client.iter_participants(chat, filter=ChannelParticipantsAdmins)}
    except Exception as e:  # listing admins can be forbidden
        log.warning("cannot list admins: %s", e)
        return set()


async def fetch_topics(client, chat) -> dict[int, str]:
    if not getattr(chat, "forum", False):
        return {}
    topics: dict[int, str] = {1: "General"}
    # The request moved from channels.* to messages.* in newer layers.
    for make in (
        lambda: functions.messages.GetForumTopicsRequest(
            peer=chat, offset_date=None, offset_id=0, offset_topic=0, limit=100),
        lambda: functions.channels.GetForumTopicsRequest(
            channel=chat, offset_date=None, offset_id=0, offset_topic=0, limit=100),
    ):
        try:
            res = await client(make())
            topics.update({t.id: t.title for t in res.topics if hasattr(t, "title")})
            return topics
        except AttributeError:
            continue
        except Exception as e:
            log.warning("cannot fetch forum topics: %s", e)
            return topics
    return topics


def topic_id(m) -> int | None:
    r = m.reply_to
    if r is None:
        return 1
    if getattr(r, "forum_topic", False):
        return r.reply_to_top_id or r.reply_to_msg_id
    return 1


def sender_name(m) -> str:
    s = m.sender
    if s is None:
        return m.post_author or "Аноним"
    if getattr(s, "title", None):
        return m.post_author or s.title
    return " ".join(x for x in (s.first_name, s.last_name) if x) or s.username or str(s.id)


def message_link(chat, msg_id: int) -> str:
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{msg_id}"
    return f"https://t.me/c/{chat.id}/{msg_id}"


def doc_filename(doc) -> str:
    for a in doc.attributes:
        if isinstance(a, DocumentAttributeFilename):
            return a.file_name
    return ""


def extract_doc_text(name: str, data: bytes) -> str | None:
    ext = Path(name).suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        if ext == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
        if ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(io.BytesIO(data))
            return "\n".join(
                sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
        if ext in TEXT_EXTS:
            return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("cannot extract text from %s: %s", name, e)
    return None


def to_data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def process_media(client, m, out: Msg) -> None:
    media = m.media
    if isinstance(media, MessageMediaPhoto):
        data = await client.download_media(m, file=bytes)
        if data:
            out.images.append(to_data_uri(data, "image/jpeg"))
            out.files.append(("photo.jpg", data))
            out.attachments.append("[фото — приложено изображением]")
        return
    if not isinstance(media, MessageMediaDocument) or media.document is None:
        if media is not None:
            out.attachments.append(f"[вложение: {type(media).__name__}]")
        return

    doc = media.document
    name = doc_filename(doc) or "файл"
    size_mb = doc.size / 1_048_576
    mime = doc.mime_type or ""
    ext = Path(name).suffix.lower()
    header = f"[файл: {name}, {size_mb:.1f} MB]"

    if size_mb > MAX_FILE_MB:
        out.attachments.append(header + " (слишком большой, не скачан)")
        return
    if mime.startswith("image/") or ext in IMAGE_EXTS:
        data = await client.download_media(m, file=bytes)
        out.images.append(to_data_uri(data, mime or "image/jpeg"))
        out.files.append((name, data))
        out.attachments.append(header + " — приложено изображением")
        return
    if ext in {".pdf", ".docx", ".pptx", *TEXT_EXTS}:
        data = await client.download_media(m, file=bytes)
        out.files.append((name, data))
        text = extract_doc_text(name, data)
        if text and text.strip():
            snippet = text.strip()[:DOC_TEXT_CHARS]
            out.attachments.append(f"{header}\n<<<содержимое файла\n{snippet}\nсодержимое файла>>>")
            return
    out.files.append((name, await client.download_media(m, file=bytes)))
    out.attachments.append(header)


async def collect(client, chat, state: dict, since_hours: float | None = None) -> tuple[list[Msg], int]:
    key = str(chat.id)
    last_id = 0 if since_hours else state.get(key, {}).get("last_id", 0)
    admins = await fetch_admin_ids(client, chat)
    topics = await fetch_topics(client, chat)

    if last_id:
        raw = [m async for m in client.iter_messages(chat, min_id=last_id, limit=MAX_MESSAGES)]
    else:
        hours = since_hours or FIRST_RUN_HOURS
        limit = MAX_MESSAGES if not since_hours else max(MAX_MESSAGES, 5000)
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
        raw = []
        async for m in client.iter_messages(chat, limit=limit):
            if m.date < since:
                break
            raw.append(m)
    raw.reverse()
    if not raw:
        return [], last_id

    by_id = {m.id: m for m in raw}
    missing = {
        m.reply_to.reply_to_msg_id for m in raw
        if m.reply_to and m.reply_to.reply_to_msg_id
        and m.reply_to.reply_to_msg_id != topic_id(m)
        and m.reply_to.reply_to_msg_id not in by_id
    }
    if missing:
        for r in await client.get_messages(chat, ids=list(missing)):
            if r:
                by_id.setdefault(r.id, r)

    msgs: list[Msg] = []
    for m in raw:
        if m.action is not None:  # service messages: joins, pins, topic edits
            continue
        tid = topic_id(m)
        out = Msg(
            id=m.id,
            date=m.date.astimezone(),
            sender=sender_name(m),
            is_admin=(m.sender_id in admins) or bool(m.post_author),
            topic=topics.get(tid) if topics else None,
            text=m.message or "",
            link=message_link(chat, m.id),
        )
        rid = m.reply_to.reply_to_msg_id if m.reply_to else None
        if rid and rid != tid and rid in by_id:
            parent = by_id[rid]
            ptext = (parent.message or ("[медиа]" if parent.media else "")).replace("\n", " ")
            out.reply_to = f"{sender_name(parent)}: {ptext[:200]}"
        try:
            await process_media(client, m, out)
        except Exception as e:
            log.warning("media of msg %s failed: %s", m.id, e)
            out.attachments.append("[вложение не удалось скачать]")
        msgs.append(out)
    return msgs, raw[-1].id


async def pinned_text(client, chat) -> str | None:
    try:
        full = await client(functions.channels.GetFullChannelRequest(chat))
        pid = full.full_chat.pinned_msg_id
        if pid:
            m = await client.get_messages(chat, ids=pid)
            return m.message if m else None
    except Exception:
        pass
    return None


# ---------- LLM ----------

def render_msg(m: Msg) -> str:
    head = f"#{m.id} {m.date:%d.%m %H:%M} | {m.sender}{' [admin]' if m.is_admin else ''}"
    if m.topic:
        head += f" | тема: {m.topic}"
    lines = [head]
    if m.reply_to:
        lines.append(f"  ↪ в ответ на {m.reply_to}")
    if m.text:
        lines.append(m.text)
    lines.extend(m.attachments)
    return "\n".join(lines)


def batches(msgs: list[Msg]):
    cur: list[Msg] = []
    imgs = 0
    for m in msgs:
        if cur and (len(cur) >= BATCH_MESSAGES or imgs + len(m.images) > BATCH_IMAGES):
            yield cur
            cur, imgs = [], 0
        cur.append(m)
        imgs += len(m.images)
    if cur:
        yield cur


LLM_FORMATS = [
    {"type": "json_schema", "json_schema": {"name": "digest", "strict": True, "schema": ITEM_SCHEMA}},
    {"type": "json_object"},
    None,
]


def chat_completion(llm: OpenAI, messages: list[dict]) -> str:
    """Chat Completions with Structured Outputs; falls back to json_object / plain text and
    drops reasoning_effort if the model (or a proxy) rejects them."""
    reasoning = OPENAI_REASONING
    for fmt in LLM_FORMATS:
        while True:
            kwargs: dict = {"model": OPENAI_MODEL, "messages": messages}
            if reasoning:
                kwargs["reasoning_effort"] = reasoning
            if fmt:
                kwargs["response_format"] = fmt
            try:
                resp = llm.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or "{}"
            except openai.BadRequestError as e:
                msg = str(e)
                if reasoning and "reasoning" in msg:
                    log.warning("model rejects reasoning_effort, retrying without it")
                    reasoning = ""
                    continue
                if fmt and ("response_format" in msg or "json" in msg.lower()):
                    log.warning("response_format %s rejected: %s", fmt["type"], msg[:200])
                    break
                raise
    raise RuntimeError("LLM rejected every response format")


def parse_items(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        items = json.loads(raw).get("items", [])
    except json.JSONDecodeError:
        log.error("LLM returned invalid JSON: %s", raw[:500])
        raise
    return [i for i in items if isinstance(i, dict) and i.get("summary")]


def ask_llm(llm: OpenAI, system: str, chat_title: str, pinned: str | None, batch: list[Msg],
            earlier: list[dict]) -> list[dict]:
    now = dt.datetime.now().astimezone()
    intro = [f"Сейчас: {now:%A %d.%m.%Y %H:%M}.", f"Группа: {chat_title}."]
    if pinned:
        intro.append(f"Закреплённое сообщение: {pinned}")
    tail = vo.recent_context(LAYOUT)
    if tail:
        intro.append("Уже записано в дайджест ранее (не повторяй это, если нет новых деталей):\n" + tail)
    if earlier:
        intro.append("Уже выбрано из предыдущих сообщений этого прогона (не повторяй):\n" + "\n".join(
            f"- [{i.get('category')}] {i.get('subject') or ''}: {i.get('summary')}" for i in earlier))
    intro.append("Новые сообщения:\n\n" + "\n\n".join(render_msg(m) for m in batch))

    content: list[dict] = [{"type": "text", "text": "\n\n".join(intro)}]
    for m in batch:
        for uri in m.images:
            content.append({"type": "text", "text": f"Изображение из сообщения #{m.id}:"})
            content.append({"type": "image_url", "image_url": {"url": uri, "detail": IMAGE_DETAIL}})

    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    return parse_items(chat_completion(llm, messages))


def build_system_prompt(courses: list[vo.Course]) -> str:
    return PROMPT_FILE.read_text(encoding="utf-8") + "\n\n" + vo.courses_prompt(courses) + "\n"


# ---------- output ----------

def write_vault(items: list[dict], msgs: dict[int, Msg], chat_title: str, dry_run: bool) -> vo.VaultWriter:
    w = vo.VaultWriter(VAULT_DIR, dry_run=dry_run)
    run_at = dt.datetime.now().astimezone()
    files = vo.save_files(w, LAYOUT, items, msgs, SAVE_FILES_FOR)
    per_day = vo.write_digests(w, LAYOUT, items, msgs, files, chat_title, run_at)
    if WRITE_LOG:
        vo.write_log_line(w, LAYOUT, items, per_day, run_at)
    new_deadlines = vo.write_deadlines(w, LAYOUT, items, msgs, run_at.date()) if WRITE_DEADLINES else None
    if WRITE_COURSE_SECTIONS:
        vo.write_course_sections(w, LAYOUT, items, msgs, files, new_deadlines)
    w.flush()
    return w


async def notify(client, items: list[dict], chat_title: str, msgs: dict[int, Msg]) -> None:
    today = dt.date.today()
    urgent = [i for i in items if i.get("urgent") and not ((d := vo.parse_due(i)) and d < today)]
    if not (NOTIFY_URGENT and urgent):
        return
    lines = [f"🔴 {chat_title}:"]
    for i in urgent:
        when = f" ({i['when']})" if i.get("when") else ""
        ref = next((msgs[mid].link for mid in i.get("source_ids", []) if mid in msgs), "")
        lines.append(f"• {i.get('summary', '').strip()}{when} {ref}".rstrip())
    await client.send_message(NOTIFY_TARGET, "\n".join(lines), link_preview=False)


# ---------- main ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--login", action="store_true", help="only log in to Telegram and check the group")
    p.add_argument("--days", type=float, help="re-read the last N days (ignores state; for backfill)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the vault diff instead of writing; no state update, no notifications")
    p.add_argument("--no-notify", action="store_true", help="do not send urgent items to Saved Messages")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "reader.log")],
    )
    if not args.login and not (OPENAI_MODEL and OPENAI_API_KEY):
        sys.exit("Missing OPENAI_MODEL / OPENAI_API_KEY in .env")
    state = load_state()

    async with TelegramClient(str(SESSION_FILE), API_ID, API_HASH) as client:
        chat = await resolve_chat(client)
        title = getattr(chat, "title", GROUP)
        if args.login:
            print(f"OK: logged in, group {title!r} (id {chat.id}, forum={getattr(chat, 'forum', False)})")
            return

        courses = vo.load_courses(LAYOUT.courses_dir)
        system = build_system_prompt(courses)
        llm = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        since_hours = args.days * 24 if args.days else None
        msgs, new_last = await collect(client, chat, state, since_hours)
        if not msgs:
            log.info("no new messages")
            if new_last and not args.dry_run:
                state[str(chat.id)] = {"last_id": max(new_last, state.get(str(chat.id), {}).get("last_id", 0))}
                save_state(state)
            return

        log.info("%d new messages, %d images", len(msgs), sum(len(m.images) for m in msgs))
        pinned = await pinned_text(client, chat)
        by_id = {m.id: m for m in msgs}
        items: list[dict] = []
        for b in batches(msgs):
            items.extend(ask_llm(llm, system, title, pinned, b, items))
        for i in items:
            i["_course"] = vo.match_course(i.get("subject"), courses)

        if items:
            w = write_vault(items, by_id, title, args.dry_run)
            if args.dry_run:
                print(w.diff() or "(no changes)")
            elif not args.no_notify:
                await notify(client, items, title, by_id)
        log.info("%d items %s", len(items), "found (dry run)" if args.dry_run else "written")
        if args.dry_run:
            return
        # Advance only after a successful run so a failure is retried next hour.
        prev = state.get(str(chat.id), {}).get("last_id", 0)
        state[str(chat.id)] = {"last_id": max(new_last, prev)}
        save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
