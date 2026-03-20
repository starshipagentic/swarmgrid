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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "heartbeat-once":
        config_paths = _collect_config_paths(args)
        results = []
        for cfg_path in config_paths:
            result = run_heartbeat(cfg_path)
            results.append(result)
        # Single board -> flat output; multi-board -> list
        output = results[0] if len(results) == 1 else results
        print(json.dumps(output, indent=2))
        return 0

    if args.command == "status":
        config_paths = _collect_config_paths(args)
        results = []
        for cfg_path in config_paths:
            result = get_status(cfg_path)
            result["config_path"] = cfg_path
            results.append(result)
        output = results[0] if len(results) == 1 else results
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

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
