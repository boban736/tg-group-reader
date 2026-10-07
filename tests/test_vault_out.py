import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_out as vo  # noqa: E402

HUB = """---
kind: course
course: Baze de date
short: BD
teacher: B. Vișnevschi
---
# Baze de date

## Препод и контакты
- Лекции и лабы: B. Vișnevschi

## Заметки

<!-- links:start -->
## Задания
<!-- links:end -->
"""


@dataclass
class FakeMsg:
    id: int
    date: dt.datetime
    link: str = "https://t.me/c/1/1"
    files: list = field(default_factory=list)


def make_vault(tmp_path: Path) -> vo.Layout:
    cdir = tmp_path / "Courses" / "Baze de date"
    (cdir / "Assignments" / "2026-10-09 Аттестация 1").mkdir(parents=True)
    (cdir / "Baze de date.md").write_text(HUB, encoding="utf-8")
    (tmp_path / "Log").mkdir()
    (tmp_path / "Log" / "2026-10-07.md").write_text("- 11:09 — Moodle sync\n", encoding="utf-8")
    return vo.Layout(vault=tmp_path, digest_dir=tmp_path / "Log/Telegram", log_dir=tmp_path / "Log",
                     courses_dir=tmp_path / "Courses", inbox_attach_dir=tmp_path / "Inbox/Telegram",
                     deadlines_note=tmp_path / "Дедлайны.md")


def deadline(title, due="2026-10-09", mid=1, course="Baze de date"):
    return {"category": "deadline", "subject": course, "_course": course, "title": title,
            "summary": title, "when": "09.10 20:00", "due": due, "urgent": False, "source_ids": [mid]}


def test_load_courses_and_match(tmp_path):
    lay = make_vault(tmp_path)
    courses = vo.load_courses(lay.courses_dir)
    assert [c.name for c in courses] == ["Baze de date"]
    assert courses[0].short == "BD"
    assert vo.match_course("baze de date", courses) == "Baze de date"
    assert vo.match_course("BD", courses) == "Baze de date"
    assert vo.match_course("Логика", courses) is None


def test_deadlines_dedupe_and_assignment_link(tmp_path):
    lay = make_vault(tmp_path)
    now = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)
    msgs = {1: FakeMsg(1, now), 2: FakeMsg(2, now)}
    w = vo.VaultWriter(tmp_path)
    added = vo.write_deadlines(w, lay, [deadline("Аттестация 1"), deadline("Аттестация №1", mid=2),
                                        deadline("Старый тест", due="2026-10-01")], msgs, now.date())
    w.flush()
    text = lay.deadlines_note.read_text(encoding="utf-8")
    assert len(added) == 1
    assert text.count("- [ ]") == 1
    assert "[[2026-10-09 Аттестация 1]]" in text and "📅 2026-10-09" in text

    # second run: same deadline again is skipped, user's tick is preserved
    lay.deadlines_note.write_text(text.replace("- [ ]", "- [x]"), encoding="utf-8")
    w2 = vo.VaultWriter(tmp_path)
    assert vo.write_deadlines(w2, lay, [deadline("Аттестация 1")], msgs, now.date()) == []
    assert w2.changes() == []


def test_course_section_survives_links_block_and_appends(tmp_path):
    lay = make_vault(tmp_path)
    now = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)
    msgs = {1: FakeMsg(1, now)}
    hub = lay.courses_dir / "Baze de date" / "Baze de date.md"
    for title in ("Lab 3", "Lab 4"):
        w = vo.VaultWriter(tmp_path)
        item = {"category": "material", "_course": "Baze de date", "summary": title, "source_ids": [1]}
        vo.write_course_sections(w, lay, [item], msgs, {})
        w.flush()
    text = hub.read_text(encoding="utf-8")
    assert text.startswith(HUB.rstrip("\n"))  # original content untouched
    assert text.count("## Из Telegram") == 1
    assert text.index("Lab 3") < text.index("Lab 4")


def test_section_inserted_before_next_heading(tmp_path):
    text = "# X\n\n## Из Telegram\n- old\n\n## Другое\n- keep\n"
    out = vo._insert_into_section(text, "## Из Telegram", ["- new\n"])
    assert out == "# X\n\n## Из Telegram\n- old\n- new\n\n## Другое\n- keep\n"
    assert vo._insert_into_section(out, "## Из Telegram", ["- new\n"]) == out


def test_flush_reapplies_append_after_concurrent_edit(tmp_path):
    lay = make_vault(tmp_path)
    log = lay.log_dir / "2026-10-07.md"
    w = vo.VaultWriter(tmp_path)
    w.append(log, "- 22:00 — Telegram\n")
    log.write_text(log.read_text(encoding="utf-8") + "- 21:59 — written meanwhile\n", encoding="utf-8")
    w.flush()
    assert log.read_text(encoding="utf-8") == (
        "- 11:09 — Moodle sync\n- 21:59 — written meanwhile\n- 22:00 — Telegram\n")


def test_dry_run_writes_nothing(tmp_path):
    lay = make_vault(tmp_path)
    w = vo.VaultWriter(tmp_path, dry_run=True)
    w.append(lay.log_dir / "2026-10-07.md", "- x\n")
    w.write_bytes(lay.inbox_attach_dir / "a.jpg", b"1")
    assert "+- x" in w.diff()
    assert w.flush() == []
    assert not (lay.inbox_attach_dir / "a.jpg").exists()
    assert (lay.log_dir / "2026-10-07.md").read_text(encoding="utf-8") == "- 11:09 — Moodle sync\n"


def test_digest_split_by_message_day(tmp_path):
    lay = make_vault(tmp_path)
    run = dt.datetime(2026, 10, 7, 22, tzinfo=dt.timezone.utc)
    msgs = {1: FakeMsg(1, run - dt.timedelta(days=3)), 2: FakeMsg(2, run)}
    items = [{"category": "announcement", "summary": "old", "source_ids": [1]},
             {"category": "announcement", "summary": "new", "source_ids": [2]}]
    w = vo.VaultWriter(tmp_path)
    per_day = vo.write_digests(w, lay, items, msgs, {}, "G", run)
    vo.write_log_line(w, lay, items, per_day, run)
    w.flush()
    assert (lay.digest_dir / "TG 2026-10-04.md").exists()
    assert "kind: digest" in (lay.digest_dir / "TG 2026-10-07.md").read_text(encoding="utf-8")
    assert "[[TG 2026-10-04]] (1), [[TG 2026-10-07]] (1)" in (lay.log_dir / "2026-10-07.md").read_text(encoding="utf-8")
