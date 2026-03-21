from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .menu import ensure_setup, run_menu
from .service import get_status, run_heartbeat
from .ui_v2 import run_console_v2
from .webapp import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarmgrid")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default="board-routes.yaml",
        help="Path to the heartbeat config YAML file (primary board).",
    )
    common.add_argument(
        "--configs-dir",
        default=None,
        help="Directory containing board config YAML files (multi-board mode).",
    )
    subparsers.add_parser(
        "heartbeat-once",
        parents=[common],
        help="Run one Jira polling tick.",
    )
    heartbeat_parser = subparsers.add_parser(
        "heartbeat",
        parents=[common],
        help="Run the heartbeat loop continuously (polls Jira, launches agents).",
    )
    heartbeat_parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Poll interval in seconds (default: from config, typically 240s).",
    )
    heartbeat_parser.add_argument(
        "--background",
        action="store_true",
        help="Run heartbeat in a background tmux session (survives terminal close).",
    )
    subparsers.add_parser(
        "stop",
        help="Stop the background heartbeat.",
    )
    subparsers.add_parser(
        "status",
        parents=[common],
        help="Show local heartbeat state.",
    )
    subparsers.add_parser(
        "menu",
        parents=[common],
        help="Launch the interactive operator menu.",
    )
    subparsers.add_parser(
        "menu2",
        parents=[common],
        help="Launch the V2 page-based operator console.",
    )
    subparsers.add_parser(
        "setup",
        parents=[common],
        help="Run the interactive setup wizard.",
    )
    web_parser = subparsers.add_parser(
        "web",
        parents=[common],
        help="Launch the web dashboard.",
    )
    web_parser.add_argument("--host", default="127.0.0.1", help="Bind host for the web UI.")
    web_parser.add_argument("--port", type=int, default=8787, help="Bind port for the web UI.")

    hub_parser = subparsers.add_parser(
        "hub",
        parents=[common],
        help="Hub commands (start, stop, status).",
    )
    hub_sub = hub_parser.add_subparsers(dest="hub_command", required=True)
    hub_sub.add_parser("start", help="Start the hub tmux session with upterm.")
    hub_sub.add_parser("stop", help="Stop the hub tmux session.")
    hub_sub.add_parser("status", help="Show hub status.")

    agent_parser = subparsers.add_parser(
        "agent",
        parents=[common],
        help="Start the edge agent daemon.",
    )
    agent_parser.add_argument(
        "--server",
        default="ssh://uptermd.upterm.dev:22",
        help="Upterm relay server.",
    )
    agent_parser.add_argument(
        "--github-user",
        action="append",
        dest="github_users",
        help="Restrict SSH access to these GitHub users (repeatable).",
    )
    agent_parser.add_argument(
        "--background",
        action="store_true",
        help="Start agent in background (don't block).",
    )

    menubar_parser = subparsers.add_parser(
        "menubar",
        parents=[common],
        help="Launch the macOS menu bar app (wraps the agent daemon).",
    )
    menubar_parser.add_argument(
        "--server",
        default="ssh://uptermd.upterm.dev:22",
        help="Upterm relay server.",
    )
    menubar_parser.add_argument(
        "--github-user",
        action="append",
        dest="github_users",
        help="Restrict SSH access to these GitHub users (repeatable).",
    )

    return parser


def _collect_config_paths(args: argparse.Namespace) -> list[str]:
    """Return all board config paths from CLI args."""
    from .config import discover_board_configs

    paths: list[str] = [args.config]
    if args.configs_dir:
        for extra in discover_board_configs(args.configs_dir):
            resolved = str(extra)
            if resolved not in paths:
                paths.append(resolved)
    return paths


def _resolve_config(config_arg: str) -> str:
    """Resolve config path, checking common locations if not found."""
    p = Path(config_arg)
    if p.exists():
        return str(p)
    # Check common locations
    candidates = [
        Path.home() / ".swarmgrid" / config_arg,
        Path.home() / "clients" / "swarmgrid" / config_arg,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return config_arg  # let it fail with the original path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, 'config'):
        args.config = _resolve_config(args.config)

    if args.command == "heartbeat-once":
        config_paths = _collect_config_paths(args)
        results = []
        for cfg_path in config_paths:
            result = run_heartbeat(cfg_path)
            results.append(result)
        output = results[0] if len(results) == 1 else results
        # Print human-readable summary
        if isinstance(output, dict):
            issues = output.get("issue_count", 0)
            launched = output.get("launched_count", 0)
            statuses = output.get("watched_statuses", [])
            print(f"Heartbeat tick: {issues} issue{'s' if issues != 1 else ''} in {', '.join(statuses)}, {launched} launched")
            for d in output.get("decisions", []):
                if d.get("should_launch"):
                    print(f"  Launched: {d['issue_key']} ({d['action']})")
            for r in output.get("reconciled", []):
                print(f"  Reconciled: {r['issue_key']} -> {r['state']} (transition: {r.get('transition_target', '—')})")
            print()
        print(json.dumps(output, indent=2))
        return 0

    if args.command == "stop":
        import subprocess
        result = subprocess.run(
            ["tmux", "kill-session", "-t", "swarmgrid-heartbeat"],
            check=False, capture_output=True,
        )
        if result.returncode == 0:
            print("Heartbeat stopped.")
        else:
            print("No heartbeat running.")
        return 0

    if args.command == "heartbeat":
        import logging
        import shutil
        import subprocess
        from .agent.heartbeat import run_heartbeat_loop
        import signal
        import threading

        config_paths = _collect_config_paths(args)

        # Background mode — launch in tmux
        if args.background:
            if not shutil.which("tmux"):
                print("Error: tmux required for --background mode")
                return 1
            session_name = "swarmgrid-heartbeat"
            subprocess.run(["tmux", "kill-session", "-t", session_name], check=False, capture_output=True)
            abs_config = str(Path(config_paths[0]).resolve())
            interval_flag = f" --interval {args.interval}" if args.interval else ""
            # Use the swarmgrid script (installed alongside python in the venv)
            sg_bin = str(Path(sys.executable).parent / "swarmgrid")
            cmd = f"{sg_bin} heartbeat --config {abs_config}{interval_flag}; echo 'Heartbeat stopped.'; sleep 999"
            subprocess.run([
                "tmux", "new-session", "-d", "-s", session_name, "-c", str(Path(abs_config).parent), cmd
            ], check=True)
            print(f"Heartbeat running in background (tmux session: {session_name})")
            print(f"  Attach: tmux attach -t {session_name}")
            print(f"  Stop:   tmux kill-session -t {session_name}")
            return 0

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )

        stop_event = threading.Event()

        def _handle_signal(sig, frame):
            print("\nStopping heartbeat...")
            stop_event.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        print(f"SwarmGrid heartbeat starting (config: {config_paths[0]})")
        print("Press Ctrl-C to stop.\n")
        run_heartbeat_loop(
            config_paths[0],
            poll_interval=args.interval,
            stop_event=stop_event,
        )
        return 0

    if args.command == "status":
        config_paths = _collect_config_paths(args)
        results = []
        for cfg_path in config_paths:
            result = get_status(cfg_path)
            result["config_path"] = cfg_path
            results.append(result)
        output = results[0] if len(results) == 1 else results
        # Print human-readable summary
        if isinstance(output, dict):
            daemon = output.get("heartbeat_daemon", "unknown")
            source = output.get("route_source", "yaml")
            statuses = output.get("watched_statuses", [])
            running = output.get("running_count", 0)
            routes = output.get("routes", [])
            print(f"SwarmGrid Status")
            print(f"  Heartbeat daemon: {daemon}")
            print(f"  Route source: {source}")
            print(f"  Watching: {', '.join(statuses) if statuses else '(none)'}")
            print(f"  Running sessions: {running}")
            last_tick = output.get("last_tick")
            if last_tick:
                print(f"  Last tick: {last_tick['at']} ({last_tick['issues']} issues, {last_tick['launched']} launched)")
            if routes:
                print(f"  Routes:")
                for r in routes:
                    armed = "Armed" if r.get("enabled") else "Off"
                    print(f"    {r['status']} -> {r['action']} [{armed}]")
                    if r.get("transition_on_launch"):
                        print(f"      Launch:{r['transition_on_launch']} Success:{r.get('transition_on_success','—')} Fail:{r.get('transition_on_failure','—')}")
            print()
        print(json.dumps(output, indent=2))
        return 0

    if args.command == "menu":
        return run_menu(args.config)

    if args.command == "menu2":
        return run_console_v2(args.config)

    if args.command == "setup":
        ensure_setup(args.config, force_prompt=True)
        return 0

    if args.command == "web":
        import uvicorn

        config_paths = _collect_config_paths(args)
        uvicorn.run(
            create_app(config_paths[0], extra_config_paths=config_paths[1:]),
            host=args.host,
            port=args.port,
            log_level="info",
        )
        return 0

    if args.command == "hub":
        from .hub import start_hub, stop_hub, hub_status
        from .config import load_config
        from .operator_settings import load_operator_settings

        if args.hub_command == "start":
            config = load_config(args.config)
            settings = load_operator_settings(config.operator_settings_path)
            server = settings.upterm_server or "ssh://uptermd.upterm.dev:22"
            result = start_hub(upterm_server=server)
            print(json.dumps(result, indent=2))
            return 0

        if args.hub_command == "stop":
            stopped = stop_hub()
            print(json.dumps({"stopped": stopped}, indent=2))
            return 0

        if args.hub_command == "status":
            status = hub_status()
            print(json.dumps(status, indent=2))
            return 0

    if args.command == "agent":
        from .agent.daemon import start_agent

        result = start_agent(
            config_path=args.config,
            upterm_server=args.server,
            github_users=args.github_users,
            foreground=not args.background,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "menubar":
        from .menubar.app import run_menubar_app

        run_menubar_app(
            config_path=args.config,
            upterm_server=args.server,
            github_users=args.github_users,
        )
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
