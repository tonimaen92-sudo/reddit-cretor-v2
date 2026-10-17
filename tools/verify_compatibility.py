"""Static verification for legacy wrappers and root batch launchers."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXPECTED_BATCHES = {
    "poster.bat",
    "run_creator.bat",
    "run.creator.bat",
    "START CREATOR PANEL.bat",
    "run_qa_scroll.bat",
}


def wrapper_target(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dispatch"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
    raise ValueError("missing dispatch target")


def main() -> int:
    errors: list[str] = []
    wrappers = [path for path in (ROOT / "misc").glob("*.py") if path.name != "_compat.py"]
    for wrapper in sorted(wrappers):
        try:
            target = wrapper_target(wrapper)
            if importlib.util.find_spec(target) is None:
                errors.append(f"{wrapper.name}: target not found: {target}")
        except Exception as exc:
            errors.append(f"{wrapper.name}: {exc}")

    actual_batches = {path.name for path in ROOT.glob("*.bat")}
    missing_batches = EXPECTED_BATCHES - actual_batches
    if missing_batches:
        errors.append("missing batch files: " + ", ".join(sorted(missing_batches)))
    for batch_name in sorted(EXPECTED_BATCHES & actual_batches):
        text = (ROOT / batch_name).read_text(encoding="utf-8-sig")
        for relative in re.findall(r"%~dp0([^\"\r\n]+\.py)", text, flags=re.I):
            if not (ROOT / relative.replace("\\", "/")).exists():
                errors.append(f"{batch_name}: missing target {relative}")

    if errors:
        print("COMPATIBILITY FAIL")
        for error in errors:
            print("- " + error)
        return 1
    print(f"COMPATIBILITY PASS: {len(wrappers)} wrappers, {len(EXPECTED_BATCHES)} batches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
