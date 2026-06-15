"""
agents/verifier.py — Syntax-check generated code (server-side, safe).

Only AST parsing is done here. Running generated code (pytest) is intentionally
NOT done on the server — the generated agent may invoke subprocess calls, make
network requests, or write files. Users run tests locally via run_tests.sh after
downloading the project.
"""

import ast
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _syntax_check(path: Path) -> list[str]:
    """Return list of error strings; empty list means clean."""
    try:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        return []
    except SyntaxError as e:
        return [f"{path.name} line {e.lineno}: {e.msg}"]
    except Exception as e:
        return [f"{path.name}: {e}"]


def run_verifier(out_dir: Path) -> dict:
    """
    Syntax-check tools.py and agent.py via AST parsing.

    Args:
        out_dir: Project directory containing tools.py, agent.py

    Returns:
        {
          "syntax_ok":     bool,
          "syntax_errors": list[str],
        }
    """
    result: dict = {
        "syntax_ok": True,
        "syntax_errors": [],
    }

    for fname in ("tools.py", "agent.py"):
        fpath = out_dir / fname
        if fpath.exists():
            result["syntax_errors"].extend(_syntax_check(fpath))

    result["syntax_ok"] = len(result["syntax_errors"]) == 0
    log.info(
        "[verifier] syntax: " + ("OK" if result["syntax_ok"]
        else f"FAILED — {result['syntax_errors']}")
    )
    return result
