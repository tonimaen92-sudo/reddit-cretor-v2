"""Install the current-user Windows logon task for crash/restart recovery."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import winreg

from friend_poster.core.paths import PROJECT_ROOT


def main() -> int:
    project = PROJECT_ROOT
    python = project / ".venv" / "Scripts" / "pythonw.exe"
    if not python.exists():
        python = Path(sys.executable)
    watchdog = project / "misc" / "creator_watchdog.py"
    task_command = f'"{python}" "{watchdog}"'
    result = subprocess.run(
        [
            "schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN",
            "DataLost Creator Recovery", "/TR", task_command, "/RL", "LIMITED",
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        # Some managed Windows profiles deny Task Scheduler writes even for
        # LIMITED current-user tasks. HKCU Run is the non-elevated equivalent
        # and remains scoped to this signed-in user.
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            winreg.SetValueEx(
                key, "DataLost Creator Recovery", 0, winreg.REG_SZ, task_command
            )
        print(
            "Task Scheduler denied the current-user task; installed the same "
            "watchdog in the current-user logon Run key."
        )
        return 0
    print("Installed current-user DataLost Creator Recovery logon task.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
