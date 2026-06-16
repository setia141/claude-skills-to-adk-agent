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
    ResultMessage,
    TextBlock,
    query,
)

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a system prompt from prompts/<name>.md — fails loudly if missing."""
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()


_SDK_TIMEOUT = 1800  # 30 minutes — safety net for genuine hangs only

# Lightweight model for simple structured/extraction tasks (query_planner, validator, test_gen, fixer).
MODEL_LIGHT = "claude-haiku-4-5-20251001"
# Mid-tier model for tasks that need format discipline but not deep reasoning (parse_skill).
MODEL_MEDIUM = "claude-sonnet-4-6"


async def _ask_async(
    system: str, user: str, max_turns: int = 3, model: str | None = None
) -> tuple[str, dict]:
    """
    Async core: run one query() call, collect all AssistantMessage TextBlocks.
    allowed_tools=[] ensures Claude does not try to use filesystem/bash tools —
    we only want a text response, not agentic tool use.
    max_turns=3 (not 1) gives the SDK room to handle any tool-rejection
    handshake internally without raising "Reached maximum number of turns".
    Times out after 30 minutes to prevent infinite hangs on a dead claude process.
    Returns (text, usage_dict) where usage_dict has keys from ResultMessage.
    """
    options = ClaudeAgentOptions(
        system_prompt=system,
        max_turns=max_turns,
        allowed_tools=[],          # text-only — no tool execution
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob"],
        model=model,               # None → CLI default (best model)
    )
    full_text = ""
    usage: dict = {}
    with anyio.fail_after(_SDK_TIMEOUT):
        async for message in query(prompt=user, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_text += block.text
            elif isinstance(message, ResultMessage):
                usage = {
                    "usage": message.usage,
                    "model_usage": message.model_usage,
                    "total_cost_usd": message.total_cost_usd,
                    "duration_ms": message.duration_ms,
                    "duration_api_ms": message.duration_api_ms,
                    "num_turns": message.num_turns,
                    "stop_reason": message.stop_reason,
                }
    return full_text, usage


def ask(
    system: str,
    user: str,
    tag: str = "",
    max_turns: int = 3,
    model: str | None = None,
) -> str:
    """
    Synchronous wrapper around _ask_async.
    Safe to call from Flask route handlers (which are sync).
    Uses anyio.run() to drive the async event loop.
    Raises TimeoutError if Claude does not respond within 30 minutes.

    Pass model=MODEL_LIGHT for structured/extraction tasks to cut cost ~10×.
    Leave model=None (default) to use the CLI's configured best model.
    """
    if tag:
        log.info(f"[{tag}] → Claude Agent SDK (model={model or 'default'})")
    try:
        result, usage = anyio.run(_ask_async, system, user, max_turns, model)
    except TimeoutError:
        log.error(f"[{tag}] Claude SDK timed out after {_SDK_TIMEOUT // 60} minutes — claude process may be hung")
        raise
    if tag:
        _log_usage(tag, len(result), usage)
    return result


def _log_usage(tag: str, chars: int, usage: dict) -> None:
    """Log token usage and cost from a ResultMessage to make high-usage steps visible."""
    parts = [f"[{tag}] ← {chars} chars"]
    u = usage.get("usage") or {}
    mu = usage.get("model_usage") or {}
    tokens_logged = False
    # Prefer model_usage (per-model breakdown) if present, else flat usage
    if mu:
        for mdl, stats in mu.items():
            if isinstance(stats, dict):
                inp = stats.get("input_tokens", 0)
                out = stats.get("output_tokens", 0)
                cache_read = stats.get("cache_read_input_tokens", 0)
                cache_write = stats.get("cache_creation_input_tokens", 0)
                parts.append(f"{mdl}: in={inp} out={out} cache_read={cache_read} cache_write={cache_write}")
                tokens_logged = True
    elif u:
        inp = u.get("input_tokens", 0)
        out = u.get("output_tokens", 0)
        cache_read = u.get("cache_read_input_tokens", 0)
        cache_write = u.get("cache_creation_input_tokens", 0)
        parts.append(f"in={inp} out={out} cache_read={cache_read} cache_write={cache_write}")
        tokens_logged = True
    if not tokens_logged and (u or mu):
        # Raw dump to discover actual key names from this CLI version
        log.debug(f"[{tag}] raw usage={u!r} model_usage={mu!r}")
    cost = usage.get("total_cost_usd")
    if cost is not None:
        parts.append(f"cost=${cost:.4f}")
    dur = usage.get("duration_api_ms")
    if dur is not None:
        parts.append(f"api={dur}ms")
    log.info(" | ".join(parts))


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


def ask_json(system: str, user: str, tag: str = "", model: str | None = None) -> Any:
    """
    Like ask() but strips markdown fences and parses JSON.

    Handles three common Claude JSON output problems:
    1. Response wrapped in ```json ... ``` fences — stripped before parsing.
    2. Literal control characters inside string values (common when Claude
       embeds Python code) — escaped to \\n / \\t / \\uXXXX.
    3. Valid JSON followed by extra explanatory text — truncated at the JSON
       boundary (json.JSONDecodeError.pos points to the first extra byte).
    """
    raw = ask(system, user, tag, model=model)
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
