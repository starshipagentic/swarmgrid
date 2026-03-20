"""Dagster jobs combining heartbeat ops."""
from __future__ import annotations

from dagster import job

from .ops import heartbeat_force_tick_op, heartbeat_tick_op, reconcile_op


@job(
    description="Scheduled heartbeat: fetch Jira issues, plan, launch, reconcile.",
)
def heartbeat_job():
    heartbeat_tick_op()


@job(
    description="Manually-triggered heartbeat with force_reconsider=True.",
)
def heartbeat_force_job():
    heartbeat_force_tick_op()


@job(
    description="Lightweight reconciliation of running processes (no Jira fetch).",
)
def reconcile_job():
    reconcile_op()
