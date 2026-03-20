"""Dagster ops wrapping the existing heartbeat service functions."""

from pathlib import Path

from dagster import OpExecutionContext, op

from .resources import HeartbeatConfigResource

SENTINEL_PATH = Path(__file__).resolve().parents[3] / "var" / "dagster" / "daemon_active"


def _touch_sentinel() -> None:
    """Update the sentinel file so the web server knows dagster is driving."""
    SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENTINEL_PATH.write_text("active\n", encoding="utf-8")


@op(
    description="Run one full heartbeat tick: fetch issues, plan decisions, launch, reconcile.",
)
def heartbeat_tick_op(context: OpExecutionContext, config_resource: HeartbeatConfigResource) -> dict:
    from ..service import run_heartbeat

    _touch_sentinel()
    config_path = config_resource.effective_path()
    context.log.info(f"Running heartbeat tick with config: {config_path}")

    try:
        result = run_heartbeat(config_path)
        context.log.info(
            f"Heartbeat complete: {result['issue_count']} issues, "
            f"{result['decision_count']} decisions, "
            f"{result['launched_count']} launched"
        )
        _touch_sentinel()
        return result
    except Exception as exc:
        context.log.error(f"Heartbeat tick failed: {exc}")
        raise


@op(
    description="Run one full heartbeat tick with force_reconsider=True (manual trigger).",
)
def heartbeat_force_tick_op(context: OpExecutionContext, config_resource: HeartbeatConfigResource) -> dict:
    from ..service import run_heartbeat

    _touch_sentinel()
    config_path = config_resource.effective_path()
    context.log.info(f"Running FORCED heartbeat tick with config: {config_path}")

    try:
        result = run_heartbeat(config_path, force_reconsider=True)
        context.log.info(
            f"Forced heartbeat complete: {result['issue_count']} issues, "
            f"{result['decision_count']} decisions, "
            f"{result['launched_count']} launched"
        )
        _touch_sentinel()
        return result
    except Exception as exc:
        context.log.error(f"Forced heartbeat tick failed: {exc}")
        raise


@op(
    description="Reconcile process states (check tmux sessions, PIDs) without running a full heartbeat.",
)
def reconcile_op(context: OpExecutionContext, config_resource: HeartbeatConfigResource) -> dict:
    from ..config import load_config
    from ..runner import reconcile_processes
    from ..service import reconcile_runs
    from ..state import StateStore

    _touch_sentinel()
    config_path = config_resource.effective_path()
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)

    context.log.info("Reconciling process states...")
    reconcile_processes(store)
    reconciled = reconcile_runs(config, store)
    context.log.info(f"Reconciliation done: {len(reconciled)} runs finalized")
    _touch_sentinel()
    return {"reconciled_count": len(reconciled)}
