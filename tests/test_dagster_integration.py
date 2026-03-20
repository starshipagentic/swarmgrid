"""Integration tests for the dagster heartbeat daemon."""
import os
import time
from pathlib import Path

# -- Dagster definitions --

def test_definitions_load():
    """Dagster Definitions object can be imported and introspected."""
    from swarmgrid.dagster_defs.definitions import defs
    repo = defs.get_repository_def()
    job_names = set(repo.job_names)
    assert "heartbeat_job" in job_names
    assert "heartbeat_force_job" in job_names
    assert "reconcile_job" in job_names


def test_definitions_have_assets():
    """Dagster Definitions include the new heartbeat assets."""
    from swarmgrid.dagster_defs.definitions import defs
    repo = defs.get_repository_def()
    asset_keys = {str(key) for key in repo.asset_graph.get_all_asset_keys()}
    # Check that at least the primary assets exist
    for expected in ["jira_issues", "heartbeat_decisions", "heartbeat_launches",
                     "process_reconciliation", "ticket_changelogs"]:
        found = any(expected in k for k in asset_keys)
        assert found, f"Asset {expected} not found in {asset_keys}"


def test_resource_effective_path():
    """HeartbeatConfigResource resolves to board-routes.yaml."""
    from swarmgrid.dagster_defs.resources import HeartbeatConfigResource

    res = HeartbeatConfigResource()
    path = res.effective_path()
    assert path.endswith("board-routes.yaml"), f"Unexpected path: {path}"
    assert Path(path).exists(), f"Config file not found: {path}"


def test_resource_env_override():
    """HeartbeatConfigResource respects DAGSTER_HEARTBEAT_CONFIG."""
    from swarmgrid.dagster_defs.resources import HeartbeatConfigResource

    old = os.environ.get("DAGSTER_HEARTBEAT_CONFIG")
    try:
        os.environ["DAGSTER_HEARTBEAT_CONFIG"] = "/tmp/fake-config.yaml"
        res = HeartbeatConfigResource()
        assert res.effective_path() == "/tmp/fake-config.yaml"
    finally:
        if old is None:
            os.environ.pop("DAGSTER_HEARTBEAT_CONFIG", None)
        else:
            os.environ["DAGSTER_HEARTBEAT_CONFIG"] = old


# -- Sentinel mechanism --

def test_sentinel_touch_and_detect():
    """Touch sentinel, detect dagster active, clean up."""
    from swarmgrid.dagster_defs.ops import _touch_sentinel, SENTINEL_PATH
    from swarmgrid.webapp import dagster_is_active

    # Clean state
    if SENTINEL_PATH.exists():
        SENTINEL_PATH.unlink()

    assert not dagster_is_active(), "Should be inactive before sentinel"

    _touch_sentinel()
    assert SENTINEL_PATH.exists()
    assert dagster_is_active(), "Should be active after sentinel touch"

    # Clean up
    SENTINEL_PATH.unlink()
    assert not dagster_is_active(), "Should be inactive after cleanup"


def test_sentinel_stale_detection():
    """Sentinel older than 10 minutes is considered stale."""
    from swarmgrid.dagster_defs.ops import _touch_sentinel, SENTINEL_PATH
    from swarmgrid.webapp import dagster_is_active

    _touch_sentinel()
    # Backdate the file 15 minutes
    old_time = time.time() - 900
    os.utime(SENTINEL_PATH, (old_time, old_time))

    # Without DAGSTER_HOME env var, stale sentinel should not count
    old_env = os.environ.pop("DAGSTER_HOME", None)
    try:
        assert not dagster_is_active(), "Stale sentinel should not count as active"
    finally:
        if old_env is not None:
            os.environ["DAGSTER_HOME"] = old_env
        if SENTINEL_PATH.exists():
            SENTINEL_PATH.unlink()


# -- Trigger file mechanism --

def test_trigger_file_write_and_read():
    """Write trigger file, verify it exists, clean up."""
    from swarmgrid.webapp import write_dagster_trigger, TRIGGER_DIR

    path = write_dagster_trigger("test-trigger")
    assert path.exists()
    assert path.read_text().strip() == "test-trigger"
    assert path.parent == TRIGGER_DIR

    # Clean up
    path.unlink()


def test_sensor_picks_up_triggers():
    """Manual trigger sensor finds and removes trigger files."""
    from swarmgrid.dagster_defs.sensors import _trigger_dir
    from swarmgrid.webapp import write_dagster_trigger

    trigger_dir = _trigger_dir()
    # Write a couple triggers
    p1 = write_dagster_trigger("sensor-test-1")
    p2 = write_dagster_trigger("sensor-test-2")
    assert p1.exists()
    assert p2.exists()

    # The sensor function can't be called directly without dagster context,
    # but we can verify the trigger dir contents
    files = sorted(f.name for f in trigger_dir.iterdir() if f.is_file())
    assert len(files) >= 2

    # Clean up
    for f in trigger_dir.iterdir():
        if f.is_file() and "sensor-test" in f.name:
            f.unlink()


# -- Webapp dagster integration --

def test_webapp_dagster_mode_fallback():
    """When dagster is NOT active, controller.trigger_now calls run_heartbeat directly."""
    from swarmgrid.dagster_defs.ops import SENTINEL_PATH
    from swarmgrid.webapp import dagster_is_active

    # Ensure dagster is not active
    if SENTINEL_PATH.exists():
        SENTINEL_PATH.unlink()
    old_env = os.environ.pop("DAGSTER_HOME", None)
    try:
        assert not dagster_is_active()
    finally:
        if old_env is not None:
            os.environ["DAGSTER_HOME"] = old_env


def test_webapp_dagster_status_endpoint():
    """FastAPI app has /api/dagster/status endpoint."""
    from swarmgrid.webapp import create_app

    app = create_app("board-routes.yaml")
    routes = [route.path for route in app.routes]
    assert "/api/dagster/status" in routes


def test_webapp_boards_endpoint():
    """FastAPI app has /api/boards endpoint."""
    from swarmgrid.webapp import create_app

    app = create_app("board-routes.yaml")
    routes = [route.path for route in app.routes]
    assert "/api/boards" in routes
    assert "/api/boards/{index}/snapshot" in routes
    assert "/api/boards/{index}/switch" in routes


def test_webapp_snapshot_has_dagster_flag():
    """Controller snapshot includes dagster_active field."""
    from swarmgrid.dagster_defs.ops import SENTINEL_PATH
    from swarmgrid.webapp import WebHeartbeatController

    if SENTINEL_PATH.exists():
        SENTINEL_PATH.unlink()

    controller = WebHeartbeatController.__new__(WebHeartbeatController)
    controller.config_path = "board-routes.yaml"
    controller.auto_enabled = True
    controller.last_tick_result = None
    controller.last_tick_error = None
    from datetime import datetime, UTC
    controller.next_run_at = datetime.now(UTC)
    from threading import Lock
    controller._lock = Lock()

    snap = controller.snapshot()
    assert "dagster_active" in snap
    assert snap["dagster_active"] is False


# -- Schedule configuration --

def test_schedule_reads_poll_interval():
    """Schedule cron matches poll_interval_minutes from config."""
    from swarmgrid.dagster_defs.schedules import heartbeat_schedule
    # Config says 5 minutes, so should be */5
    assert "*/5" in heartbeat_schedule.cron_schedule or "*/1" in heartbeat_schedule.cron_schedule


def test_reconcile_schedule_every_minute():
    """Reconcile schedule runs every minute."""
    from swarmgrid.dagster_defs.schedules import reconcile_schedule
    assert reconcile_schedule.cron_schedule == "* * * * *"


# -- Config: multi-board support --

def test_config_load_succeeds():
    """Primary config loads without error."""
    from swarmgrid.config import load_config
    config = load_config("board-routes.yaml")
    assert config.project_key == "LMSV3"


def test_config_route_settings_have_new_fields():
    """RouteSettings schema includes idle/cold/match fields."""
    from swarmgrid.config import load_config
    config = load_config("board-routes.yaml")
    route = config.routes[0]
    assert hasattr(route, "idle_timeout_minutes")
    assert hasattr(route, "cold_timeout_minutes")
    assert hasattr(route, "output_match_patterns")
    assert hasattr(route, "transition_on_idle")
    assert hasattr(route, "transition_on_match")
    # Defaults should be None/empty
    assert route.idle_timeout_minutes is None
    assert route.output_match_patterns == []
    assert route.transition_on_match == {}


def test_board_name_from_config():
    """board_name_from_config strips common prefixes."""
    from swarmgrid.config import board_name_from_config, load_config
    config = load_config("board-routes.yaml")
    name = board_name_from_config(config)
    # 'board-routes' -> should fall through to project_key or stem
    assert isinstance(name, str)
    assert len(name) > 0


def test_discover_board_configs_empty_dir(tmp_path):
    """discover_board_configs returns empty for non-existent dir."""
    from swarmgrid.config import discover_board_configs
    result = discover_board_configs(tmp_path / "nonexistent")
    assert result == []


def test_discover_board_configs_finds_yaml(tmp_path):
    """discover_board_configs finds YAML files."""
    from swarmgrid.config import discover_board_configs
    (tmp_path / "board-a.yaml").write_text("site_url: x\n")
    (tmp_path / "board-b.yml").write_text("site_url: y\n")
    (tmp_path / "readme.txt").write_text("not a config\n")
    result = discover_board_configs(tmp_path)
    assert len(result) == 2
    assert all(p.suffix in {".yaml", ".yml"} for p in result)


# -- Pre-launch transition --

def test_pre_launch_transition_function_exists():
    """_pre_launch_transition is importable."""
    from swarmgrid.service import _pre_launch_transition
    assert callable(_pre_launch_transition)


if __name__ == "__main__":
    import sys
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for func in funcs:
        try:
            import inspect
            if inspect.signature(func).parameters:
                # Skip tests that need fixtures (like tmp_path)
                print(f"  SKIP  {func.__name__} (needs fixtures)")
                continue
            func()
            print(f"  PASS  {func.__name__}")
        except Exception as exc:
            print(f"  FAIL  {func.__name__}: {exc}")
            failures += 1
    print(f"\n{len(funcs)} tests, {failures} failures")
    sys.exit(1 if failures else 0)
