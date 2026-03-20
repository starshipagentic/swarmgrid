"""Build script to package SwarmGrid as a macOS .app bundle.

Uses py2app to create a self-contained application in dist/SwarmGrid.app.
Run: python -m swarmgrid.menubar.build
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
APP_SCRIPT = Path(__file__).resolve().parent / "app.py"
RESOURCES_DIR = PROJECT_ROOT / "resources"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


def write_setup_py() -> Path:
    """Generate a temporary setup.py for py2app."""
    setup_path = PROJECT_ROOT / "_menubar_setup.py"

    icon_files = []
    for name in ("icon_connected.png", "icon_disconnected.png"):
        p = RESOURCES_DIR / name
        if p.exists():
            icon_files.append(str(p))

    setup_content = f"""\
from setuptools import setup

APP = [{str(APP_SCRIPT)!r}]
DATA_FILES = []
OPTIONS = {{
    "argv_emulation": False,
    "iconfile": None,
    "plist": {{
        "CFBundleName": "SwarmGrid",
        "CFBundleDisplayName": "SwarmGrid",
        "CFBundleIdentifier": "com.swarmgrid.agent",
        "CFBundleVersion": "1.0.0",
        "LSUIElement": True,  # hide from Dock (menu bar app)
    }},
    "packages": ["swarmgrid", "rumps"],
    "resources": {icon_files!r},
}}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={{"py2app": OPTIONS}},
    setup_requires=["py2app"],
)
"""
    setup_path.write_text(setup_content)
    return setup_path


def build():
    """Run py2app to create the .app bundle."""
    # Ensure py2app is available
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "py2app"],
        check=True,
    )

    setup_py = write_setup_py()

    try:
        subprocess.run(
            [sys.executable, str(setup_py), "py2app"],
            cwd=str(PROJECT_ROOT),
            check=True,
        )
    finally:
        setup_py.unlink(missing_ok=True)

    app_path = DIST_DIR / "SwarmGrid.app"
    if app_path.exists():
        print(f"Built: {app_path}")
    else:
        print("Build failed — no .app produced", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    build()
