"""Everything that touches the Obsidian vault: subjects, digest notes, Log lines,
the deadlines note, course-hub sections and saved attachments.

Rules: never delete or rewrite user content. New files are created; existing notes only
get text appended at the end or inside our own section. All writes are buffered in
VaultWriter and applied at the end of a run (or printed as a diff with --dry-run)."""

from __future__ import annotations

import datetime as dt
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

CATEGORY_TITLES = {
    "deadline": "Дедлайны, тесты, экзамены",
    "schedule": "Замены и изменения пар",
    "material": "Файлы и материалы",
    "announcement": "Объявления",
    "other": "Прочее полезное",
}


# ---------- buffered writer ----------

class VaultWriter:
    def __init__(self, root: Path, dry_run: bool = False):
        self.root = root
        self.dry_run = dry_run
        self._orig: dict[Path, str | None] = {}
        self._text: dict[Path, str] = {}
        self._bytes: dict[Path, bytes] = {}

    def read(self, path: Path) -> str:
        if path in self._text:
            return self._text[path]
        orig = path.read_text(encoding="utf-8") if path.exists() else None
        self._orig[path] = orig
        self._text[path] = orig or ""
        return self._text[path]

    def set(self, path: Path, text: str) -> None:
        self.read(path)
        self._text[path] = text

    def append(self, path: Path, text: str) -> None:
        cur = self.read(path)
        if cur and not cur.endswith("\n"):
            cur += "\n"
        self._text[path] = cur + text

    def write_bytes(self, path: Path, data: bytes) -> None:
        self._bytes[path] = data

    def changes(self) -> list[Path]:
        return [p for p, t in self._text.items() if t != (self._orig.get(p) or "")]

    def diff(self) -> str:
        out = []
        for p in self.changes():
            rel = p.relative_to(self.root).as_posix()
            old = self._orig.get(p)
            out.extend(difflib.unified_diff(
                (old or "").splitlines(keepends=True), self._text[p].splitlines(keepends=True),
                fromfile="/dev/null" if old is None else f"a/{rel}", tofile=f"b/{rel}"))
        for p, data in self._bytes.items():
            out.append(f"+++ new file {p.relative_to(self.root).as_posix()} ({len(data)} bytes)\n")
        return "".join(out)

    def flush(self) -> list[Path]:
        if self.dry_run:
            return []
        written = []
        for p, data in self._bytes.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_bytes(data)
                written.append(p)
        for p in self.changes():
            old = self._orig.get(p)
            # Guard against a concurrent edit (Obsidian/iCloud) since we read the file.
            now = p.read_text(encoding="utf-8") if p.exists() else None
            text = self._text[p]
            if now != old:
                if old is not None and now is not None and text.startswith(old):
                    text = now + text[len(old):]  # pure append: re-apply on top of new content
                else:
                    raise RuntimeError(f"{p} changed during the run, not overwriting")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tgtmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(p)
            written.append(p)
        return written


# ---------- subjects ----------

def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm


@dataclass
class Course:
    name: str
    short: str = ""
    teachers: list[str] = field(default_factory=list)


def load_courses(courses_dir: Path) -> list[Course]:
    out = []
    for cdir in sorted(courses_dir.iterdir()) if courses_dir.exists() else []:
        hub = cdir / f"{cdir.name}.md"
        if not cdir.is_dir() or not hub.exists():
            continue
        text = hub.read_text(encoding="utf-8")
        fm = _frontmatter(text)
        if fm.get("kind") not in (None, "course"):
            continue
        teachers = [fm["teacher"]] if fm.get("teacher") else []
        sec = re.search(r"## Препод и контакты\n(.*?)(?:\n## |\Z)", text, re.S)
        for line in (sec.group(1).splitlines() if sec else []):
            m = re.match(r"-\s*([^:]+):\s*(.+)", line.strip())
            if m and m.group(2).strip() not in teachers:
                teachers.append(f"{m.group(2).strip()} ({m.group(1).strip().lower()})")
        out.append(Course(name=fm.get("course") or cdir.name, short=fm.get("short", ""), teachers=teachers))
    return out


def courses_prompt(courses: list[Course]) -> str:
    lines = ["Предметы группы (в поле subject пиши ТОЧНО одно из этих названий, символ в символ, или null):"]
    for c in courses:
        extra = []
        if c.short:
            extra.append(f"в расписании «{c.short}»")
        if c.teachers:
            extra.append("преподаватели: " + ", ".join(c.teachers))
        lines.append(f"- {c.name}" + (f" — {'; '.join(extra)}" if extra else ""))
    return "\n".join(lines)


def match_course(subject: str | None, courses: list[Course]) -> str | None:
    if not subject:
        return None
    s = subject.strip().casefold()
    for c in courses:
        if s in (c.name.casefold(), c.short.casefold()):
            return c.name
    best = max(courses, key=lambda c: difflib.SequenceMatcher(None, s, c.name.casefold()).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, s, best.name.casefold()).ratio() >= 0.85:
        return best.name
    return None


# ---------- layout ----------

@dataclass
class Layout:
    vault: Path
    digest_dir: Path
    log_dir: Path
    courses_dir: Path
    inbox_attach_dir: Path
    deadlines_note: Path
    course_attach_sub: str = "Materials/Telegram"
    course_section: str = "## Из Telegram"
    tags: list[str] = field(default_factory=lambda: ["telegram"])

    def digest_path(self, day: dt.date) -> Path:
        return self.digest_dir / f"TG {day:%Y-%m-%d}.md"

    def attach_dir(self, course: str | None) -> Path:
        if course:
            return self.courses_dir / course / self.course_attach_sub
        return self.inbox_attach_dir


def digest_link(day: dt.date) -> str:
    return f"[[TG {day:%Y-%m-%d}]]"


# ---------- item helpers ----------

def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "_", name).strip() or "file"


def item_day(item: dict, msgs: dict) -> dt.date:
    dates = [msgs[i].date.date() for i in item.get("source_ids", []) if i in msgs]
    return min(dates) if dates else dt.date.today()


def parse_due(item: dict) -> dt.date | None:
    try:
        return dt.date.fromisoformat((item.get("due") or "")[:10])
    except ValueError:
        return None


def subj_prefix(course: str | None, raw: str | None) -> str:
    if course:
        return f"[[{course}]]: "
    return f"{raw}: " if raw else ""


# ---------- writers ----------

def save_files(w: VaultWriter, lay: Layout, items: list[dict], msgs: dict, save_for: set[str]) -> dict[int, list[str]]:
    """Queue attachments of messages that useful items refer to; return msg id -> file names."""
    saved: dict[int, list[str]] = {}
    for it in items:
        if it.get("category", "other") not in save_for:
            continue
        for mid in it.get("source_ids", []):
            m = msgs.get(mid)
            if not m or not m.files or mid in saved:
                continue
            target = lay.attach_dir(it.get("_course"))
            for n, (name, data) in enumerate(m.files):
                base = f"{m.date:%Y-%m-%d}_{m.id}_{n}_{name}" if name == "photo.jpg" else f"{m.date:%Y-%m-%d}_{m.id}_{name}"
                fname = safe_name(base)
                w.write_bytes(target / fname, data)
                saved.setdefault(mid, []).append(fname)
    return saved


def write_digests(w: VaultWriter, lay: Layout, items: list[dict], msgs: dict, files: dict[int, list[str]],
                  chat_title: str, run_at: dt.datetime) -> dict[dt.date, int]:
    by_day: dict[dt.date, list[dict]] = {}
    for it in items:
        by_day.setdefault(item_day(it, msgs), []).append(it)

    for day, group in sorted(by_day.items()):
        path = lay.digest_path(day)
        parts = []
        if not w.read(path):
            tags = "".join(f"\n  - {t}" for t in lay.tags)
            parts.append(f"---\nkind: digest\ndate: {day:%Y-%m-%d}\nsource: \"{chat_title}\"\ntags:{tags}\n---\n"
                         f"\n# Telegram {day:%d.%m.%Y}\n\n← [[{day:%Y-%m-%d}]]\n")
        stamp = f"{run_at:%H:%M}" if day == run_at.date() else f"прогон {run_at:%d.%m %H:%M}"
        parts.append(f"\n## {stamp}\n")
        for cat, title in CATEGORY_TITLES.items():
            catitems = [i for i in group if i.get("category", "other") == cat]
            if not catitems:
                continue
            parts.append(f"\n### {title}\n")
            for i in catitems:
                mark = "🔴 " if i.get("urgent") else ""
                when = f" — **{i['when']}**" if i.get("when") else ""
                ids = [mid for mid in i.get("source_ids", []) if mid in msgs]
                refs = " ".join(f"[#{mid}]({msgs[mid].link})" for mid in ids)
                line = f"- {mark}{subj_prefix(i.get('_course'), i.get('subject'))}{i.get('summary', '').strip()}{when} {refs}"
                parts.append(line.rstrip() + "\n")
                for mid in ids:
                    for fname in files.get(mid, []):
                        embed = "!" if Path(fname).suffix.lower() in IMAGE_EXTS else ""
                        parts.append(f"\t- {embed}[[{fname}]]\n")
        w.append(path, "".join(parts))
    return {d: len(g) for d, g in by_day.items()}


def write_log_line(w: VaultWriter, lay: Layout, items: list[dict], per_day: dict[dt.date, int],
                   run_at: dt.datetime) -> None:
    if not per_day:
        return
    links = ", ".join(f"{digest_link(d)} ({n})" for d, n in sorted(per_day.items()))
    lines = [f"- {run_at:%H:%M} — Telegram: {sum(per_day.values())} записей → {links}\n"]
    for i in items:
        if i.get("urgent"):
            when = f" ({i['when']})" if i.get("when") else ""
            lines.append(f"  - 🔴 {subj_prefix(i.get('_course'), i.get('subject'))}{i.get('summary', '').strip()}{when}\n")
    w.append(lay.log_dir / f"{run_at:%Y-%m-%d}.md", "".join(lines))


DEADLINES_HEADER = """---
kind: deadlines
source: telegram
---
# Дедлайны

Собирается автоматически из Telegram-группы (tg-group-reader): новые строки дописываются в конец, дубли пропускаются.
Выполнил — поставь галочку. Формат дат `📅 YYYY-MM-DD` понимают Dataview и плагин Tasks.

"""

DEADLINE_RE = re.compile(r"^- \[.\] (?:\[\[([^\]]+)\]\]: )?(.*?) 📅 (\d{4}-\d{2}-\d{2})", re.M)


def _norm(s: str) -> str:
    s = re.sub(r"\[\[[^\]]*\]\]|\(обновлено\)", "", s)
    return re.sub(r"[^\w]+", " ", s.casefold()).strip()


def find_assignment(lay: Layout, course: str | None, due: dt.date) -> str | None:
    adir = lay.courses_dir / course / "Assignments" if course else None
    if not adir or not adir.exists():
        return None
    hits = sorted(p.name for p in adir.iterdir() if p.is_dir() and p.name.startswith(f"{due:%Y-%m-%d}"))
    return hits[0] if len(hits) == 1 else None


def write_deadlines(w: VaultWriter, lay: Layout, items: list[dict], msgs: dict, today: dt.date) -> list[dict]:
    path = lay.deadlines_note
    text = w.read(path) or DEADLINES_HEADER
    existing = [(c, _norm(t), d) for c, t, d in DEADLINE_RE.findall(text)]
    added = []
    for it in items:
        due = parse_due(it)
        if it.get("category") != "deadline" or not due or due < today:
            continue
        course = it.get("_course")
        title = (it.get("title") or it.get("summary") or "").strip()
        key = _norm(title)
        dup = any(c == (course or "") and d == due.isoformat()
                  and difflib.SequenceMatcher(None, key, t).ratio() >= 0.6 for c, t, d in existing)
        if dup:
            continue
        task = find_assignment(lay, course, due)
        extra = f" · [[{task}]]" if task else ""
        src = digest_link(item_day(it, msgs))
        text += f"- [ ] {subj_prefix(course, it.get('subject'))}{title}{extra} · {src} 📅 {due.isoformat()}\n"
        existing.append((course or "", key, due.isoformat()))
        added.append(it)
    if added:
        w.set(path, text)
    return added


def _insert_into_section(text: str, heading: str, lines: list[str]) -> str:
    new = [l for l in lines if l.rstrip("\n") not in text]
    if not new:
        return text
    block = "".join(new)
    if heading not in text:
        return text.rstrip("\n") + f"\n\n{heading}\n" + block
    start = text.index(heading) + len(heading)
    nxt = re.search(r"\n## |\n<!-- links:start -->", text[start:])
    end = start + nxt.start() + 1 if nxt else len(text)
    body = text[:end].rstrip("\n") + "\n"
    return body + block + ("\n" + text[end:] if nxt else "")


def write_course_sections(w: VaultWriter, lay: Layout, items: list[dict], msgs: dict,
                          files: dict[int, list[str]], new_deadlines: list[dict] | None = None) -> None:
    """Append deadline/material lines to the course hub. If new_deadlines is given, only those
    deadlines are added (the rest were duplicates in the deadlines note)."""
    fresh = {id(i) for i in new_deadlines} if new_deadlines is not None else None
    per_course: dict[str, list[str]] = {}
    for it in items:
        course, cat = it.get("_course"), it.get("category")
        if not course or cat not in ("deadline", "material"):
            continue
        if cat == "deadline" and fresh is not None and id(it) not in fresh:
            continue
        day = item_day(it, msgs)
        if cat == "deadline":
            due = f" — до {it['when']}" if it.get("when") else ""
            line = f"- {day:%Y-%m-%d} ⏰ {(it.get('title') or it.get('summary', '')).strip()}{due} · {digest_link(day)}\n"
        else:
            fl = [f"[[{f}]]" for mid in it.get("source_ids", []) for f in files.get(mid, [])]
            line = f"- {day:%Y-%m-%d} 📎 {it.get('summary', '').strip()}" + (f" · {' '.join(fl)}" if fl else "") \
                + f" · {digest_link(day)}\n"
        per_course.setdefault(course, []).append(line)
    for course, lines in per_course.items():
        hub = lay.courses_dir / course / f"{course}.md"
        if not hub.exists():
            continue
        w.set(hub, _insert_into_section(w.read(hub), lay.course_section, lines))


def recent_context(lay: Layout, days: int = 2, chars: int = 4000) -> str:
    today = dt.date.today()
    paths = [lay.digest_path(today - dt.timedelta(days=d)) for d in range(days, -1, -1)]
    text = "".join(p.read_text(encoding="utf-8") for p in paths if p.exists())
    return text[-chars:]
