"""
agents/claude_sdk.py

Shared helper that calls Claude via the Claude Agent SDK (claude-agent-sdk).
No ANTHROPIC_API_KEY needed — uses the bundled Claude CLI auth.

The SDK ships with Claude Code CLI baked in. As long as `claude` is authenticated
on the machine (via `claude login`), this works with zero env var setup.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a system prompt from prompts/<name>.md — fails loudly if missing."""
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()


async def _ask_async(system: str, user: str, max_tokens: int = 8192) -> str:
    """
    Async core: run one query() call, collect all AssistantMessage TextBlocks.
    allowed_tools=[] ensures Claude does not try to use filesystem/bash tools —
    we only want a text response, not agentic tool use.
    """
    options = ClaudeAgentOptions(
        system_prompt=system,
        max_turns=1,
        allowed_tools=[],          # text-only — no tool execution
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob"],
    )
    full_text = ""
    async for message in query(prompt=user, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    full_text += block.text
    return full_text


def ask(system: str, user: str, tag: str = "") -> str:
    """
    Synchronous wrapper around _ask_async.
    Safe to call from Flask route handlers (which are sync).
    Uses anyio.run() to drive the async event loop.
    """
    if tag:
        log.info(f"[{tag}] → Claude Agent SDK")
    result = anyio.run(_ask_async, system, user)
    if tag:
        log.info(f"[{tag}] ← {len(result)} chars")
    return result


def _escape_control_chars(s: str) -> str:
    """
    Walk JSON text and escape any raw control characters (\\x00-\\x1f) that
    appear inside string values.  Claude frequently emits literal newlines/tabs
    when embedding Python code — those are valid in Python but invalid in JSON.

    The scan tracks string/escape context so it never touches structural JSON
    characters (brackets, colons, commas) and is idempotent on well-formed JSON.
    """
    result: list[str] = []
    in_string = False
    escaped = False
    _esc = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for ch in s:
        if escaped:
            result.append(ch)
            escaped = False
        elif in_string and ch == "\\":
            result.append(ch)
            escaped = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ord(ch) < 0x20:
            result.append(_esc.get(ch, f"\\u{ord(ch):04x}"))
        else:
            result.append(ch)
    return "".join(result)


def ask_json(system: str, user: str, tag: str = "") -> Any:
    """
    Like ask() but strips markdown fences and parses JSON.

    Handles three common Claude JSON output problems:
    1. Response wrapped in ```json ... ``` fences — stripped before parsing.
    2. Literal control characters inside string values (common when Claude
       embeds Python code) — escaped to \\n / \\t / \\uXXXX.
    3. Valid JSON followed by extra explanatory text — truncated at the JSON
       boundary (json.JSONDecodeError.pos points to the first extra byte).
    """
    raw = ask(system, user, tag)
    clean = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    clean = _escape_control_chars(clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            log.warning(f"[ask_json:{tag}] trailing text after JSON at pos {e.pos} — truncating")
            return json.loads(clean[: e.pos])
        raise


def strip_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences from a string."""
    return re.sub(r"(^```[\w]*\s*)|(\s*```$)", "", text.strip())


def parse_file_blocks(text: str) -> dict[str, str]:
    """
    Extract <file name="...">content</file> blocks from Claude's response.

    Used for generator and validator output so Claude never has to JSON-encode
    Python source — eliminates the entire class of escaping failures (invalid
    control characters, missing commas, extra data, etc.).
    """
    result: dict[str, str] = {}
    for m in re.finditer(r'<file\s+name="([^"]+)">(.*?)</file>', text, re.DOTALL):
        name    = m.group(1).strip()
        content = m.group(2).lstrip("\n").rstrip("\n")
        result[name] = content
    return result


def parse_json_block(text: str) -> Any:
    """
    Extract and parse the <json>...</json> block from Claude's response.

    Used by the validator, which mixes structured metadata (JSON) with code
    (file blocks) in the same response to avoid embedding code inside JSON.
    Falls back gracefully so a missing block doesn't crash the pipeline.
    """
    m = re.search(r"<json>(.*?)</json>", text, re.DOTALL)
    if not m:
        raise ValueError("No <json> block found in response")
    clean = _escape_control_chars(m.group(1).strip())
    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            return json.loads(clean[: e.pos])
        raise
