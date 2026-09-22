"""
setup_and_run.py — One-click setup & launcher for the Lenovo ASP Portal.

Steps performed automatically:
  1. Create .venv (skipped if it already exists)
  2. Install / update packages from requirements.txt
  3. Launch run.py inside the virtual environment

Usage:
    python setup_and_run.py
"""

import os
import subprocess
import sys
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / ".venv"

# On Windows the Python executable lives under Scripts/, on Unix under bin/
if sys.platform == "win32":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
    VENV_PIP    = VENV_DIR / "Scripts" / "pip.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"
    VENV_PIP    = VENV_DIR / "bin" / "pip"

REQUIREMENTS = BASE_DIR / "requirements.txt"
RUN_SCRIPT   = BASE_DIR / "run.py"

# ── Shared environment ────────────────────────────────────────────────────────
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(cmd: list, **kwargs):
    """Run a command and raise on failure."""
    print(f"\n▶  {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, env=ENV, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)


def step_create_venv():
    if VENV_DIR.exists():
        print(f"\n✔  Virtual environment already exists at {VENV_DIR}")
        return
    print(f"\n── Creating virtual environment at {VENV_DIR} ──")
    run([sys.executable, "-m", "venv", str(VENV_DIR)])


def step_install_requirements():
    if not REQUIREMENTS.exists():
        print(f"\n⚠  {REQUIREMENTS} not found — skipping pip install.")
        return
    print("\n── Installing / updating dependencies ──")
    run([str(VENV_PIP), "install", "-r", str(REQUIREMENTS)])


def step_run_app():
    if not RUN_SCRIPT.exists():
        print(f"\n✘  {RUN_SCRIPT} not found — cannot start the app.")
        sys.exit(1)
    print("\n── Starting the application ──")
    run([str(VENV_PYTHON), str(RUN_SCRIPT)])


def main():
    print("=" * 55)
    print("  Lenovo ASP Portal — Setup & Run")
    print("=" * 55)

    step_create_venv()
    step_install_requirements()
    step_run_app()


if __name__ == "__main__":
    main()
