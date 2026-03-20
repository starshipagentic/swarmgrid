"""Dagster sensors for manual trigger and health checks."""
from __future__ import annotations

from pathlib import Path

from dagster import RunRequest, SensorEvaluationContext, SensorResult, sensor

from .jobs import heartbeat_force_job


def _trigger_dir() -> Path:
    """Directory where the web UI drops trigger files."""
    project_root = Path(__file__).resolve().parents[3]
    return project_root / "var" / "dagster-trigger"


@sensor(
    job=heartbeat_force_job,
    minimum_interval_seconds=5,
    description="Watch var/dagster-trigger/ for manual 'run now' requests from the web UI.",
)
def manual_trigger_sensor(context: SensorEvaluationContext) -> SensorResult:
    trigger_dir = _trigger_dir()
    if not trigger_dir.exists():
        return SensorResult(run_requests=[])

    requests: list[RunRequest] = []
    for trigger_file in sorted(trigger_dir.iterdir()):
        if not trigger_file.is_file():
            continue
        run_key = f"manual-{trigger_file.stem}"
        context.log.info(f"Found trigger file: {trigger_file.name}, creating run {run_key}")
        requests.append(RunRequest(run_key=run_key))
        # Remove the trigger file so it doesn't fire again
        try:
            trigger_file.unlink()
        except OSError:
            pass

    return SensorResult(run_requests=requests)
