"""Local heartbeat loop for the edge agent.

Reuses the existing service.py logic to poll Jira and launch agent
sessions.  Reports results back to the cloud via registration.py.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import load_config
from ..cloud_config import _resolve_cloud_board_id, _api_key
from ..service import run_heartbeat
from .registration import report_heartbeat

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 120  # seconds


def run_heartbeat_loop(
    config_path: str | Path,
    *,
    poll_interval: int | None = None,
    stop_event=None,
) -> None:
    """Run the Jira heartbeat in a loop until stop_event is set.

    This is the main heartbeat for the edge agent.  Each tick:
    1. Calls the existing run_heartbeat() from service.py
    2. Reports results to the cloud (best-effort)
    3. Sleeps for poll_interval seconds

    Args:
        config_path: Path to board-routes.yaml
        poll_interval: Override poll interval in seconds (default from config)
        stop_event: threading.Event or similar — loop exits when set
    """
    config = load_config(config_path)
    interval = poll_interval or int(config.poll_interval_minutes * 60)
    if interval < 10:
        interval = DEFAULT_POLL_INTERVAL

    logger.info("Heartbeat loop starting (interval=%ds, config=%s)", interval, config_path)

    while True:
        if stop_event and stop_event.is_set():
            logger.info("Heartbeat loop stopping (stop_event set)")
            break

        try:
            result = run_heartbeat(config_path)
            logger.info(
                "Heartbeat tick: %d issues, %d decisions, %d launched",
                result.get("issue_count", 0),
                result.get("decision_count", 0),
                result.get("launched_count", 0),
            )

            # Best-effort cloud reporting
            try:
                api_key = _api_key()
                cloud_board_id = 0
                if api_key and config.board_id:
                    resolved = _resolve_cloud_board_id(api_key, config.board_id)
                    if resolved:
                        cloud_board_id = int(resolved)

                tickets = [
                    {"key": d.get("issue_key"), "status": d.get("status_name")}
                    for d in result.get("decisions", [])
                ]
                launches = [
                    {
                        "session_id": l.get("session_name", ""),
                        "ticket_key": l.get("issue_key", ""),
                        "ticket_summary": l.get("prompt", "")[:200],
                        "prompt": l.get("prompt", "")[:500],
                    }
                    for l in result.get("launches", [])
                    if l.get("state") == "running"
                ]
                if cloud_board_id:
                    report_heartbeat(
                        board_id=cloud_board_id,
                        tickets_found=tickets,
                        sessions_launched=launches,
                    )
            except Exception as exc:
                logger.debug("Cloud heartbeat report failed (non-fatal): %s", exc)

        except Exception as exc:
            logger.error("Heartbeat tick failed: %s", exc)

        if stop_event:
            stop_event.wait(timeout=interval)
            if stop_event.is_set():
                break
        else:
            time.sleep(interval)
