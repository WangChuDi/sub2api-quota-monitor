from pathlib import Path
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
add_data = f"{ROOT / 'app' / 'html'}{os.pathsep}html"

subprocess.run(
    [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "sub2api-quota-monitor",
        "--add-data",
        add_data,
        str(ROOT / "app" / "server.py"),
    ],
    cwd=ROOT,
    check=True,
)
