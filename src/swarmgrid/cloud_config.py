"""Fetch routes from the SwarmGrid cloud API and convert to RouteSettings.

Falls back to YAML routes if the cloud is unreachable or has no routes.
"""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from .config import AppConfig, RouteSettings, _build_route

logger = logging.getLogger(__name__)


def _cloud_base_url() -> str:
    from .agent.registration import _cloud_base_url as _base
    return _base()


def _api_key() -> str | None:
    from .agent.registration import _api_key as _key
    return _key()


def _cloud_get(url: str, api_key: str, timeout: int = 15) -> dict | list | None:
    """GET from the cloud API. Returns parsed JSON or None on failure."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        logger.warning("Cloud API HTTP %d for %s", exc.code, url)
        return None
    except Exception as exc:
        logger.warning("Cloud API request failed for %s: %s", url, exc)
        return None


def _resolve_template(board_id: str, template_name: str, api_key: str) -> str | None:
    """Resolve a template name to its prompt_template via the cloud API."""
    base = _cloud_base_url()
    url = f"{base}/api/templates/resolve/{board_id}/{template_name}"
    data = _cloud_get(url, api_key)
    if data and isinstance(data, dict):
        return data.get("prompt_template")
    return None


def _cloud_route_to_settings(
    cloud_route: dict,
    yaml_route: RouteSettings | None,
    board_id: str,
    api_key: str,
) -> RouteSettings:
    """Convert a cloud route dict to a RouteSettings, merging YAML defaults."""
    # Start with the cloud fields
    item: dict = {
        "status": cloud_route["status"],
        "action": cloud_route.get("action", ""),
        "prompt_template": cloud_route.get("prompt_template", ""),
        "enabled": cloud_route.get("enabled", True),
        "transition_on_launch": cloud_route.get("transition_on_launch"),
        "transition_on_success": cloud_route.get("transition_on_success"),
        "transition_on_failure": cloud_route.get("transition_on_failure"),
    }

    # If prompt_template is empty but action looks like a template name, resolve it
    if not item["prompt_template"] and item["action"]:
        template_name = item["action"].lstrip("/")
        resolved = _resolve_template(board_id, template_name, api_key)
        if resolved:
            item["prompt_template"] = resolved
            logger.info("Resolved template '%s' for status '%s'", template_name, item["status"])

    # Merge fields the cloud doesn't provide from the YAML route (if one matches)
    if yaml_route:
        if not item.get("prompt_template"):
            item["prompt_template"] = yaml_route.prompt_template
        for field in (
            "allowed_issue_types",
            "fire_on_first_seen",
            "comment_on_launch_template",
            "comment_on_success_template",
            "comment_on_failure_template",
            "artifact_globs",
            "idle_timeout_minutes",
            "cold_timeout_minutes",
            "output_match_patterns",
            "transition_on_idle",
            "transition_on_match",
        ):
            if field not in cloud_route:
                val = getattr(yaml_route, field, None)
                if val is not None:
                    item[field] = val

    return _build_route(item)


def _resolve_cloud_board_id(api_key: str, jira_board_id: str) -> str | None:
    """Map a Jira board ID (e.g. '1183') to the cloud DB board ID (e.g. '1')."""
    base = _cloud_base_url()
    data = _cloud_get(f"{base}/api/boards", api_key)
    if not data or not isinstance(data, dict):
        return None
    for board in data.get("boards", []):
        if str(board.get("jira_board_id", "")) == str(jira_board_id):
            return str(board["id"])
    return None


def fetch_cloud_routes(config: AppConfig) -> list[RouteSettings] | None:
    """Fetch routes from the cloud API for this board.

    Returns a list of RouteSettings if successful, or None if the cloud
    is unreachable / has no routes (so the caller can fall back to YAML).
    """
    api_key = _api_key()
    if not api_key:
        logger.debug("No cloud API key — using YAML routes")
        return None

    jira_board_id = config.board_id
    if not jira_board_id:
        logger.debug("No board_id in config — using YAML routes")
        return None

    # Map Jira board ID to cloud board ID
    board_id = _resolve_cloud_board_id(api_key, jira_board_id)
    if not board_id:
        logger.debug("Jira board %s not found in cloud — using YAML routes", jira_board_id)
        return None

    base = _cloud_base_url()
    url = f"{base}/api/boards/{board_id}/routes"
    data = _cloud_get(url, api_key)

    if data is None:
        return None

    cloud_routes_raw = data.get("routes", []) if isinstance(data, dict) else []
    if not cloud_routes_raw:
        logger.info("Cloud returned no routes for board %s — using YAML routes", board_id)
        return None

    # Build a lookup of YAML routes by status for merging defaults
    yaml_by_status = {r.status: r for r in config.routes}

    routes: list[RouteSettings] = []
    for cr in cloud_routes_raw:
        if not cr.get("status"):
            continue
        yaml_match = yaml_by_status.get(cr["status"])
        try:
            route = _cloud_route_to_settings(cr, yaml_match, board_id, api_key)
            routes.append(route)
        except Exception as exc:
            logger.warning("Failed to parse cloud route for status '%s': %s", cr.get("status"), exc)

    logger.info("Loaded %d routes from cloud for board %s", len(routes), board_id)
    return routes
