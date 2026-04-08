"""Tests for the stats module."""

from __future__ import annotations

import csv
from pathlib import Path

from flarereminder.stats import WEEK_SECONDS, StatsDB


def make_db(tmp_path: Path) -> StatsDB:
    return StatsDB(tmp_path / "stats.db")


def test_record_fire_returns_id(tmp_path: Path):
    db = make_db(tmp_path)
    eid = db.record_fire("Blink", fired_at=1_700_000_000)
    assert eid > 0


def test_ack_sets_delay(tmp_path: Path):
    db = make_db(tmp_path)
    eid = db.record_fire("Blink", fired_at=1_700_000_000)
    db.record_ack(eid, ack_at=1_700_000_010)
    rows = db.all_events()
    assert len(rows) == 1
    assert rows[0]["delay_seconds"] == 10.0
    assert rows[0]["acknowledged_at"] == 1_700_000_010


def test_ack_clamps_negative_delay(tmp_path: Path):
    db = make_db(tmp_path)
    eid = db.record_fire("Blink", fired_at=1_700_000_100)
    db.record_ack(eid, ack_at=1_700_000_050)  # acknowledged "before" fired
    rows = db.all_events()
    assert rows[0]["delay_seconds"] == 0.0


def test_session_stats_only_includes_session(tmp_path: Path):
    db = make_db(tmp_path)
    # Insert one event from "before" the session start.
    db.record_fire("Blink", fired_at=db.session_start_unix - 100)
    # And one from now.
    eid = db.record_fire("Blink", fired_at=db.session_start_unix + 5)
    db.record_ack(eid, ack_at=db.session_start_unix + 12)
    session = db.session_stats()
    assert len(session) == 1
    assert session[0].reminder_name == "Blink"
    assert session[0].occurrences == 1
    assert session[0].avg_delay_seconds == 7.0


def test_week_stats_includes_week(tmp_path: Path):
    db = make_db(tmp_path)
    now = 2_000_000_000
    # Inside the week.
    db.record_fire("Blink", fired_at=now - 100)
    # Outside the week.
    db.record_fire("Blink", fired_at=now - WEEK_SECONDS - 1000)
    week = db.week_stats(now=now)
    assert len(week) == 1
    assert week[0].occurrences == 1


def test_aggregate_groups_by_reminder(tmp_path: Path):
    db = make_db(tmp_path)
    e1 = db.record_fire("Blink", fired_at=db.session_start_unix + 1)
    e2 = db.record_fire("Blink", fired_at=db.session_start_unix + 2)
    e3 = db.record_fire("Drink Water", fired_at=db.session_start_unix + 3)
    db.record_ack(e1, ack_at=db.session_start_unix + 6)   # delay 5
    db.record_ack(e2, ack_at=db.session_start_unix + 12)  # delay 10
    db.record_ack(e3, ack_at=db.session_start_unix + 23)  # delay 20
    stats = {s.reminder_name: s for s in db.session_stats()}
    assert stats["Blink"].occurrences == 2
    assert stats["Blink"].avg_delay_seconds == 7.5
    assert stats["Blink"].max_delay_seconds == 10
    assert stats["Blink"].min_delay_seconds == 5
    assert stats["Drink Water"].occurrences == 1


def test_export_csv(tmp_path: Path):
    db = make_db(tmp_path)
    eid = db.record_fire("Blink", fired_at=1_700_000_000)
    db.record_ack(eid, ack_at=1_700_000_005)
    out = tmp_path / "out.csv"
    n = db.export_csv(out)
    assert n == 1
    with out.open() as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["reminder_name", "fired_at", "acknowledged_at", "delay_seconds"]
    assert rows[1][0] == "Blink"
    assert rows[1][1].startswith("2023-")  # ISO format
    assert rows[1][3] == "5.000"


def test_clear(tmp_path: Path):
    db = make_db(tmp_path)
    db.record_fire("Blink")
    db.record_fire("Blink")
    db.clear()
    assert db.all_events() == []


def test_save_reminders_backup(tmp_path: Path):
    db = make_db(tmp_path)
    db.save_reminders_backup('{"reminders": []}')
    db.save_reminders_backup('{"reminders": [1]}')  # overwrite
    # No exception means it worked. Spot-check via raw SQL.
    import sqlite3
    conn = sqlite3.connect(str(db.path))
    row = conn.execute(
        "SELECT value FROM reminders_config WHERE key='reminders_json'"
    ).fetchone()
    assert row[0] == '{"reminders": [1]}'
    conn.close()
