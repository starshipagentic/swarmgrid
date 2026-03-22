"""Launcher script for PyInstaller — entry point for SwarmGrid.app."""
import sys
import os

# Ensure the package is importable
if getattr(sys, 'frozen', False):
    # Running as PyInstaller bundle
    bundle_dir = sys._MEIPASS
else:
    bundle_dir = os.path.dirname(os.path.abspath(__file__))

from swarmgrid.menubar.app import run_menubar_app
run_menubar_app()
