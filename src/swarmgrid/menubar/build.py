"""Build script to package SwarmGrid as a macOS .app bundle.

Uses PyInstaller to create a self-contained application in dist/SwarmGrid.app.
Run: python -m swarmgrid.menubar.build
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LAUNCHER = Path(__file__).resolve().parent / "launcher.py"
RESOURCES_DIR = PROJECT_ROOT / "resources"
DIST_DIR = PROJECT_ROOT / "dist"


def build():
    """Run PyInstaller to create the .app bundle."""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller"],
        check=True,
        capture_output=True,
    )

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "SwarmGrid",
        "--windowed",
        "--onedir",
        "--osx-bundle-identifier", "com.swarmgrid.agent",
        "--paths", str(PROJECT_ROOT / "src"),
        "--hidden-import", "rumps",
        "--hidden-import", "swarmgrid",
        "--hidden-import", "swarmgrid.agent",
        "--hidden-import", "swarmgrid.agent.daemon",
        "--hidden-import", "swarmgrid.agent.worker",
        "--hidden-import", "swarmgrid.agent.registration",
        "--hidden-import", "swarmgrid.agent.credential_store",
        "--hidden-import", "swarmgrid.agent.session_manager",
        "--hidden-import", "swarmgrid.agent.heartbeat",
        "--hidden-import", "swarmgrid.menubar",
        "--hidden-import", "swarmgrid.menubar.app",
        "--noconfirm",
    ]

    # Add icon if available
    icon = RESOURCES_DIR / "icon_connected.png"
    if icon.exists():
        cmd.extend(["--icon", str(icon)])

    # Add resources data
    if RESOURCES_DIR.exists():
        cmd.extend(["--add-data", f"{RESOURCES_DIR}:resources"])

    cmd.append(str(LAUNCHER))

    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)

    app_path = DIST_DIR / "SwarmGrid.app"
    if app_path.exists():
        print(f"Built: {app_path}")
    else:
        print("Build failed — no .app produced", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    build()
