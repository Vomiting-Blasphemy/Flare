"""SQLite-backed statistics storage for FlareReminder.

Schema (one table is enough; ``reminders_config`` is a tiny key/value
backup of the JSON config so historical stats remain interpretable even
if the user later deletes a reminder)::

    events(
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_name   TEXT    NOT NULL,
        fired_at        INTEGER NOT NULL,        -- unix seconds
        acknowledged_at INTEGER,                 -- nullable
        delay_seconds   REAL                     -- nullable, set on ack
    )

    reminders_config(
        key   TEXT PRIMARY KEY,
        value TEXT
    )
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

DATA_DIR = Path(os.path.expanduser("~/.local/share/flarereminder"))
DB_FILE = DATA_DIR / "stats.db"

WEEK_SECONDS = 7 * 24 * 60 * 60


@dataclass
class ReminderStats:
    """Aggregated statistics for one reminder over a window."""

    reminder_name: str
    occurrences: int
    avg_delay_seconds: float | None
    max_delay_seconds: float | None
    min_delay_seconds: float | None


class StatsDB:
    """Wrapper around the SQLite stats database.

    Sessions: ``session_start_unix`` is captured at construction time;
    ``session_stats()`` only counts events fired since then.
    """

    def __init__(self, path: Path = DB_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_start_unix = int(time.time())
        self._init_schema()

    # ---- low-level --------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events(
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    reminder_name   TEXT    NOT NULL,
                    fired_at        INTEGER NOT NULL,
                    acknowledged_at INTEGER,
                    delay_seconds   REAL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_fired_at
                ON events(fired_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_reminder
                ON events(reminder_name)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders_config(
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

    # ---- write ------------------------------------------------------------

    def record_fire(self, reminder_name: str, fired_at: int | None = None) -> int:
        """Insert a new fire event and return its event id."""
        if fired_at is None:
            fired_at = int(time.time())
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO events(reminder_name, fired_at) VALUES (?, ?)",
                (reminder_name, fired_at),
            )
            event_id = int(cur.lastrowid or 0)
        log.debug("Recorded fire event %d for %s @ %d", event_id, reminder_name, fired_at)
        return event_id

    def record_ack(self, event_id: int, ack_at: int | None = None) -> None:
        """Mark a previously-fired event as acknowledged."""
        if ack_at is None:
            ack_at = int(time.time())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT fired_at FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                log.warning("record_ack: no event with id %s", event_id)
                return
            delay = float(ack_at - int(row["fired_at"]))
            if delay < 0:
                delay = 0.0
            conn.execute(
                "UPDATE events SET acknowledged_at = ?, delay_seconds = ? WHERE id = ?",
                (ack_at, delay, event_id),
            )
        log.debug("Recorded ack for event %d (delay=%.2fs)", event_id, delay)

    def save_reminders_backup(self, json_text: str) -> None:
        """Persist a serialised snapshot of the reminders config."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO reminders_config(key, value) VALUES (?, ?)",
                ("reminders_json", json_text),
            )

    # ---- read -------------------------------------------------------------

    def _aggregate(self, since_unix: int) -> list[ReminderStats]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT reminder_name,
                       COUNT(*)            AS occurrences,
                       AVG(delay_seconds)  AS avg_delay,
                       MAX(delay_seconds)  AS max_delay,
                       MIN(delay_seconds)  AS min_delay
                FROM events
                WHERE fired_at >= ?
                GROUP BY reminder_name
                ORDER BY reminder_name
                """,
                (since_unix,),
            ).fetchall()
        return [
            ReminderStats(
                reminder_name=row["reminder_name"],
                occurrences=int(row["occurrences"]),
                avg_delay_seconds=row["avg_delay"],
                max_delay_seconds=row["max_delay"],
                min_delay_seconds=row["min_delay"],
            )
            for row in rows
        ]

    def session_stats(self) -> list[ReminderStats]:
        """Stats for events fired since this StatsDB instance was created."""
        return self._aggregate(self.session_start_unix)

    def week_stats(self, now: int | None = None) -> list[ReminderStats]:
        """Stats for events fired in the last 7 days."""
        if now is None:
            now = int(time.time())
        return self._aggregate(now - WEEK_SECONDS)

    def all_events(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT reminder_name, fired_at, acknowledged_at, delay_seconds "
                "FROM events ORDER BY fired_at ASC"
            ).fetchall()

    # ---- export / clear ---------------------------------------------------

    def export_csv(self, path: Path) -> int:
        """Export all events to a CSV file. Returns the number of rows written."""
        rows = self.all_events()
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["reminder_name", "fired_at", "acknowledged_at", "delay_seconds"]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["reminder_name"],
                        _iso(row["fired_at"]),
                        _iso(row["acknowledged_at"]),
                        f"{row['delay_seconds']:.3f}"
                        if row["delay_seconds"] is not None
                        else "",
                    ]
                )
        log.info("Exported %d stat rows to %s", len(rows), path)
        return len(rows)

    def clear(self) -> None:
        """Delete all stats. Schema is preserved."""
        with self._conn() as conn:
            conn.execute("DELETE FROM events")
        log.info("Cleared all stats")


def _iso(unix_seconds: int | None) -> str:
    if unix_seconds is None:
        return ""
    return datetime.fromtimestamp(int(unix_seconds), tz=timezone.utc).isoformat()
