# CLAUDE.md — Skill → ADK Agent Platform

## What this project is

A Flask web app that converts Claude Skill markdown files into Google ADK agent code.
The pipeline: parse SKILL.md → Architect Agent produces ADR → user reviews/approves →
Generator produces RAG-grounded Python code → Validator → Test Generator → Sanity Check → Download.

All Claude calls go through `claude-agent-sdk` using local CLI auth (`claude login`) — no API key.
OpenAI API is used only for embeddings (`text-embedding-3-small`) to power the RAG index.

---

## Project layout

```
app.py                    Flask backend — all routes and SSE pipeline logic
agents/
  __init__.py             Exports: parse_skill, run_architect, run_adr_chat,
                                   run_generator, run_validator, run_test_generator,
                                   run_verifier, run_fixer
  claude_sdk.py           ask() / ask_json() / parse_file_blocks() wrappers
  rag.py                  ChromaDB index build + retrieval (OpenAI embeddings)
  parser.py               SKILL.md → structured JSON via Claude
  architect.py            ADR generation + ADR Chat (run_architect, run_adr_chat)
  generator.py            run_generator · run_validator · run_test_generator
  fixer.py                run_fixer — rewrites a file given a runtime error
  verifier.py             run_verifier — AST syntax check (py_compile, no execution)
prompts/
  architect.md            Architect Agent system prompt (ADR schema defined here)
  adr_chat.md             ADR Chat system prompt (two-phase: <response> + <proposed_adr>)
  generator_base.md       Generator system prompt (Python-only rule, Dockerfile rule)
  validator.md            Validator system prompt
  test_gen.md             Test generator system prompt
  fixer.md                Fixer system prompt
  parse_skill.md          Parser system prompt
  query_planner.md        RAG query planner system prompt
templates/
  index.html              Single-page Flask UI (all JS inline)
examples/
  code-quality-skill/     Example skill with SKILL.md + scripts + docs
  code-quality-skill.zip  Same, zipped for drag-drop into UI
sessions/                 Server-side session persistence (gitignored, .gitkeep present)
adk_chroma_db/            ChromaDB vector index (gitignored — rebuilt from adk_docs.txt)
adk_docs.txt              ADK docs source (gitignored — 3MB, download separately)
```

---

## Key architecture patterns

### SSE streaming with blocking permission gates

All pipeline stages stream Server-Sent Events via Flask `stream_with_context`.
Every file write blocks on `threading.Event.wait(timeout=900)` until the user approves or rejects in the UI.

```python
# Sub-generator pattern — yields SSE events AND returns a value
approved = yield from _perm_helper(job_id, filename, content, reason)
if not approved:
    yield sse("error", {...}); return
path.write_text(content)
yield sse("file", {"filename": filename, "content": content})
```

`_perm_helper` — file write approval gate (15-min timeout)
`_cmd_helper`  — shell command approval gate (15-min timeout)
`_run_subprocess` — sub-generator that yields `run_output` SSE lines, returns `(returncode, output)`

### In-memory job store

```python
JOBS: dict[str, dict] = {}
# Each job has: tmp_root, out_dir, files, perm_events, perm_results, hitl_event, hitl_approved
```

Jobs are lost on server restart. Session persistence (filesystem + localStorage) covers this.

### RAG pipeline

1. `build_index()` — chunks `adk_docs.txt` into 3,252 overlapping chunks, embeds via OpenAI, persists to ChromaDB
2. `_plan_retrieval_queries(adr)` — Claude reads the ADR and plans ~14 targeted search queries
3. `retrieve_for_queries(queries)` — cosine search, dedup by content hash, top-20 chunks, max 50k chars
4. Context injected into generator system prompt

Hash-based cache: if `adk_docs.txt` hasn't changed since last index, `build_index()` loads instantly.

### Generator output format

Claude returns XML file blocks (not JSON — Python code breaks JSON escaping):
```
<file name="tools.py">...complete file...</file>
<file name="agent.py">...complete file...</file>
<file name="prompts/root_agent.md">...instructions...</file>
<file name="Dockerfile">...</file>
<file name=".dockerignore">...</file>
```

Parsed by `parse_file_blocks(raw)` in `claude_sdk.py`.

### Session persistence (two-layer)

- **localStorage**: `skill_adk_session` key stores `{sessionId, phase, parsedSkills, currentAdr, projectName}`
- **Filesystem**: `sessions/<uuid>/` stores generated files as individual text files

`saveCheckpointAdr()` — called after ADR approval, saves phase="adr"
`saveCheckpointGenerated()` — called after `fetchFiles()` (pipeline done), saves phase="generated" + persists files to server
`resumeSession()` — restores both layers on page load if a session is detected

### Security

- Session IDs validated by `_SAFE_ID = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')` before use as path component
- All subprocess calls use list form, never `shell=True`
- `_safe()` helper validates tool inputs for shell metacharacters

---

## ADR JSON schema

Defined in `prompts/architect.md`. Key fields under `decisions`:

```json
{
  "topology":          {"choice": "flat|hierarchical|pipeline|router", "reasoning": ""},
  "memory":            {"short_term": {"choice": "session_only|tool_output_passing"}, "long_term": {"choice": "none|sqlite|file_based"}},
  "tool_granularity":  {"choice": "per_step|per_skill|grouped", "groupings": []},
  "error_strategy":    {"default": "stop|retry|warn_continue", "per_skill": []},
  "recovery_strategy": {"choice": "restart|checkpoint_resume|manual_intervention", "reasoning": ""},
  "autonomy_level":    {"choice": "supervised|semi_autonomous|autonomous", "reasoning": ""},
  "context_strategy":  {"choice": "full|filtered|summarized", "reasoning": ""},
  "parallelism":       {"parallel_groups": [], "sequential_chains": [], "reasoning": ""},
  "hitl_points":       [{"skill": "", "step": "", "reason": ""}],
  "data_flow":         [{"from": "", "produces": "", "to": "", "via": "file|env|tool_output"}]
}
```

---

## Generated code rules (enforced in generator_base.md)

- **Python-only**: no `.sh` files generated. Existing user shell scripts called via `subprocess.run()`
- Every `@tool` returns `{"status": "ok"|"error", "output": Any, "error": str|None}`
- Every `Agent()` instruction loaded from `prompts/<name>.md` — never hardcoded strings
- `_safe(val)` helper added to tools.py for input validation
- Error handling strictly follows ADR `error_strategy` per skill

---

## Flask routes reference

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Serve UI |
| POST | `/parse` | Parse SKILL.md(s) → JSON |
| POST | `/architect` | Run Architect Agent → ADR |
| POST | `/generate` | SSE stream — full pipeline |
| POST | `/permission/<id>` | Resolve pending permission gate |
| POST | `/hitl/approve/<job_id>` | Resolve HITL gate |
| GET | `/files/<job_id>` | Get all generated files |
| GET | `/download/<job_id>` | Download ZIP |
| POST | `/fix` | Fix a file given a runtime error (stateless) |
| POST | `/ask` | Ask Claude about a generated file |
| POST | `/chat/adr` | ADR Chat turn |
| GET | `/run/<job_id>` | SSE stream — sanity check |
| POST | `/smoketest/<job_id>` | SSE stream — smoke test |
| POST | `/session/<id>` | Save session files |
| GET | `/session/<id>` | Load session files |
| GET | `/session/<id>/download` | Download session ZIP |
| GET | `/rag/status` | RAG index status |
| POST | `/rag/build` | Build/rebuild RAG index |

---

## Running locally

```bash
pip install -r requirements.txt
claude login
export OPENAI_API_KEY=sk-...
# Place adk_docs.txt (llms-full.txt from google.github.io/adk-docs/llms-full.txt) in project root
python app.py
# → http://localhost:5000
```

Flask runs in debug mode with the reloader excluding `venv/`, `sessions/`, `adk_chroma_db/`, and `output/`
so that `pip install` during sanity check does not trigger a restart.

---

## What NOT to do

- Do not add `shell=True` to any subprocess call
- Do not generate `.sh` or `.bat` files from the generator — Python only
- Do not store API keys in session files or log them
- Do not change the `_SAFE_ID` regex without also updating the prefix check in `_safe_session_dir()`
- Do not run generated code on the server (sanity check uses `py_compile` and `import`, not execution of tool logic)
- The RAG index must be rebuilt with `build_index(force=True)` after changing `adk_docs.txt` — hash mismatch detection handles this automatically but only if the file is read with `encoding="utf-8"`
