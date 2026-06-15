"""
app.py — Flask web UI for the Skill → ADK platform (v4).

New in v4:
  - Skill folder zip upload (SKILL.md + scripts resolved)
  - Agent intent input field
  - ADK choice selector (extensible for future ADKs)
  - Fixed ADR rendering (defensive, all fields optional)
  - /rag/status and /rag/build routes
"""

import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import venv as _venv_mod
import zipfile
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from agents import parse_skill, run_architect, run_generator, run_validator, run_test_generator, run_verifier, run_fixer
from agents.rag import build_index, index_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
JOBS: dict[str, dict] = {}

# Persistent session storage — survives server restarts
SESSIONS_DIR = Path(__file__).parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Smoke test runner script ──────────────────────────────────────────────────
# Written to a temp file in the project dir, run as a subprocess, then deleted.
# Emits newline-delimited JSON so the server can re-emit structured SSE events.
# Model-agnostic: uses whatever env vars the user provides.
_SMOKETEST_SCRIPT = '''\
import sys, json, asyncio
sys.path.insert(0, ".")

def emit(event, **kw):
    print(json.dumps({"event": event, **kw}), flush=True)

async def main():
    # 1. Import tools
    try:
        import tools
        emit("step", message="tools.py — imported OK")
    except Exception as e:
        emit("error", message=f"tools.py import failed: {e}"); return

    # 2. Import agent
    try:
        import agent as agent_mod
        emit("step", message="agent.py — imported OK")
    except Exception as e:
        emit("error", message=f"agent.py import failed: {e}"); return

    # 3. Find root_agent
    root_agent = getattr(agent_mod, "root_agent", None)
    if root_agent is None:
        emit("error", message="root_agent not found in agent.py"); return
    emit("step", message=f"root_agent — {getattr(root_agent, 'name', type(root_agent).__name__)}")

    # 4. Run via ADK Runner (model-agnostic — uses env vars for credentials)
    try:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        svc    = InMemorySessionService()
        runner = Runner(agent=root_agent, app_name="smoketest", session_service=svc)
        sess   = await svc.create_session(app_name="smoketest", user_id="tester")
        msg    = types.Content(role="user",
                               parts=[types.Part(text="Hello, what can you help me with?")])

        reply = ""
        async for ev in runner.run_async(user_id="tester", session_id=sess.id, new_message=msg):
            if hasattr(ev, "is_final_response") and ev.is_final_response():
                if ev.content and ev.content.parts:
                    reply = ev.content.parts[0].text or ""
                break

        emit("reply", text=reply[:1000])
    except ImportError as e:
        emit("warn", message=f"ADK Runner not available ({e}) — import OK, runtime skipped")
    except Exception as e:
        emit("error", message=f"Runtime error: {e}")

asyncio.run(main())
'''

# Supported ADKs — extensible list (only Google ADK active now)
SUPPORTED_ADKS = [
    {"id": "google_adk",    "label": "Google ADK",      "active": True},
    {"id": "langchain",     "label": "LangChain",        "active": False},
    {"id": "autogen",       "label": "AutoGen",          "active": False},
    {"id": "bedrock",       "label": "AWS Bedrock",      "active": False},
    {"id": "semantic_kernel","label": "Semantic Kernel", "active": False},
]

# ── RAG bootstrap ─────────────────────────────────────────────────────────────
def _bootstrap_rag():
    try:
        result = build_index()
        log.info(f"[startup] RAG ready — {result['status']}, {result['chunks']} chunks")
    except FileNotFoundError as e:
        log.warning(f"[startup] {e}")
    except Exception as e:
        log.error(f"[startup] RAG build failed: {e}")

threading.Thread(target=_bootstrap_rag, daemon=True).start()


# ── Helpers ───────────────────────────────────────────────────────────────────

def sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


def unpack_skill_zip(zip_bytes: bytes) -> list[tuple[str, dict[str, str]]]:
    """
    Generic skill zip unpacker. Returns one (skill_md_text, companion_files) tuple
    per SKILL.md found (case-insensitive), at any nesting depth.

    Companion files are every readable text file in the same directory tree as
    the SKILL.md — no extension allowlist. Binary files are silently skipped.

    Handles: flat zips, single-subdir zips, multi-skill bundles, deeply nested trees.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        non_dirs = [n for n in zf.namelist() if not n.endswith("/")]

        # Strip one shared top-level prefix so bundled zips (skill-bundle/a/SKILL.md)
        # are normalised to (a/SKILL.md) before processing.
        prefix = ""
        if non_dirs and all("/" in n for n in non_dirs):
            roots = {n.split("/")[0] for n in non_dirs}
            if len(roots) == 1:
                prefix = roots.pop() + "/"

        file_map: dict[str, str] = {}
        for name in non_dirs:
            rel = name[len(prefix):]
            if not rel:
                continue
            try:
                raw = zf.read(name)
                text = raw.decode("utf-8", errors="replace")
                # Skip files that are overwhelmingly non-text (>5% replacement chars)
                if text.count("�") / max(len(text), 1) >= 0.05:
                    continue
                file_map[rel] = text
            except Exception:
                continue

    skill_rels = [r for r in file_map if Path(r).name.upper() == "SKILL.MD"]

    results = []
    for skill_rel in skill_rels:
        skill_dir = "/".join(skill_rel.split("/")[:-1])  # "" = root, "sub" = subdirectory
        skill_text = file_map[skill_rel]
        companions: dict[str, str] = {}
        for rel, content in file_map.items():
            if rel == skill_rel:
                continue
            file_dir = "/".join(rel.split("/")[:-1])
            in_scope = (
                skill_dir == ""                         # root skill — include everything
                or file_dir == skill_dir               # same directory
                or file_dir.startswith(skill_dir + "/") # subdirectory of skill
            )
            if in_scope:
                companions[rel] = content
        results.append((skill_text, companions))

    return results


def make_requirements(all_deps: list[str]) -> str:
    base = ["google-adk>=0.3.0", "claude-agent-sdk>=0.1.0", "anyio>=4.0.0", "pytest>=7.0.0"]
    return "\n".join(dict.fromkeys(base + all_deps))


_RUN_TESTS_SH = """\
#!/usr/bin/env bash
# Run the generated test suite locally.
# The server intentionally does NOT run tests — this script is for you to run
# after downloading and reviewing the project.
set -e
echo "Installing dependencies..."
pip install -r requirements.txt
echo ""
echo "Running tests..."
pytest tests/ -v --tb=short
"""

_RUN_TESTS_BAT = """\
@echo off
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Running tests...
pytest tests\\ -v --tb=short
"""


def make_readme(parsed_skills, adr, project_name):
    d = adr.get("decisions", {})
    topology  = d.get("topology", {}).get("choice", "N/A")
    mem_st    = d.get("memory", {}).get("short_term", {}).get("choice", "N/A")
    mem_lt    = d.get("memory", {}).get("long_term", {}).get("choice", "N/A")
    error     = d.get("error_strategy", {}).get("default", "N/A")
    hitl      = d.get("hitl_points", [])
    skills    = "\n".join(f"- **{s['name']}**: {s['description']}" for s in parsed_skills)
    hitl_txt  = "\n".join(f"- {h['skill']} / {h['step']}: {h['reason']}" for h in hitl) or "None"
    intent    = adr.get("agent_intent_summary", "")
    target    = adr.get("adk_target", "Google ADK")

    return f"""# {project_name}

{intent}

Generated {target} agent from {len(parsed_skills)} Claude Skill(s).

## Architecture (from ADR)
| Decision | Choice |
|---|---|
| Topology | {topology} |
| Memory short-term | {mem_st} |
| Memory long-term | {mem_lt} |
| Error strategy | {error} |
| HITL points | {len(hitl)} |

## Included Skills
{skills}

## Human-in-the-Loop Points
{hitl_txt}

## Setup
```bash
pip install -r requirements.txt
claude login
```

## Run
```bash
adk run agent.py
```

## Test
```bash
pytest tests/
```
"""


# ── Permission gate helper ────────────────────────────────────────────────────

def _perm_helper(job_id: str, filename: str, content: str, reason: str = ""):
    """
    Sub-generator for per-file approval gates.
    Yields one SSE 'permission_required' event, then blocks until the user
    clicks Approve / Reject in the UI (or times out after 5 minutes).

    Usage inside stream():
        approved = yield from _perm_helper(job_id, "tools.py", tools_py, "reason")
    """
    rid = str(uuid.uuid4())
    ev = threading.Event()
    JOBS[job_id]["perm_events"][rid] = ev
    yield sse("permission_required", {
        "request_id": rid,
        "job_id":      job_id,
        "filename":    filename,
        "content":     content[:8000],
        "total_lines": content.count("\n") + 1,
        "total_chars": len(content),
        "reason":      reason,
    })
    approved = ev.wait(timeout=900)
    result   = bool(approved and JOBS[job_id]["perm_results"].pop(rid, False))
    JOBS[job_id]["perm_events"].pop(rid, None)
    return result


def _cmd_helper(job_id: str, command: str, reason: str = ""):
    """
    Sub-generator for shell-command approval gates.
    Identical pattern to _perm_helper but sends a 'cmd_required' event.
    User sees a popup: "Run $ <command>?" with Approve / Reject.
    Times out after 2 minutes (commands shouldn't sit unanswered long).
    """
    rid = str(uuid.uuid4())
    ev  = threading.Event()
    JOBS[job_id]["perm_events"][rid] = ev
    yield sse("cmd_required", {
        "request_id": rid,
        "job_id":     job_id,
        "command":    command,
        "reason":     reason,
    })
    approved = ev.wait(timeout=900)
    result   = bool(approved and JOBS[job_id]["perm_results"].pop(rid, False))
    JOBS[job_id]["perm_events"].pop(rid, None)
    return result


def _run_subprocess(cmd: list, cwd: Path):
    """
    Sub-generator that runs a subprocess and yields 'run_output' SSE events
    for each line of combined stdout+stderr.
    Returns (returncode, full_output) via StopIteration value.
    """
    lines = []
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for raw in proc.stdout:
            line = raw.rstrip()
            lines.append(line)
            yield sse("run_output", {"line": line})
        proc.wait()
        returncode = proc.returncode
    except FileNotFoundError:
        msg = f"Command not found: {cmd[0]}"
        lines.append(msg)
        yield sse("run_output", {"line": msg})
        returncode = 1
    return returncode, "\n".join(lines)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", adks=SUPPORTED_ADKS)


@app.route("/rag/status")
def rag_status():
    return jsonify(index_status())


@app.route("/rag/build", methods=["POST"])
def rag_build():
    force = (request.get_json(silent=True) or {}).get("force", False)
    try:
        return jsonify(build_index(force=force))
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/adks")
def adks():
    """Return available ADK options."""
    return jsonify(SUPPORTED_ADKS)


@app.route("/parse", methods=["POST"])
def parse_route():
    """
    Parse skills. Accepts two formats:
      - multipart/form-data: skill zip files (field: skill_zips[])
      - application/json:    {"skills": ["...text..."]}  (backward compat)
    """
    parsed, errors = [], []

    if request.content_type and "multipart" in request.content_type:
        # Zip upload path
        zip_files = request.files.getlist("skill_zips[]")
        text_skills = request.form.getlist("skill_texts[]")

        for zf in zip_files:
            try:
                skills = unpack_skill_zip(zf.read())
                if not skills:
                    errors.append(f"{zf.filename}: no SKILL.md found in zip")
                    continue
                for skill_text, extra_files in skills:
                    parsed.append(parse_skill(skill_text, extra_files))
            except Exception as e:
                errors.append(f"{zf.filename}: {e}")

        for text in text_skills:
            if text.strip():
                try:
                    parsed.append(parse_skill(text))
                except Exception as e:
                    errors.append(str(e))
    else:
        # JSON path (backward compat)
        data = request.get_json()
        for i, text in enumerate([s for s in data.get("skills", []) if s.strip()]):
            try:
                parsed.append(parse_skill(text))
            except Exception as e:
                errors.append(f"Skill {i+1}: {e}")

    if not parsed:
        return jsonify({"error": "All skills failed to parse.", "details": errors}), 400

    return jsonify({"parsed": parsed, "errors": errors})


@app.route("/architect", methods=["POST"])
def architect_route():
    data         = request.get_json()
    parsed       = data.get("parsed_skills", [])
    agent_intent = data.get("agent_intent", "")
    adk_choice   = data.get("adk_choice", "Google ADK")

    if not parsed:
        return jsonify({"error": "No parsed skills."}), 400
    try:
        adr = run_architect(parsed, agent_intent=agent_intent, adk_choice=adk_choice)
        return jsonify({"adr": adr})
    except Exception as e:
        log.exception("Architect failed")
        return jsonify({"error": str(e)}), 500


@app.route("/generate", methods=["POST"])
def generate_route():
    data         = request.get_json()
    parsed       = data.get("parsed_skills", [])
    adr          = data.get("adr", {})
    project_name = data.get("project_name", "my-adk-agent").strip() or "my-adk-agent"

    if not parsed or not adr:
        return jsonify({"error": "Missing parsed_skills or adr."}), 400

    job_id   = str(uuid.uuid4())
    tmp_root = Path(tempfile.mkdtemp())
    out_dir  = tmp_root / project_name
    out_dir.mkdir()
    hitl_event = threading.Event()
    JOBS[job_id] = {
        "tmp_root":      tmp_root,
        "project_name":  project_name,
        "adr":           adr,
        "files":         {},
        "hitl_event":    hitl_event,
        "hitl_approved": False,
        "perm_events":   {},   # request_id -> threading.Event
        "perm_results":  {},   # request_id -> bool
    }

    def stream():
        yield sse("job_id", {"job_id": job_id, "project_name": project_name})

        # ── Pre-step: copy skill scripts to output/scripts/ ───────────────────
        script_paths: list[str] = []
        scripts_dir = out_dir / "scripts"
        for skill in parsed:
            for rel, content in skill.get("script_files", {}).items():
                scripts_dir.mkdir(exist_ok=True)
                fname = Path(rel).name
                (scripts_dir / fname).write_text(content, encoding="utf-8")
                script_paths.append(f"scripts/{fname}")
                yield sse("file", {"filename": f"scripts/{fname}", "content": content})
        if script_paths:
            yield sse("info", {
                "message": f"Copied {len(script_paths)} skill script(s) → output: {', '.join(script_paths)}"
            })

        # ── Step 1: Generate ──────────────────────────────────────────────────
        yield sse("stage", {"name": "generate", "message": "Phase 1: Claude planning ADK doc queries… Phase 2: retrieving + generating code…"})
        try:
            gen           = run_generator(parsed, adr, script_paths=script_paths or None)
            tools_py      = gen["tools_py"]
            agent_py      = gen["agent_py"]
            agent_prompts = gen.get("agent_prompts", {})
            gen_extra     = gen.get("extra_files", {})
            rag_used      = gen.get("rag_used", False)
            rag_chars     = gen.get("rag_chars_injected", 0)
            rag_queries   = gen.get("rag_queries", [])

            prompt_note = f", {len(agent_prompts)} prompt file(s)" if agent_prompts else ""
            yield sse("stage_done", {
                "name": "generate",
                "rag_queries": rag_queries,
                "agent_prompts": list(agent_prompts.keys()),
                "message": (
                    f"✓ Code generated — RAG: {rag_chars} chars from {len(rag_queries)} Claude-planned queries{prompt_note}"
                    if rag_used else
                    f"✓ Code generated (no RAG){prompt_note}"
                ),
            })
        except Exception as e:
            yield sse("error", {"message": f"Generator failed: {e}"}); return

        # ── Permission: tools.py ───────────────────────────────────────────────
        approved = yield from _perm_helper(job_id, "tools.py", tools_py, "Generated by Claude (generator)")
        if not approved:
            yield sse("error", {"message": "Pipeline cancelled — tools.py rejected"}); return
        (out_dir / "tools.py").write_text(tools_py, encoding="utf-8")
        yield sse("file", {"filename": "tools.py", "content": tools_py})
        yield sse("info", {"message": "✓ tools.py written to disk"})

        # ── Permission: agent.py ───────────────────────────────────────────────
        approved = yield from _perm_helper(job_id, "agent.py", agent_py, "Generated by Claude (generator)")
        if not approved:
            yield sse("error", {"message": "Pipeline cancelled — agent.py rejected"}); return
        (out_dir / "agent.py").write_text(agent_py, encoding="utf-8")
        yield sse("file", {"filename": "agent.py", "content": agent_py})
        yield sse("info", {"message": "✓ agent.py written to disk"})

        # ── Permission: prompts/ ───────────────────────────────────────────────
        if agent_prompts:
            combined = "\n\n".join(f"# {k}\n{v}" for k, v in agent_prompts.items())
            approved = yield from _perm_helper(
                job_id, f"prompts/ ({len(agent_prompts)} file(s))", combined,
                "Generated by Claude (generator)"
            )
            if not approved:
                yield sse("error", {"message": "Pipeline cancelled — prompt files rejected"}); return
            for fname, content in agent_prompts.items():
                dest = out_dir / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                yield sse("file", {"filename": fname, "content": content})
            log.info(f"[generate] Wrote {len(agent_prompts)} agent prompt file(s): {list(agent_prompts)}")
            yield sse("info", {"message": f"✓ {len(agent_prompts)} prompt file(s) written"})

        # ── Permission: Dockerfile + .dockerignore ────────────────────────────
        if gen_extra:
            combined = "\n\n".join(f"# {k}\n{v}" for k, v in gen_extra.items())
            approved = yield from _perm_helper(
                job_id, f"Dockerfile + .dockerignore", combined,
                "Generated by Claude (generator)"
            )
            if approved:
                for fname, content in gen_extra.items():
                    (out_dir / fname).write_text(content, encoding="utf-8")
                    yield sse("file", {"filename": fname, "content": content})
                yield sse("info", {"message": f"✓ Dockerfile + .dockerignore written"})
            else:
                yield sse("warning", {"message": "⚠ Dockerfile skipped — rejected by user"})

        # ── Step 2: Validate ──────────────────────────────────────────────────
        yield sse("stage", {"name": "validate", "message": "Validating code against ADR…"})
        try:
            val            = run_validator(tools_py, agent_py, adr)
            tools_py_fixed = val.get("tools_py_fixed")
            agent_py_fixed = val.get("agent_py_fixed")
            code_issues    = val.get("code_issues", [])
            adr_violations = val.get("adr_violations", [])

            yield sse("validation_result", {
                "code_issues": code_issues, "adr_violations": adr_violations,
                "is_valid": val.get("is_valid", True),
                "adr_conformant": val.get("adr_conformant", True),
                "message": f"✓ {len(code_issues)} code issues + {len(adr_violations)} ADR violations found"
            })

            # Permission gate only when validator actually changed a file
            if tools_py_fixed:
                approved = yield from _perm_helper(
                    job_id, "tools.py (validator fixes)", tools_py_fixed,
                    f"Validator made {len(code_issues)} fix(es)"
                )
                if approved:
                    tools_py = tools_py_fixed
                    (out_dir / "tools.py").write_text(tools_py, encoding="utf-8")
                    yield sse("file", {"filename": "tools.py", "content": tools_py})
                    yield sse("info", {"message": "✓ tools.py validator fixes applied"})
                else:
                    yield sse("warning", {"message": "⚠ tools.py validator fixes rejected — keeping original"})

            if agent_py_fixed:
                approved = yield from _perm_helper(
                    job_id, "agent.py (validator fixes)", agent_py_fixed,
                    f"Validator made {len(adr_violations)} fix(es)"
                )
                if approved:
                    agent_py = agent_py_fixed
                    (out_dir / "agent.py").write_text(agent_py, encoding="utf-8")
                    yield sse("file", {"filename": "agent.py", "content": agent_py})
                    yield sse("info", {"message": "✓ agent.py validator fixes applied"})
                else:
                    yield sse("warning", {"message": "⚠ agent.py validator fixes rejected — keeping original"})

        except Exception as e:
            yield sse("warning", {"message": f"Validator error: {e}"})

        # ── Step 3: Tests ──────────────────────────────────────────────────────
        yield sse("stage", {"name": "tests", "message": "Generating pytest suite…"})
        try:
            skill_names = ", ".join(p["name"] for p in parsed)
            test_py = run_test_generator(tools_py, adr, skill_names)

            approved = yield from _perm_helper(
                job_id, "tests/test_tools.py", test_py,
                "Generated by Claude (test generator)"
            )
            if approved:
                (out_dir / "tests").mkdir(exist_ok=True)
                (out_dir / "tests" / "test_tools.py").write_text(test_py, encoding="utf-8")
                yield sse("file", {"filename": "tests/test_tools.py", "content": test_py})
                yield sse("stage_done", {"name": "tests", "message": "✓ Tests generated and approved"})
            else:
                yield sse("warning", {"message": "⚠ Tests skipped — user rejected test file"})
        except Exception as e:
            yield sse("warning", {"message": f"Tests skipped: {e}"})

        # ── Step 4: Verify — AST syntax check only (no code execution on server)
        yield sse("stage", {"name": "verify", "message": "Syntax checking generated code…"})
        try:
            verify    = run_verifier(out_dir)
            syntax_ok = verify["syntax_ok"]
            errs      = verify["syntax_errors"]
            ok        = syntax_ok
            yield sse("stage_done" if ok else "stage_warn", {
                "name":    "verify",
                "verify":  verify,
                "message": ("✓ Syntax OK — run run_tests.sh locally to execute tests"
                            if ok else
                            "⚠ Syntax errors: " + "; ".join(errs[:3])),
            })
        except Exception as e:
            yield sse("warning", {"message": f"Verify skipped: {e}"})

        # ── Step 5: HITL gate — pause if ADR has human-in-the-loop points ────────
        hitl_points = adr.get("decisions", {}).get("hitl_points", [])
        if hitl_points:
            yield sse("hitl_required", {
                "job_id":      job_id,
                "hitl_points": hitl_points,
                "message":     f"{len(hitl_points)} step(s) require your approval before packaging",
            })
            # Block this Flask worker thread until UI calls /hitl/approve/<job_id>
            approved = JOBS[job_id]["hitl_event"].wait(timeout=900)
            if not approved:
                yield sse("error", {"message": "HITL approval timed out — pipeline cancelled"}); return
            if not JOBS[job_id].get("hitl_approved"):
                yield sse("hitl_rejected", {"message": "Pipeline cancelled by user"}); return
            yield sse("hitl_done", {"message": "✓ Approved — packaging…"})

        # ── Step 6: Package ────────────────────────────────────────────────────
        yield sse("stage", {"name": "write", "message": "Packaging project files…"})
        try:
            all_deps = [d for p in parsed for d in p.get("external_deps", [])]

            # Write files not yet on disk
            (out_dir / "requirements.txt").write_text(make_requirements(all_deps), encoding="utf-8")
            (out_dir / "README.md").write_text(make_readme(parsed, adr, project_name), encoding="utf-8")
            (out_dir / "adr.json").write_text(json.dumps(adr, indent=2), encoding="utf-8")
            # Local test runner scripts — pytest not run on server (security)
            (out_dir / "run_tests.sh").write_text(_RUN_TESTS_SH, encoding="utf-8")
            (out_dir / "run_tests.bat").write_text(_RUN_TESTS_BAT, encoding="utf-8")

            # Build files dict from everything written to out_dir (scripts/ included)
            all_files: dict[str, str] = {}
            for f in sorted(out_dir.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(out_dir).as_posix()
                    try:
                        all_files[rel] = f.read_text(encoding="utf-8")
                    except Exception:
                        pass

            JOBS[job_id]["files"] = all_files
            yield sse("stage_done", {
                "name":    "write",
                "message": f"✓ {len(all_files)} files packaged",
            })
        except Exception as e:
            yield sse("error", {"message": f"Package failed: {e}"}); return

        yield sse("done", {"message": "Pipeline complete."})

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/files/<job_id>")
def files_route(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify({"error": "Not found"}), 404
    return jsonify(job.get("files", {}))


@app.route("/download/<job_id>")
def download_route(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify({"error": "Not found"}), 404
    tmp_root     = job["tmp_root"]
    project_name = job["project_name"]
    project_dir  = tmp_root / project_name
    zip_path     = tmp_root / f"{project_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in project_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(tmp_root))
    return send_file(zip_path, as_attachment=True,
                     download_name=f"{project_name}.zip", mimetype="application/zip")


def _detect_filename(error: str, known_files: list) -> str:
    """
    Pick the most likely file to fix from a traceback.
    Scans 'File "..."' lines; falls back to tools.py then first known file.
    """
    import re as _re
    for match in _re.finditer(r'File ["\']([^"\']+)["\']', error):
        base = Path(match.group(1)).name
        for f in known_files:
            if Path(f).name == base:
                return f
    for preferred in ("tools.py", "agent.py"):
        if preferred in known_files:
            return preferred
    return known_files[0] if known_files else "tools.py"


@app.route("/fix", methods=["POST"])
def fix_route():
    """
    Fix a runtime error in a generated file.

    Stateless — client sends file contents directly so this works after
    server restarts and session resumes, not just during an active job.

    Body: {
        error:   str,                      # error traceback (required)
        files:   {"tools.py": "...", ...}, # all generated files (required)
        adr:     {},                       # ADR for context (optional)
        job_id:  str,                      # if present, also updates job on disk
    }
    Returns: { fixed, filename, verify }
    """
    data       = request.get_json(silent=True) or {}
    error_text = (data.get("error") or "").strip()
    files      = data.get("files") or {}
    adr        = data.get("adr") or {}
    job_id     = data.get("job_id", "")

    if not error_text:
        return jsonify({"error": "error text is required"}), 400
    if not files:
        return jsonify({"error": "no files provided"}), 400

    filename        = _detect_filename(error_text, list(files.keys()))
    current_content = files.get(filename, "")
    if not current_content:
        return jsonify({"error": f"content for {filename} not found in provided files"}), 400

    try:
        fixed = run_fixer(current_content, filename, error_text, adr)
    except Exception as e:
        log.exception("Fixer failed")
        return jsonify({"error": str(e)}), 500

    # Best-effort: update job on disk if still alive
    verify = {}
    job = JOBS.get(job_id) if job_id else None
    if job:
        job["files"][filename] = fixed
        out_dir = job["tmp_root"] / job["project_name"]
        try:
            dest = out_dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(fixed, encoding="utf-8")
            verify = run_verifier(out_dir)
        except Exception as e:
            log.warning(f"[fix] Could not write to disk: {e}")

    log.info(f"[fix] Fixed {filename} ({len(fixed)} chars), job_alive={bool(job)}")
    return jsonify({"fixed": fixed, "filename": filename, "verify": verify})


@app.route("/hitl/approve/<job_id>", methods=["POST"])
def hitl_approve(job_id):
    """
    Called by the UI when the user approves or cancels the HITL gate.
    Sets hitl_approved and unblocks the generate stream.
    """
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    data = request.get_json(silent=True) or {}
    job["hitl_approved"] = bool(data.get("approved", True))
    event: threading.Event = job.get("hitl_event")
    if event:
        event.set()
    return jsonify({"ok": True, "approved": job["hitl_approved"]})


@app.route("/permission/<request_id>", methods=["POST"])
def permission_route(request_id):
    """Receive user's Approve / Reject decision for a file-write gate."""
    data    = request.get_json(silent=True) or {}
    job_id  = data.get("job_id", "")
    approved = bool(data.get("approved", False))

    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    ev = job.get("perm_events", {}).get(request_id)
    if not ev:
        return jsonify({"error": "Permission request not found or already decided"}), 404

    job["perm_results"][request_id] = approved
    ev.set()
    return jsonify({"ok": True, "approved": approved})


@app.route("/ask", methods=["POST"])
def ask_route():
    """Let the user ask Claude why it generated a specific file."""
    from agents.claude_sdk import ask as claude_ask
    data     = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    context  = (data.get("context") or "")[:6000]
    filename = data.get("filename", "file")

    if not question:
        return jsonify({"error": "question is required"}), 400
    try:
        answer = claude_ask(
            "You are a concise code reviewer. The user is reviewing a file Claude generated "
            "and wants to understand it before approving. Answer clearly and briefly.",
            f"File: {filename}\n\nContent:\n{context}\n\nQuestion: {question}",
            tag="ask",
        )
        return jsonify({"answer": answer})
    except Exception as e:
        log.exception("Ask route failed")
        return jsonify({"error": str(e)}), 500


@app.route("/chat/adr", methods=["POST"])
def chat_adr_route():
    """
    ADR conversational interface — two-phase: respond + optionally propose changes.

    Body:  { current_adr: {}, conversation: [{role, content}], message: "..." }
    Returns: { response: "...", proposed_adr: {}|null }
    """
    from agents.architect import run_adr_chat
    data         = request.get_json(silent=True) or {}
    current_adr  = data.get("current_adr")
    conversation = data.get("conversation") or []
    message      = (data.get("message") or "").strip()

    if not current_adr:
        return jsonify({"error": "current_adr is required"}), 400
    if not message:
        return jsonify({"error": "message is required"}), 400

    try:
        result = run_adr_chat(current_adr, conversation, message)
        return jsonify(result)
    except Exception as e:
        log.exception("ADR chat failed")
        return jsonify({"error": str(e)}), 500


@app.route("/run/<job_id>")
def run_stream(job_id):
    """
    SSE stream that runs sanity checks on the generated project with user approval
    at each shell-command boundary.

    Steps:
      1. pip install -r requirements.txt     (cmd_required popup)
      2. py_compile tools.py + agent.py      (no popup — pure Python, no side effects)
      3. python -c "import tools; import agent"  (cmd_required popup)

    On syntax/import errors: auto-fix loop (Claude fixer → permission_required
    for file write → re-run), up to 3 attempts per step.
    """
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found — pipeline may have expired"}), 404

    out_dir = job["tmp_root"] / job["project_name"]
    adr     = job.get("adr", {})
    python  = sys.executable

    def stream():
        # ── Step 0: Create isolated sandbox venv ─────────────────────────────
        # pip install runs inside this venv so the Flask app's packages are
        # never touched and the reloader never sees venv changes.
        sandbox_dir    = out_dir / "_venv"
        sandbox_python = python  # fallback if venv creation fails
        yield sse("run_output", {"line": "Creating isolated sandbox environment…"})
        try:
            _venv_mod.EnvBuilder(with_pip=True, clear=True).create(str(sandbox_dir))
            _bin = "Scripts" if sys.platform == "win32" else "bin"
            _exe = "python.exe" if sys.platform == "win32" else "python"
            sandbox_python = str(sandbox_dir / _bin / _exe)
            job["sandbox_python"] = sandbox_python
            yield sse("run_output", {"line": "  ✓ Sandbox venv ready"})
        except Exception as e:
            yield sse("run_output", {"line": f"  ! Sandbox venv failed: {e} — using app venv"})

        # ── Step 1: pip install ───────────────────────────────────────────────
        yield sse("run_stage", {"name": "install", "message": "Step 1: Installing dependencies…"})
        req_file = out_dir / "requirements.txt"
        if req_file.exists():
            approved = yield from _cmd_helper(
                job_id, "pip install -r requirements.txt",
                reason="Install the generated agent's Python dependencies (into isolated sandbox venv)",
            )
            if approved:
                rc, _ = yield from _run_subprocess(
                    [sandbox_python, "-m", "pip", "install", "-r", "requirements.txt"],
                    cwd=out_dir,
                )
                if rc == 0:
                    yield sse("run_step_ok",   {"name": "install", "message": "Dependencies installed"})
                else:
                    yield sse("run_step_warn", {"name": "install", "message": f"pip install exited {rc} — continuing anyway"})
            else:
                yield sse("run_step_warn", {"name": "install", "message": "pip install skipped"})
        else:
            yield sse("run_step_ok", {"name": "install", "message": "No requirements.txt — skipping"})

        # ── Step 2: Syntax check (no popup) ───────────────────────────────────
        yield sse("run_stage", {"name": "syntax", "message": "Step 2: Syntax check (py_compile)…"})
        syntax_ok    = True
        syntax_error = ""
        syntax_file  = ""
        for fname in ("tools.py", "agent.py"):
            fpath = out_dir / fname
            if not fpath.exists():
                continue
            r = subprocess.run(
                [python, "-m", "py_compile", str(fpath)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                syntax_ok    = False
                syntax_error = r.stderr.strip()
                syntax_file  = fname
                yield sse("run_output", {"line": f"  ✕ {fname}: {syntax_error}"})
                break
            else:
                yield sse("run_output", {"line": f"  ✓ {fname}"})

        if not syntax_ok:
            for attempt in range(3):
                yield sse("run_output", {"line": f"  Auto-fix attempt {attempt + 1}/3…"})
                content = (out_dir / syntax_file).read_text(encoding="utf-8", errors="replace")
                try:
                    fixed = run_fixer(content, syntax_file, syntax_error, adr)
                except Exception as e:
                    yield sse("run_output", {"line": f"  Fixer error: {e}"}); break

                fix_approved = yield from _perm_helper(
                    job_id, syntax_file, fixed,
                    reason=f"Claude auto-fix for syntax error (attempt {attempt + 1})",
                )
                if not fix_approved:
                    yield sse("run_output", {"line": "  Fix rejected by user"}); break

                (out_dir / syntax_file).write_text(fixed, encoding="utf-8")
                job["files"][syntax_file] = fixed
                yield sse("file", {"filename": syntax_file, "content": fixed})

                r = subprocess.run(
                    [python, "-m", "py_compile", str(out_dir / syntax_file)],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    syntax_ok = True
                    yield sse("run_output", {"line": f"  ✓ {syntax_file} compiles cleanly after fix"})
                    break
                syntax_error = r.stderr.strip()

        if syntax_ok:
            yield sse("run_step_ok",   {"name": "syntax", "message": "Syntax check passed"})
        else:
            yield sse("run_step_error", {"name": "syntax", "message": f"Syntax errors remain in {syntax_file}"})
            yield sse("run_done", {"ok": False}); return

        # ── Step 3: Import test ───────────────────────────────────────────────
        yield sse("run_stage", {"name": "import", "message": "Step 3: Import test…"})
        import_cmd = (
            "import sys; sys.path.insert(0, '.'); "
            "import tools; import agent; "
            "print('root_agent:', getattr(agent, 'root_agent', 'NOT FOUND'))"
        )
        approved = yield from _cmd_helper(
            job_id, "python -c \"import tools; import agent\"",
            reason="Verify all imports resolve and packages are installed",
        )
        import_ok = False
        if approved:
            for attempt in range(3):
                rc, out = yield from _run_subprocess(
                    [sandbox_python, "-c", import_cmd], cwd=out_dir,
                )
                if rc == 0:
                    import_ok = True
                    yield sse("run_step_ok", {"name": "import", "message": "Import test passed"})
                    break

                yield sse("run_output", {"line": f"  Import failed (attempt {attempt + 1}/3)"})
                if attempt >= 2:
                    break

                fname = _detect_filename(out, list(job["files"].keys()))
                content = (out_dir / fname).read_text(encoding="utf-8", errors="replace")
                try:
                    fixed = run_fixer(content, fname, out, adr)
                except Exception as e:
                    yield sse("run_output", {"line": f"  Fixer error: {e}"}); break

                fix_approved = yield from _perm_helper(
                    job_id, fname, fixed,
                    reason=f"Claude auto-fix for import error (attempt {attempt + 1})",
                )
                if not fix_approved:
                    yield sse("run_output", {"line": "  Fix rejected by user"}); break

                (out_dir / fname).write_text(fixed, encoding="utf-8")
                job["files"][fname] = fixed
                yield sse("file", {"filename": fname, "content": fixed})

            if not import_ok:
                yield sse("run_step_error", {"name": "import", "message": "Import test failed after 3 attempts"})
        else:
            yield sse("run_step_warn", {"name": "import", "message": "Import test skipped"})
            import_ok = True

        yield sse("run_done", {"ok": import_ok})

    return Response(
        stream_with_context(stream()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/smoketest/<job_id>", methods=["POST"])
def smoketest_stream(job_id):
    """
    SSE stream that runs a model-agnostic smoke test against the generated agent.

    Client POSTs { env: {"KEY": "VALUE", ...} } with whatever credentials the
    chosen model needs (ANTHROPIC_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY, …).
    Keys are never logged or persisted — they live only in the subprocess env.

    Steps (emitted as structured JSON lines from _SMOKETEST_SCRIPT):
      1. import tools
      2. import agent
      3. locate root_agent
      4. Runner.run_async — send "Hello" message, capture reply
    """
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found — pipeline may have expired"}), 404

    out_dir = job["tmp_root"] / job["project_name"]
    data    = request.get_json(silent=True) or {}
    env_in  = {k.strip(): v.strip() for k, v in (data.get("env") or {}).items() if k and v}

    def stream():
        script_path = out_dir / "_smoketest_runner.py"
        try:
            script_path.write_text(_SMOKETEST_SCRIPT, encoding="utf-8")
        except Exception as e:
            yield sse("smoke_error", {"message": f"Could not write test script: {e}"}); return

        env = {**os.environ, **env_in}
        yield sse("smoke_stage", {"message": "Running smoke test…"})

        smoke_python = job.get("sandbox_python", sys.executable)
        try:
            proc = subprocess.Popen(
                [smoke_python, "-u", str(script_path)],
                cwd=str(out_dir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
            for raw in proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                try:
                    parsed     = json.loads(line)
                    event_type = parsed.pop("event", "output")
                    yield sse("smoke_" + event_type, parsed)
                except Exception:
                    yield sse("smoke_output", {"line": line})
            proc.wait()
            rc = proc.returncode
        except Exception as e:
            yield sse("smoke_error", {"message": str(e)})
            rc = 1
        finally:
            try: script_path.unlink()
            except: pass

        yield sse("smoke_done", {"ok": rc == 0})

    return Response(
        stream_with_context(stream()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_SAFE_ID = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')

def _safe_session_dir(session_id: str):
    """
    Return (sdir, error_response) — sdir is None if the id is unsafe.
    Prevents path traversal: session_id must be alphanumeric/dash/underscore only.
    """
    if not _SAFE_ID.match(session_id):
        return None, (jsonify({"error": "Invalid session id"}), 400)
    sdir = (SESSIONS_DIR / session_id).resolve()
    if not str(sdir).startswith(str(SESSIONS_DIR.resolve())):
        return None, (jsonify({"error": "Invalid session id"}), 400)
    return sdir, None


@app.route("/session/<session_id>", methods=["POST"])
def save_session(session_id):
    """
    Persist generated files for a session so the UI can resume after page refresh.

    Body: { meta: {projectName, phase, ...}, files: {"tools.py": "...", ...} }
    """
    sdir, err = _safe_session_dir(session_id)
    if err: return err

    data = request.get_json(silent=True) or {}
    sdir.mkdir(exist_ok=True)
    (sdir / "meta.json").write_text(
        json.dumps(data.get("meta", {}), indent=2), encoding="utf-8"
    )
    for fname, content in (data.get("files") or {}).items():
        dest = (sdir / fname).resolve()
        if not str(dest).startswith(str(sdir)):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    log.info(f"[session] Saved {len(data.get('files', {}))} files for session {session_id}")
    return jsonify({"ok": True})


@app.route("/session/<session_id>", methods=["GET"])
def load_session(session_id):
    """Return saved session meta + file contents."""
    sdir, err = _safe_session_dir(session_id)
    if err: return err

    if not sdir.exists():
        return jsonify({"error": "Session not found"}), 404
    meta_path = sdir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    files = {}
    for f in sdir.rglob("*"):
        if f.is_file() and f.name != "meta.json":
            rel = str(f.relative_to(sdir)).replace("\\", "/")
            files[rel] = f.read_text(encoding="utf-8", errors="replace")
    return jsonify({"meta": meta, "files": files})


@app.route("/session/<session_id>/download")
def download_session(session_id):
    """Serve a zip of the session's generated files (works even after server restart)."""
    sdir, err = _safe_session_dir(session_id)
    if err: return err

    if not sdir.exists():
        return jsonify({"error": "Session not found"}), 404
    meta_path = sdir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    project_name = meta.get("projectName", "my-adk-agent")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sdir.rglob("*"):
            if f.is_file() and f.name != "meta.json":
                zf.write(f, Path(project_name) / f.relative_to(sdir))
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"{project_name}.zip", mimetype="application/zip")


@app.route("/cleanup/<job_id>", methods=["DELETE"])
def cleanup_route(job_id):
    job = JOBS.pop(job_id, None)
    if job: shutil.rmtree(job["tmp_root"], ignore_errors=True)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True,
            extra_files=[],
            reloader_type="stat",
            exclude_patterns=["venv/*", "__pycache__/*",
                              "adk_chroma_db/*", "sessions/*", "output/*"])
