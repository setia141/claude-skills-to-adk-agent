"""
agents/fixer.py — Targeted runtime-error fixer.

Takes a single generated file (tools.py or agent.py), a runtime error/traceback
the user got when running the agent locally, and the ADR for context.

Returns the corrected file content with the minimal change needed to fix the error.
Does NOT regenerate the whole pipeline — only patches the specific broken file.
"""

import json
import logging
from agents.claude_sdk import ask, load_prompt, strip_fences, MODEL_LIGHT

log = logging.getLogger(__name__)

FIXER_SYSTEM = load_prompt("fixer")


def run_fixer(file_content: str, filename: str, error_text: str, adr: dict) -> str:
    """
    Fix a runtime error in a generated file.

    Args:
        file_content: Current content of tools.py or agent.py.
        filename:     "tools.py" or "agent.py" (for context in the prompt).
        error_text:   The traceback / error message the user pasted.
        adr:          The ADR so Claude preserves the architecture.

    Returns:
        Complete corrected Python source as a string.
    """
    log.info(f"[fixer] Fixing {filename} — error length={len(error_text)} chars")

    user = (
        f"## File: {filename}\n"
        f"{file_content}\n\n"
        f"## Runtime Error\n"
        f"{error_text}\n\n"
        f"## ADR (architecture to preserve)\n"
        f"{json.dumps(adr, indent=2)}"
    )

    raw = ask(FIXER_SYSTEM, user, tag="fixer", model=MODEL_LIGHT)
    fixed = strip_fences(raw)

    log.info(f"[fixer] {filename} fixed — {len(fixed)} chars")
    return fixed
