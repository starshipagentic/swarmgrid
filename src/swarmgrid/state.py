from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import os
import sqlite3
from typing import Any

from .models import JiraIssue, LaunchRecord


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_state (
  issue_key TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  status_name TEXT NOT NULL,
  status_id TEXT NOT NULL,
  updated TEXT NOT NULL,
  assignee TEXT,
  browse_url TEXT NOT NULL,
  parent_key TEXT,
  parent_summary TEXT,
  epic_story_count INTEGER,
  labels TEXT,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key TEXT NOT NULL,
  status_name TEXT NOT NULL,
  action_name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  should_launch INTEGER NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key TEXT NOT NULL,
  status_name TEXT NOT NULL,
  command_line TEXT NOT NULL,
  pid INTEGER,
  log_path TEXT NOT NULL,
  state TEXT NOT NULL,
  return_code INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat_ticks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  issue_count INTEGER NOT NULL,
  decision_count INTEGER NOT NULL,
  launched_count INTEGER NOT NULL
);
"""

PROCESS_RUN_COLUMNS: dict[str, str] = {
    "action_name": "TEXT",
    "prompt": "TEXT",
    "run_dir": "TEXT",
    "session_name": "TEXT",
    "launch_mode": "TEXT",
    "is_live": "INTEGER DEFAULT 1",
    "archived_at": "TEXT",
    "archived_reason": "TEXT",
    "artifact_globs": "TEXT",
    "artifact_paths": "TEXT",
    "transition_on_launch": "TEXT",
    "transition_on_success": "TEXT",
    "transition_on_failure": "TEXT",
    "comment_on_launch": "TEXT",
    "comment_on_success": "TEXT",
    "comment_on_failure": "TEXT",
    "jira_launch_transition_applied": "INTEGER DEFAULT 0",
    "jira_final_transition_applied": "INTEGER DEFAULT 0",
    "jira_comment_on_launch_applied": "INTEGER DEFAULT 0",
    "jira_comment_on_final_applied": "INTEGER DEFAULT 0",
    "jira_last_error": "TEXT",
}

ISSUE_STATE_COLUMNS: dict[str, str] = {
    "parent_key": "TEXT",
    "parent_issue_type": "TEXT",
    "parent_summary": "TEXT",
    "epic_story_count": "INTEGER",
    "labels": "TEXT",
}


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.logs_dir = self.state_dir / "logs"
        self.run_artifacts_dir = self.state_dir / "artifacts"
        self.transitions_dir = self.state_dir / "transitions"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.run_artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.transitions_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "heartbeat.sqlite"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connection() as connection:
            connection.executescript(BASE_SCHEMA)
            existing_issue_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(issue_state)").fetchall()
            }
            for column_name, column_type in ISSUE_STATE_COLUMNS.items():
                if column_name in existing_issue_columns:
                    continue
                try:
                    connection.execute(
                        f"ALTER TABLE issue_state ADD COLUMN {column_name} {column_type}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(process_runs)").fetchall()
            }
            for column_name, column_type in PROCESS_RUN_COLUMNS.items():
                if column_name in existing_columns:
                    continue
                try:
                    connection.execute(
                        f"ALTER TABLE process_runs ADD COLUMN {column_name} {column_type}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise

    def get_issue_state(self, issue_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM issue_state WHERE issue_key = ?",
                (issue_key,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_issue_state(self, issue: JiraIssue, seen_at: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO issue_state (
                  issue_key, summary, issue_type, status_name, status_id,
                  updated, assignee, browse_url, parent_key, parent_issue_type,
                  parent_summary, epic_story_count, labels, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                  summary = excluded.summary,
                  issue_type = excluded.issue_type,
                  status_name = excluded.status_name,
                  status_id = excluded.status_id,
                  updated = excluded.updated,
                  assignee = excluded.assignee,
                  browse_url = excluded.browse_url,
                  parent_key = excluded.parent_key,
                  parent_issue_type = excluded.parent_issue_type,
                  parent_summary = excluded.parent_summary,
                  epic_story_count = excluded.epic_story_count,
                  labels = excluded.labels,
                  last_seen_at = excluded.last_seen_at
                """,
                (
                    issue.key,
                    issue.summary,
                    issue.issue_type,
                    issue.status_name,
                    issue.status_id,
                    issue.updated,
                    issue.assignee,
                    issue.browse_url,
                    issue.parent_key,
                    issue.parent_issue_type,
                    issue.parent_summary,
                    issue.epic_story_count,
                    "\n".join(issue.labels or []),
                    seen_at,
                ),
            )

    def record_decision(
        self,
        issue_key: str,
        status_name: str,
        action_name: str,
        prompt: str,
        should_launch: bool,
        reason: str,
        created_at: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO decisions (
                  issue_key, status_name, action_name, prompt,
                  should_launch, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_key,
                    status_name,
                    action_name,
                    prompt,
                    1 if should_launch else 0,
                    reason,
                    created_at,
                ),
            )

    def get_latest_decision(
        self,
        issue_key: str,
        action_name: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT issue_key, status_name, action_name, prompt, should_launch, reason, created_at
                FROM decisions
                WHERE issue_key = ? AND action_name = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (issue_key, action_name),
            ).fetchone()
        return dict(row) if row else None

    def get_active_process(
        self,
        issue_key: str,
        action_name: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM process_runs
                WHERE issue_key = ? AND action_name = ? AND state = 'running'
                ORDER BY id DESC
                LIMIT 1
                """,
                (issue_key, action_name),
            ).fetchone()
        return dict(row) if row else None

    def record_process_run(
        self,
        launch: LaunchRecord,
        created_at: str,
        return_code: int | None = None,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO process_runs (
                  issue_key, status_name, action_name, prompt, command_line, pid,
                  log_path, run_dir, session_name, launch_mode, is_live, archived_at, archived_reason,
                  artifact_globs, artifact_paths, state, return_code,
                  transition_on_launch, transition_on_success, transition_on_failure,
                  comment_on_launch, comment_on_success, comment_on_failure,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    launch.issue_key,
                    launch.status_name,
                    launch.action,
                    launch.prompt,
                    launch.command_line,
                    launch.pid,
                    launch.log_path,
                    launch.run_dir,
                    launch.session_name,
                    launch.launch_mode,
                    1,
                    None,
                    None,
                    "\n".join(launch.artifact_globs),
                    "",
                    launch.state,
                    return_code,
                    launch.transition_on_launch,
                    launch.transition_on_success,
                    launch.transition_on_failure,
                    launch.comment_on_launch,
                    launch.comment_on_success,
                    launch.comment_on_failure,
                    created_at,
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def record_tick(
        self,
        started_at: str,
        finished_at: str,
        issue_count: int,
        decision_count: int,
        launched_count: int,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO heartbeat_ticks (
                  started_at, finished_at, issue_count, decision_count, launched_count
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (started_at, finished_at, issue_count, decision_count, launched_count),
            )

    def list_running_processes(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM process_runs
                WHERE state = 'running' AND COALESCE(is_live, 1) = 1
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_archived_processes(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM process_runs
                WHERE COALESCE(is_live, 1) = 0
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_unfinalized_processes(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM process_runs
                WHERE state IN ('running', 'exited', 'command_missing', 'failed')
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def update_process_state(
        self,
        row_id: int,
        state: str,
        updated_at: str,
        return_code: int | None = None,
        artifact_paths: list[str] | None = None,
        jira_last_error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE process_runs
                SET state = ?,
                    updated_at = ?,
                    return_code = COALESCE(?, return_code),
                    artifact_paths = COALESCE(?, artifact_paths),
                    jira_last_error = COALESCE(?, jira_last_error)
                WHERE id = ?
                """,
                (
                    state,
                    updated_at,
                    return_code,
                    "\n".join(artifact_paths) if artifact_paths is not None else None,
                    jira_last_error,
                    row_id,
                ),
            )

    def archive_process(
        self,
        row_id: int,
        updated_at: str,
        reason: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE process_runs
                SET is_live = 0,
                    archived_at = ?,
                    archived_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (updated_at, reason, updated_at, row_id),
            )

    def mark_jira_launch_updates_applied(self, row_id: int, updated_at: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE process_runs
                SET jira_launch_transition_applied = 1,
                    jira_comment_on_launch_applied = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (updated_at, row_id),
            )

    def mark_jira_final_updates_applied(self, row_id: int, updated_at: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE process_runs
                SET jira_final_transition_applied = 1,
                    jira_comment_on_final_applied = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (updated_at, row_id),
            )

    def summarize(self) -> dict[str, Any]:
        with self._connection() as connection:
            issue_count = connection.execute(
                "SELECT COUNT(*) FROM issue_state"
            ).fetchone()[0]
            decision_count = connection.execute(
                "SELECT COUNT(*) FROM decisions"
            ).fetchone()[0]
            process_count = connection.execute(
                "SELECT COUNT(*) FROM process_runs"
            ).fetchone()[0]
            running_count = connection.execute(
                "SELECT COUNT(*) FROM process_runs WHERE state = 'running'"
            ).fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "issue_count": issue_count,
            "decision_count": decision_count,
            "process_count": process_count,
            "running_count": running_count,
        }

    def store_transitions(self, issue_key: str, transitions: list[dict[str, Any]]) -> None:
        """Write transition history for an issue to a JSON file."""
        file_path = self.transitions_dir / f"{issue_key}.json"
        file_path.write_text(json.dumps(transitions, indent=2), encoding="utf-8")

    def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """Read stored transition history for an issue. Returns empty list if not found."""
        file_path = self.transitions_dir / f"{issue_key}.json"
        if not file_path.exists():
            return []
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def list_issue_states(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT issue_key, summary, issue_type, status_name, assignee, updated,
                       browse_url, parent_key, parent_issue_type, parent_summary,
                       epic_story_count, labels
                FROM issue_state
                ORDER BY status_name ASC, updated DESC
                """
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["labels"] = item["labels"].split("\n") if item.get("labels") else []
            items.append(item)
        return items

    def list_recent_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT issue_key, status_name, action_name, prompt, should_launch, reason, created_at
                FROM decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_process_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT issue_key, status_name, action_name, state, prompt, log_path,
                       run_dir, session_name, launch_mode, is_live, archived_at, archived_reason,
                       artifact_paths, created_at, updated_at
                FROM process_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
