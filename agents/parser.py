"""
agents/parser.py

Parses a skill into structured JSON.
Supports two input modes:
  1. skill_text only  — plain SKILL.md text (backward compatible)
  2. skill_bundle     — dict with SKILL.md + all referenced scripts resolved inline

Output format: <json> block for metadata + <file name="step_N.code"> blocks for code.
Code is never embedded inside JSON — eliminates all JSON escaping failures.
"""

import logging
import os
import re
from agents.claude_sdk import ask, load_prompt, parse_file_blocks, parse_json_block, MODEL_MEDIUM

log = logging.getLogger(__name__)

PARSE_SYSTEM = load_prompt("parse_skill")

# Extensions treated as executable scripts (copied verbatim to output project)
_SCRIPT_EXTS = {
    ".sh", ".bash", ".zsh", ".fish",
    ".py",
    ".js", ".ts", ".mjs", ".cjs",
    ".rb", ".go", ".rs",
    ".ps1", ".cmd", ".bat",
}

_EXT_LANG = {
    ".py": "python", ".sh": "bash", ".bash": "bash",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".md": "markdown", ".txt": "text",
}


def _is_script(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in _SCRIPT_EXTS


def _fenced(filename: str, content: str) -> str:
    lang = _EXT_LANG.get(os.path.splitext(filename)[1].lower(), "text")
    return f"\n\n<!-- companion: {filename} -->\n```{lang}\n{content}\n```\n"


def _resolve_skill_bundle(skill_text: str, extra_files: dict[str, str]) -> str:
    """
    Inject all companion files into the SKILL.md before parsing.

    Pass 1 — any file referenced in the skill text (by full relative path,
             ./relative path, or bare filename) is inlined at its first
             point of reference.
    Pass 2 — every remaining companion file is appended in a Companion Files
             section so nothing is silently lost.
    """
    if not extra_files:
        return skill_text

    resolved = skill_text
    injected: set[str] = set()

    for filename, content in extra_files.items():
        basename = os.path.basename(filename)
        matched = next(
            (ref for ref in (filename, f"./{filename}", basename) if ref in resolved),
            None,
        )
        if matched:
            resolved = re.sub(
                re.escape(matched),
                f"{matched}{_fenced(filename, content)}",
                resolved,
                count=1,
            )
            injected.add(filename)
            log.info(f"[parser] Inlined referenced file: {filename} ({len(content)} chars)")

    remaining = {f: c for f, c in extra_files.items() if f not in injected}
    if remaining:
        resolved += "\n\n---\n## Companion Files\n"
        for filename, content in remaining.items():
            resolved += _fenced(filename, content)
            log.info(f"[parser] Appended companion file: {filename} ({len(content)} chars)")

    return resolved


def parse_skill(skill_text: str, extra_files: dict[str, str] | None = None) -> dict:
    """
    Parse a SKILL.md into structured JSON.

    Uses <json> + <file> block format — step code is never embedded inside
    JSON, so escaping failures (control characters, delimiter mismatches) are
    impossible regardless of what the skill code contains.

    Args:
        skill_text:   Raw SKILL.md content.
        extra_files:  Optional dict of {filename: content} for all companion
                      files in the skill folder.
    """
    log.info("[parser] Parsing skill…")
    extra = extra_files or {}

    full_text = _resolve_skill_bundle(skill_text, extra)
    raw = ask(PARSE_SYSTEM, f"Parse this skill:\n\n{full_text}", tag="parse_skill", model=MODEL_MEDIUM)

    # Extract metadata from <json> block (no code fields inside)
    try:
        parsed = parse_json_block(raw)
    except Exception as e:
        raise ValueError(f"[parser] Failed to extract <json> block: {e}\nRaw start: {raw[:400]}")

    # Merge step code from <file name="step_N.code"> blocks
    blocks = parse_file_blocks(raw)
    for step in parsed.get("steps", []):
        key = f"step_{step.get('id', 0)}.code"
        step["code"] = blocks.get(key, "")
        if not step["code"]:
            log.warning(f"[parser] No code block found for step {step.get('id')} ({step.get('title')})")

    # Attach raw script files so the generator can copy them to output/scripts/
    parsed["script_files"] = {k: v for k, v in extra.items() if _is_script(k)}

    log.info(
        f"[parser] '{parsed.get('name')}': {len(parsed.get('steps', []))} steps, "
        f"{len(extra)} companion files, {len(parsed['script_files'])} scripts attached"
    )
    return parsed
