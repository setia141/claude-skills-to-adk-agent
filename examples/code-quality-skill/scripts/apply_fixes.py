#!/usr/bin/env python3
"""
Apply automated formatting fixes (black + isort) to a Python repository.
Prints a JSON summary of what was changed.

Usage: python apply_fixes.py <repo_path>
"""
import json
import subprocess
import sys
from pathlib import Path


def run_tool(cmd: list[str], cwd: str) -> dict:
    """Run a formatting tool and return a result dict."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=60,
        )
        return {
            "tool":   cmd[0],
            "ok":     r.returncode == 0,
            "output": (r.stdout + r.stderr)[:400].strip(),
        }
    except FileNotFoundError:
        return {"tool": cmd[0], "ok": False, "output": f"{cmd[0]} not installed"}
    except subprocess.TimeoutExpired:
        return {"tool": cmd[0], "ok": False, "output": "timed out after 60s"}


def main() -> None:
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    repo_path = Path(repo).resolve()

    if not repo_path.exists():
        print(json.dumps({"error": f"path not found: {repo}"}))
        sys.exit(1)

    results = []

    # isort — fix import ordering
    results.append(run_tool(
        ["python", "-m", "isort", str(repo_path), "--profile", "black"],
        cwd=str(repo_path),
    ))

    # black — auto-format
    results.append(run_tool(
        ["python", "-m", "black", str(repo_path), "--quiet"],
        cwd=str(repo_path),
    ))

    applied = [r["tool"] for r in results if r["ok"]]
    failed  = [{"tool": r["tool"], "reason": r["output"]} for r in results if not r["ok"]]

    print(json.dumps({
        "fixes_applied": applied,
        "failed":        failed,
        "total":         len(applied),
    }, indent=2))


if __name__ == "__main__":
    main()
