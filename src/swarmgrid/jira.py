from __future__ import annotations

from collections import defaultdict
from typing import Any
import re

import requests

from .config import AppConfig, resolve_jira_auth
from .models import JiraIssue


class JiraClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        email, token = resolve_jira_auth(config)
        self._auth = (email, token)
        self._session = requests.Session()
        self._session.auth = self._auth
        self._session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )
        self._board_filter_jql: str | None | object = _UNSET

    @property
    def auth_email(self) -> str:
        """Return the email used for Jira authentication."""
        return self._auth[0]

    def fetch_issue_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch status transitions from the issue changelog.

        Returns a list of dicts sorted by timestamp ascending:
            {"timestamp", "author", "author_id", "from_status", "to_status"}
        """
        response = self._session.get(
            f"{self._config.site_url}/rest/api/3/issue/{issue_key}",
            params={"expand": "changelog", "fields": "status"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        transitions: list[dict[str, Any]] = []
        for history in data.get("changelog", {}).get("histories", []):
            created = history.get("created", "")
            author = history.get("author", {})
            author_name = author.get("displayName", "")
            author_id = author.get("accountId", "")
            for item in history.get("items", []):
                if item.get("field") == "status":
                    transitions.append({
                        "timestamp": created,
                        "author": author_name,
                        "author_id": author_id,
                        "from_status": item.get("fromString", ""),
                        "to_status": item.get("toString", ""),
                    })

        transitions.sort(key=lambda t: t["timestamp"])
        return transitions

    def validate_auth(self) -> dict[str, Any]:
        response = self._session.get(
            f"{self._config.site_url}/rest/api/3/myself",
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return {
            "account_id": data.get("accountId"),
            "display_name": data.get("displayName"),
            "email_address": data.get("emailAddress"),
        }

    def add_comment(self, issue_key: str, body_text: str) -> None:
        response = self._session.post(
            f"{self._config.site_url}/rest/api/3/issue/{issue_key}/comment",
            json={
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": body_text}],
                        }
                    ],
                }
            },
            timeout=30,
        )
        response.raise_for_status()

    def transition_issue(self, issue_key: str, transition_id: str) -> None:
        response = self._session.post(
            f"{self._config.site_url}/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": str(transition_id)}},
            timeout=30,
        )
        response.raise_for_status()

    def fetch_issue(self, issue_key: str) -> JiraIssue | None:
        response = self._session.get(
            f"{self._config.site_url}/rest/api/3/issue/{issue_key}",
            params={
                "fields": "summary,status,issuetype,assignee,updated,parent,labels",
            },
            timeout=30,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        issue = self._parse_issue(response.json())
        self._attach_epic_story_counts([issue])
        return issue

    def search_issues_by_status_history(self, statuses: list[str]) -> list[JiraIssue]:
        """Search for issues that were ever in any of the given statuses.

        Uses ``status WAS IN (...)`` JQL to find tickets that passed through
        trigger columns at any point.  Returns the same JiraIssue list as
        ``search_issues_by_statuses`` but with historical coverage.
        """
        if not statuses:
            return []

        quoted_statuses = ", ".join(f'"{status}"' for status in statuses)
        scope_jql = _strip_order_by(self._board_scope_jql()) or f"project = {self._config.project_key}"
        jql = f"({scope_jql}) AND status WAS IN ({quoted_statuses}) ORDER BY updated DESC"

        issues: list[JiraIssue] = []
        next_page_token: str | None = None
        max_results = 100

        while True:
            payload = {
                "jql": jql,
                "fields": [
                    "summary",
                    "status",
                    "issuetype",
                    "assignee",
                    "updated",
                    "parent",
                    "labels",
                ],
                "maxResults": max_results,
                "fieldsByKeys": False,
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            response = self._session.post(
                f"{self._config.site_url}/rest/api/3/search/jql",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            batch = [self._parse_issue(item) for item in data.get("issues", [])]
            issues.extend(batch)

            next_page_token = data.get("nextPageToken")
            if not next_page_token or data.get("isLast") is True:
                break

        self._attach_epic_story_counts(issues)
        return issues

    def search_issues_by_statuses(self, statuses: list[str]) -> list[JiraIssue]:
        if not statuses:
            return []

        quoted_statuses = ", ".join(f'"{status}"' for status in statuses)
        scope_jql = _strip_order_by(self._board_scope_jql()) or f"project = {self._config.project_key}"
        jql = f"({scope_jql}) AND status IN ({quoted_statuses}) ORDER BY updated DESC"

        issues: list[JiraIssue] = []
        next_page_token: str | None = None
        max_results = 100

        while True:
            payload = {
                "jql": jql,
                "fields": [
                    "summary",
                    "status",
                    "issuetype",
                    "assignee",
                    "updated",
                    "parent",
                    "labels",
                ],
                "maxResults": max_results,
                "fieldsByKeys": False,
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            response = self._session.post(
                f"{self._config.site_url}/rest/api/3/search/jql",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            batch = [self._parse_issue(item) for item in data.get("issues", [])]
            issues.extend(batch)

            next_page_token = data.get("nextPageToken")
            if not next_page_token or data.get("isLast") is True:
                break

        self._attach_epic_story_counts(issues)
        return issues

    def fetch_issue_statuses(self, issue_keys: list[str]) -> dict[str, str]:
        if not issue_keys:
            return {}

        statuses: dict[str, str] = {}
        for chunk in _chunked(issue_keys, size=50):
            quoted_keys = ", ".join(f'"{key}"' for key in chunk)
            payload = {
                "jql": f"issuekey IN ({quoted_keys})",
                "fields": ["status"],
                "maxResults": 100,
                "fieldsByKeys": False,
            }
            response = self._session.post(
                f"{self._config.site_url}/rest/api/3/search/jql",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            for item in data.get("issues", []):
                fields = item.get("fields", {})
                status = fields.get("status", {})
                name = status.get("name")
                if name:
                    statuses[item["key"]] = name
        return statuses

    def fetch_board_columns(self) -> list[dict]:
        """Fetch the actual columns and their statuses from the Jira board.

        Returns a list of dicts:
            [{"name": "To Do", "statuses": [{"name": "Droid-Do", "id": "11971"}, ...]}, ...]

        Uses the Agile board configuration endpoint.  Returns an empty list
        when board_id is not configured or the API call fails.
        """
        board_id = self._config.board_id
        if not board_id:
            return []

        try:
            response = self._session.get(
                f"{self._config.site_url}/rest/agile/1.0/board/{board_id}/configuration",
                timeout=30,
            )
            response.raise_for_status()
            config_data = response.json()
        except Exception:
            return []

        columns_raw = config_data.get("columnConfig", {}).get("columns", [])
        if not columns_raw:
            return []

        # The column config includes status objects with at least an "id" field.
        # Some Jira instances also include "self" but NOT the status name.
        # Build a lookup from status id -> name via project statuses endpoint.
        status_id_to_name: dict[str, str] = {}
        try:
            response = self._session.get(
                f"{self._config.site_url}/rest/api/3/project/{self._config.project_key}/statuses",
                timeout=30,
            )
            response.raise_for_status()
            for issue_type_block in response.json():
                for status in issue_type_block.get("statuses", []):
                    sid = str(status.get("id", ""))
                    sname = status.get("name", "")
                    if sid and sname:
                        status_id_to_name[sid] = sname
        except Exception:
            pass  # Best-effort: columns will still have IDs

        columns: list[dict] = []
        for col in columns_raw:
            col_name = col.get("name", "")
            statuses: list[dict] = []
            for st in col.get("statuses", []):
                sid = str(st.get("id", ""))
                sname = status_id_to_name.get(sid, sid)
                statuses.append({"name": sname, "id": sid})
            columns.append({"name": col_name, "statuses": statuses})

        return columns

    def _board_scope_jql(self) -> str | None:
        if self._board_filter_jql is not _UNSET:
            return self._board_filter_jql or None
        board_id = self._config.board_id
        if not board_id:
            self._board_filter_jql = None
            return None
        try:
            config_response = self._session.get(
                f"{self._config.site_url}/rest/agile/1.0/board/{board_id}/configuration",
                timeout=30,
            )
            config_response.raise_for_status()
            config_data = config_response.json()
            filter_id = config_data.get("filter", {}).get("id")
            if not filter_id:
                self._board_filter_jql = None
                return None
            filter_response = self._session.get(
                f"{self._config.site_url}/rest/api/3/filter/{filter_id}",
                timeout=30,
            )
            filter_response.raise_for_status()
            filter_data = filter_response.json()
            self._board_filter_jql = filter_data.get("jql") or None
            return self._board_filter_jql or None
        except Exception:
            self._board_filter_jql = None
            return None

    def _parse_issue(self, raw: dict[str, Any]) -> JiraIssue:
        fields = raw["fields"]
        assignee = fields.get("assignee")
        parent = fields.get("parent")
        parent_fields = parent.get("fields", {}) if parent else {}
        return JiraIssue(
            key=raw["key"],
            summary=fields.get("summary", ""),
            issue_type=fields["issuetype"]["name"],
            status_name=fields["status"]["name"],
            status_id=str(fields["status"]["id"]),
            updated=fields["updated"],
            assignee=assignee["displayName"] if assignee else None,
            browse_url=f"{self._config.site_url}/browse/{raw['key']}",
            parent_key=parent.get("key") if parent else None,
            parent_issue_type=parent_fields.get("issuetype", {}).get("name") if parent else None,
            parent_summary=parent_fields.get("summary") if parent else None,
            labels=fields.get("labels") or [],
        )

    def _attach_epic_story_counts(self, issues: list[JiraIssue]) -> None:
        epic_keys = [issue.key for issue in issues if issue.issue_type == "Epic"]
        if not epic_keys:
            return

        counts = self._fetch_epic_story_counts(epic_keys)
        for issue in issues:
            if issue.issue_type == "Epic":
                issue.epic_story_count = counts.get(issue.key, 0)

    def _fetch_epic_story_counts(self, epic_keys: list[str]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)

        for chunk in _chunked(epic_keys, size=20):
            quoted_epics = ", ".join(f'"{key}"' for key in chunk)
            jql = (
                f"project = {self._config.project_key} "
                f'AND issuetype = Story AND parent IN ({quoted_epics})'
            )
            next_page_token: str | None = None

            while True:
                payload = {
                    "jql": jql,
                    "fields": ["parent"],
                    "maxResults": 100,
                    "fieldsByKeys": False,
                }
                if next_page_token:
                    payload["nextPageToken"] = next_page_token

                response = self._session.post(
                    f"{self._config.site_url}/rest/api/3/search/jql",
                    json=payload,
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()

                for item in data.get("issues", []):
                    parent = item.get("fields", {}).get("parent")
                    parent_key = parent.get("key") if parent else None
                    if parent_key:
                        counts[parent_key] += 1

                next_page_token = data.get("nextPageToken")
                if not next_page_token or data.get("isLast") is True:
                    break

        return dict(counts)


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


_UNSET = object()


def _strip_order_by(jql: str | None) -> str | None:
    if not jql:
        return jql
    return re.sub(r"\s+ORDER\s+BY\s+.+$", "", jql, flags=re.IGNORECASE).strip()
