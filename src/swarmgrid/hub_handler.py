#!/usr/bin/env python3
"""Hub CGI handler — one invocation per SSH connection.

Reads a single JSON line from stdin, dispatches the command against
a local SQLite database, writes a JSON response to stdout, and exits.

This script is invoked by upterm's --force-command for each incoming
SSH connection (CGI model).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_DIR = Path(__file__).resolve().parents[2] / "var" / "hub"
DB_PATH = DB_DIR / "hub.sqlite"


def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dev_id TEXT NOT NULL,
            ticket_key TEXT NOT NULL,
            summary TEXT DEFAULT '',
            status TEXT DEFAULT '',
            checked_in_at REAL NOT NULL,
            ssh_client TEXT DEFAULT ''
        )"""
    )
    conn.commit()
    return conn


def _ssh_client_info() -> str:
    return os.environ.get("SSH_CLIENT", "unknown")


def handle_ping(_payload: dict) -> dict:
    return {"ok": True, "pong": True}


def handle_checkin(payload: dict) -> dict:
    dev_id = payload.get("dev_id", "")
    tickets = payload.get("tickets", [])
    if not dev_id:
        return {"ok": False, "error": "dev_id required"}

    now = time.time()
    ssh_client = _ssh_client_info()
    db = _get_db()
    count = 0
    for ticket in tickets:
        key = ticket if isinstance(ticket, str) else ticket.get("key", "")
        summary = ticket.get("summary", "") if isinstance(ticket, dict) else ""
        status = ticket.get("status", "") if isinstance(ticket, dict) else ""
        if not key:
            continue
        db.execute(
            "INSERT INTO checkins (dev_id, ticket_key, summary, status, checked_in_at, ssh_client) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dev_id, key, summary, status, now, ssh_client),
        )
        count += 1
    db.commit()
    db.close()
    return {"ok": True, "checked_in": count}


def handle_list(_payload: dict) -> dict:
    db = _get_db()
    rows = db.execute(
        "SELECT dev_id, ticket_key, summary, status, checked_in_at, ssh_client "
        "FROM checkins ORDER BY checked_in_at DESC LIMIT 100"
    ).fetchall()
    db.close()
    checkins = [
        {
            "dev_id": r[0],
            "ticket_key": r[1],
            "summary": r[2],
            "status": r[3],
            "checked_in_at": r[4],
            "ssh_client": r[5],
        }
        for r in rows
    ]
    return {"ok": True, "checkins": checkins}


def handle_whoami(_payload: dict) -> dict:
    return {"ok": True, "ssh_client": _ssh_client_info()}


COMMANDS = {
    "ping": handle_ping,
    "checkin": handle_checkin,
    "list": handle_list,
    "whoami": handle_whoami,
}


def main() -> None:
    try:
        line = sys.stdin.readline().strip()
        if not line:
            json.dump({"ok": False, "error": "empty input"}, sys.stdout)
            return
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"invalid JSON: {exc}"}, sys.stdout)
        return

    cmd = payload.get("cmd", "")
    handler = COMMANDS.get(cmd)
    if not handler:
        json.dump({"ok": False, "error": f"unknown command: {cmd}"}, sys.stdout)
        return

    try:
        result = handler(payload)
        json.dump(result, sys.stdout)
    except Exception as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout)


if __name__ == "__main__":
    main()
