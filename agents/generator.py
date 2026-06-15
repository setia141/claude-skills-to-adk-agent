"""
agents/generator.py — Generator, Validator, Test Generator.
Uses Claude Agent SDK + RAG-retrieved ADK doc context.
"""

import json
import logging
from agents.claude_sdk import ask, ask_json, strip_fences, load_prompt, parse_file_blocks, parse_json_block
from agents.rag import retrieve_for_queries

log = logging.getLogger(__name__)

# ── Prompts (loaded from prompts/ directory) ──────────────────────────────────
_GENERATOR_BASE    = load_prompt("generator_base")
_QUERY_PLANNER_SYS = load_prompt("query_planner")
_VALIDATOR_SYS     = load_prompt("validator")
_TEST_SYS          = load_prompt("test_gen")


# ── Query planner ─────────────────────────────────────────────────────────────

def _plan_retrieval_queries(adr: dict) -> list[str]:
    """
    Ask Claude to plan its own retrieval queries from the ADR.

    Claude reads the ADR, reasons about what code it will write, and generates
    targeted search queries — far more precise than hand-crafted heuristics
    because Claude knows which specific API signatures, import paths, and patterns
    it will actually need before writing a single line of code.
    """
    log.info("[generator] Phase 1 — Claude planning retrieval queries from ADR…")
    user = (
        f"Plan retrieval queries for this ADR so you can generate correct Google ADK code:\n\n"
        f"{json.dumps(adr, indent=2)}"
    )
    try:
        queries = ask_json(_QUERY_PLANNER_SYS, user, tag="query_planner")
        if isinstance(queries, list) and queries and all(isinstance(q, str) for q in queries):
            log.info(f"[generator] Claude planned {len(queries)} queries: {queries}")
            return queries
        log.warning(f"[generator] Query planner returned unexpected format: {type(queries)}")
        return []
    except Exception as e:
        log.warning(f"[generator] Query planning failed ({e}) — generator will use base knowledge")
        return []


# ── Generator ─────────────────────────────────────────────────────────────────

def run_generator(
    parsed_skills: list[dict],
    adr: dict,
    script_paths: list[str] | None = None,
) -> dict:
    """
    Two-phase generation:
      Phase 1 — Claude reads the ADR and plans its own retrieval queries.
      Phase 2 — OpenAI embeddings retrieve matching ADK doc chunks from ChromaDB;
                Claude generates tools.py + agent.py with those chunks as ground truth.

    Args:
        parsed_skills: Parsed skill dicts from the parser.
        adr:           Architecture Decision Record from the architect.
        script_paths:  List of relative paths (e.g. ["scripts/deploy.sh"]) for
                       scripts already copied to the output project. Claude is
                       instructed to call these via subprocess instead of
                       reimplementing them.
    """
    topology = adr["decisions"]["topology"]["choice"]
    log.info(f"[generator] topology={topology} — starting two-phase retrieval + generation…")

    # ── Phase 1: Claude plans retrieval queries ────────────────────────────────
    queries = _plan_retrieval_queries(adr)

    # ── Phase 2: OpenAI embeddings retrieve relevant ADK doc chunks ───────────
    adk_context = retrieve_for_queries(queries) if queries else ""
    rag_injected = bool(adk_context)

    # Build system prompt: base + optional bundled-scripts notice + optional RAG docs
    system = _GENERATOR_BASE

    if script_paths:
        scripts_list = "\n".join(f"  - {p}" for p in script_paths)
        system += (
            "\n\n## Bundled Skill Scripts (CRITICAL — do NOT reimplement these)\n"
            "The following scripts from the original skill are already copied into the output "
            "project. Call them via subprocess — never rewrite their logic:\n"
            f"{scripts_list}\n"
            "Reference them as relative paths, e.g.:\n"
            "  subprocess.run(['bash', 'scripts/deploy.sh'], capture_output=True, text=True, check=False)"
        )
        log.info(f"[generator] Injecting {len(script_paths)} bundled script path(s) into prompt")

    if rag_injected:
        log.info(f"[generator] Phase 2 — injecting {len(adk_context)} chars of ADK doc context")
        system += (
            "\n\n## Relevant Google ADK Documentation\n"
            "The sections below were retrieved specifically for this ADR using the queries "
            "you planned in phase 1. They are from the official ADK docs — treat them as "
            "ground truth and follow the exact patterns shown:\n\n"
            + adk_context
        )
    else:
        log.warning("[generator] No RAG context retrieved — generating from base knowledge only")

    user = (
        f"## Parsed Skills\n{json.dumps(parsed_skills, indent=2)}\n\n"
        f"## Architecture Decision Record\n{json.dumps(adr, indent=2)}"
    )

    # Use ask() not ask_json() — Python code inside JSON causes constant escaping failures.
    # Claude returns <file name="...">content</file> blocks which need zero escaping.
    raw    = ask(system, user, tag="generator")
    blocks = parse_file_blocks(raw)

    tools_py = blocks.get("tools.py", "")
    agent_py = blocks.get("agent.py", "")
    if not tools_py or not agent_py:
        raise ValueError(
            f"Generator response missing required files — got: {list(blocks.keys()) or '(none)'}\n"
            f"Raw response start: {raw[:300]}"
        )

    # Collect all prompts/ blocks as the agent prompt files
    agent_prompts = {k: v for k, v in blocks.items() if k.startswith("prompts/")}
    if not agent_prompts:
        log.warning("[generator] No prompts/ blocks found — agent instructions may be hardcoded")

    # Collect extra files: Dockerfile, .dockerignore
    _extra_names = {"Dockerfile", ".dockerignore"}
    extra_files  = {k: v for k, v in blocks.items() if k in _extra_names}

    log.info(
        f"[generator] tools.py={len(tools_py)} chars, agent.py={len(agent_py)} chars, "
        f"agent_prompts={list(agent_prompts)}, extra_files={list(extra_files)}, "
        f"rag_used={rag_injected}, queries={len(queries)}"
    )
    return {
        "tools_py":           tools_py,
        "agent_py":           agent_py,
        "agent_prompts":      agent_prompts,
        "extra_files":        extra_files,
        "rag_used":           rag_injected,
        "rag_chars_injected": len(adk_context),
        "rag_queries":        queries,
    }


def run_validator(tools_py: str, agent_py: str, adr: dict) -> dict:
    """Validate generated code against ADR. Returns fixed files + issue lists."""
    log.info("[validator] Validating code against ADR…")
    user = (
        f"## tools.py\n{tools_py}\n\n"
        f"## agent.py\n{agent_py}\n\n"
        f"## ADR\n{json.dumps(adr, indent=2)}"
    )
    raw = ask(_VALIDATOR_SYS, user, tag="validator")

    # Parse <json> block for structured metadata
    try:
        meta = parse_json_block(raw)
    except Exception as e:
        log.warning(f"[validator] <json> block parse failed ({e}) — assuming clean")
        meta = {"code_issues": [], "adr_violations": [], "is_valid": True, "adr_conformant": True}

    # Parse <file> blocks for corrected code
    blocks = parse_file_blocks(raw)

    result = {
        "code_issues":    meta.get("code_issues", []),
        "adr_violations": meta.get("adr_violations", []),
        "is_valid":       meta.get("is_valid", True),
        "adr_conformant": meta.get("adr_conformant", True),
        "tools_py_fixed": blocks.get("tools.py") or None,
        "agent_py_fixed": blocks.get("agent.py") or None,
    }
    log.info(
        f"[validator] code_issues={len(result['code_issues'])}, "
        f"adr_violations={len(result['adr_violations'])}"
    )
    return result


def run_test_generator(tools_py: str, adr: dict, skill_names: str) -> str:
    """Generate pytest suite informed by tools.py and ADR error strategy."""
    log.info("[test_gen] Generating tests…")
    user = (
        f"Skills: {skill_names}\n\n"
        f"## ADR error_strategy\n{json.dumps(adr['decisions']['error_strategy'], indent=2)}\n\n"
        f"## ADR hitl_points\n{json.dumps(adr['decisions'].get('hitl_points', []), indent=2)}\n\n"
        f"## tools.py\n{tools_py}"
    )
    raw = ask(_TEST_SYS, user, tag="test_gen")
    return strip_fences(raw)
