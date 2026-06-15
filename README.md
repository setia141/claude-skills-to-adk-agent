# Skill → ADK Agent Platform

Convert Claude Skill files into production-ready Google ADK agent code.
An Architect Agent produces a reviewed Architecture Decision Record (ADR), a Generator
produces RAG-grounded code that strictly follows it, and a built-in sanity check + auto-fix
loop validates and corrects the output before you download.

**No Claude API key** — uses `claude-agent-sdk` with local CLI auth (`claude login`).

---

## Features

| | |
|---|---|
| **Multi-skill support** | Single `SKILL.md` or a ZIP of multiple skills — composed into one agent |
| **ADR-driven generation** | Architect Agent reasons about architecture first; code strictly follows the ADR |
| **ADR Chat** | Conversational interface to refine architecture decisions before generating |
| **RAG-grounded code** | 3,252 chunks of official ADK docs retrieved per ADR via semantic search |
| **Permission gates** | Every file write shown to user for approval before touching disk |
| **Session persistence** | Resume after page refresh or server restart — localStorage + filesystem |
| **Validator** | Generated code checked against ADR decisions; auto-fixed if mismatched |
| **Sanity check** | `pip install` → `py_compile` → `import` test, with Claude auto-fix loop (3 attempts) |
| **Smoke test** | Runs the generated agent end-to-end via ADK Runner with your credentials |
| **Fix with Claude** | Paste any runtime error — Claude rewrites the affected file |
| **Dockerfile** | `Dockerfile` + `.dockerignore` generated alongside agent code |
| **Python-only output** | All generated code is Python; existing shell scripts called via `subprocess` |

---

## Architecture

```
╔══════════════════════════════════════════════════════════════════════╗
║  OFFLINE SETUP  (once — or when adk_docs.txt is updated)            ║
║                                                                      ║
║   adk_docs.txt ──► chunk (3,252 chunks) ──► text-embedding-3-small  ║
║   3 MB · 66k lines        500 tokens each        OpenAI API         ║
║                                    │                                 ║
║                               ChromaDB  ◄── persisted to disk       ║
║                            adk_chroma_db/                            ║
╚══════════════════════════════════════════════════════════════════════╝

╔══════════════════════════════════════════════════════════════════════╗
║  PIPELINE  (per request)                                             ║
║                                                                      ║
║  SKILL.md × N  ──────────────────────────────────────────────────   ║
║  or ZIP                                                          │   ║
║                                                                  ▼   ║
║                              ┌─────────────────────────────────┐    ║
║                              │  Parser Agent                   │    ║
║                              │  SKILL.md → structured JSON     │    ║
║                              │  per step: type · purpose ·     │    ║
║                              │  deps · script refs             │    ║
║                              └──────────────┬──────────────────┘    ║
║                                             │ parsed skills[]       ║
║                              ┌──────────────▼──────────────────┐    ║
║                              │  Architect Agent                 │    ║
║                              │  reasons about topology ·        │    ║
║                              │  memory · error strategy ·       │◄───╫── ADR Chat
║                              │  autonomy · context · recovery   │    ║  (iterative
║                              └──────────────┬──────────────────┘    ║   refinement)
║                                             │ ADR (JSON)            ║
║                              ┌──────────────▼──────────────────┐    ║
║                              │  ADR Review  (UI)               │    ║
║                              │  user reads · edits · approves  │    ║
║                              └──────────────┬──────────────────┘    ║
║                                             │ approved ADR          ║
║                 ┌───────────────────────────▼──────────────────┐    ║
║                 │  RAG Retrieval                                │    ║
║                 │  Query Planner → ~14 targeted queries         │    ║
║                 │  ChromaDB cosine search (text-embedding-3-sm) │    ║
║                 │  top-20 unique chunks · up to 18k chars       │    ║
║                 └───────────────────────────┬──────────────────┘    ║
║                                             │ ADK doc context       ║
║                 ┌───────────────────────────▼──────────────────┐    ║
║                 │  Generator Agent                              │    ║
║                 │  ADR + doc context → Python-only code        │    ║
║                 │                                               │    ║
║                 │  tools.py      @tool per ADR granularity      │    ║
║                 │  agent.py      topology from ADR              │    ║
║                 │  prompts/*.md  one instruction file / Agent() │    ║
║                 │  Dockerfile    python:3.11-slim               │    ║
║                 │  .dockerignore                                │    ║
║                 └───────────────────────────┬──────────────────┘    ║
║                                             │                       ║
║                          ✍ permission popup per file ──────────────►║
║                                             │                       ║
║                 ┌───────────────────────────▼──────────────────┐    ║
║                 │  Validator Agent                              │    ║
║                 │  checks code against every ADR decision       │    ║
║                 │  auto-fixes mismatches (permission popup)     │    ║
║                 └───────────────────────────┬──────────────────┘    ║
║                                             │                       ║
║                 ┌───────────────────────────▼──────────────────┐    ║
║                 │  Test Generator Agent                         │    ║
║                 │  tools.py → pytest suite                      │    ║
║                 │  error strategy + HITL points from ADR        │    ║
║                 └───────────────────────────┬──────────────────┘    ║
║                                             │                       ║
║                 ┌───────────────────────────▼──────────────────┐    ║
║                 │  AST Syntax Verifier                          │    ║
║                 │  py_compile on every generated .py file       │    ║
║                 └───────────────────────────┬──────────────────┘    ║
║                                             │                       ║
║                 ┌───────────────────────────▼──────────────────┐    ║
║                 │  HITL Gate  (if ADR hitl_points non-empty)    │    ║
║                 │  pauses pipeline · waits for user approval    │    ║
║                 └───────────────────────────┬──────────────────┘    ║
║                                             │                       ║
║                 ┌───────────────────────────▼──────────────────┐    ║
║                 │  Package                                      │    ║
║                 │  tools.py · agent.py · prompts/ · tests/      │    ║
║                 │  requirements.txt · adr.json · README.md      │    ║
║                 │  Dockerfile · .dockerignore · run_tests.*     │    ║
║                 └───────────────────────────┬──────────────────┘    ║
║                                             │                       ║
║                             Download ZIP ◄──┘                       ║
║                                                                      ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │  Sanity Check  (on demand, post-download)                   │    ║
║  │                                                             │    ║
║  │  1. pip install -r requirements.txt   ► approval popup      │    ║
║  │  2. py_compile on tools.py + agent.py (no execution)       │    ║
║  │  3. python -c "import tools; import agent"  ► popup        │    ║
║  │                                                             │    ║
║  │  On failure → Claude rewrites file → approval → re-check   │    ║
║  │              (up to 3 auto-fix attempts per step)           │    ║
║  └─────────────────────────────────────────────────────────────┘    ║
║                                                                      ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │  Smoke Test  (on demand)                                    │    ║
║  │  ADK Runner + user-supplied env vars → live agent run       │    ║
║  │  model-agnostic: ANTHROPIC_API_KEY / GOOGLE_API_KEY / etc.  │    ║
║  └─────────────────────────────────────────────────────────────┘    ║
╚══════════════════════════════════════════════════════════════════════╝
```

---

## ADR decisions

| Decision | Options | Controls |
|---|---|---|
| **Topology** | flat / hierarchical / pipeline / router | Agent class structure and sub-agent wiring |
| **Memory — short-term** | session_only / tool_output_passing | How state flows between tool calls |
| **Memory — long-term** | none / sqlite / file_based | Session service class selected |
| **Tool granularity** | per_step / per_skill / grouped | Number and naming of `@tool` functions |
| **Error strategy** | stop / retry / warn_continue / fallback | `try/except` pattern in every tool |
| **Recovery strategy** | restart / checkpoint_resume / manual_intervention | Post-failure resumption behaviour |
| **Autonomy level** | supervised / semi_autonomous / autonomous | Density of HITL approval gates |
| **Context strategy** | full / filtered / summarized | How much state each agent receives |
| **Parallelism** | parallel groups / sequential chains | `asyncio.gather` or `SequentialAgent` |
| **HITL points** | list of step + reason | `input()` confirmation before each flagged tool |
| **Data flow** | from / produces / to / via | Tool output passing between skills |

---

## Project structure

```
skill_adk_v4/
├── app.py                    Flask backend + SSE pipeline routes
├── requirements.txt
├── agents/
│   ├── __init__.py
│   ├── claude_sdk.py         ask() wrapper over claude-agent-sdk
│   ├── rag.py                chunk · embed · retrieve ADK docs (ChromaDB)
│   ├── parser.py             SKILL.md → structured JSON
│   ├── architect.py          Architect Agent → ADR + ADR Chat
│   ├── generator.py          Generator · Validator · Test Generator
│   ├── fixer.py              Fix-with-Claude (runtime error → patched file)
│   └── verifier.py           AST syntax check on generated files
├── prompts/
│   ├── architect.md          Architect Agent system prompt
│   ├── adr_chat.md           ADR Chat system prompt
│   ├── generator_base.md     Generator system prompt (Python-only rule)
│   ├── validator.md          Validator system prompt
│   ├── test_gen.md           Test generator system prompt
│   ├── fixer.md              Fixer system prompt
│   ├── parse_skill.md        Parser system prompt
│   └── query_planner.md      RAG query planner system prompt
├── templates/
│   └── index.html            Single-page Flask UI
├── examples/
│   ├── code-quality-skill/   Example: SKILL.md + scripts + reference docs
│   └── code-quality-skill.zip  Drag-and-drop into the UI
└── sessions/                 Server-side session files (gitignored)
```

---

## Generated output

```
my-adk-agent/
├── agent.py              root_agent wired to ADR topology
├── tools.py              @tool per step, namespaced across skills
├── prompts/
│   └── *.md              one instruction file per Agent() instance
├── requirements.txt      inferred pip dependencies
├── adr.json              the ADR that drove this generation
├── Dockerfile            python:3.11-slim · runtime env vars documented
├── .dockerignore
├── README.md
├── run_tests.sh          local pytest runner (not executed on server)
├── run_tests.bat
└── tests/
    └── test_tools.py     pytest suite, ADR error-strategy-aware
```

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Authenticate Claude (no API key — uses local CLI)
claude login

# 3. OpenAI key (embeddings only — not for generation)
export OPENAI_API_KEY=sk-...        # Windows: set OPENAI_API_KEY=sk-...

# 4. Get ADK docs (3 MB — indexed once, reused forever)
curl -o adk_docs.txt https://google.github.io/adk-docs/llms-full.txt

# 5. Start
python app.py
# → http://localhost:5000
```

First run builds the RAG index automatically (~3,252 chunks, 2–5 min).
Every subsequent start loads from disk in under a second.

---

## RAG index management

```bash
# Check status
curl http://localhost:5000/rag/status

# Force rebuild (after updating adk_docs.txt)
curl -X POST http://localhost:5000/rag/build \
     -H "Content-Type: application/json" \
     -d '{"force": true}'
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web UI + SSE streaming pipeline |
| `claude-agent-sdk` | Claude calls — local CLI auth, no API key needed |
| `openai` | `text-embedding-3-small` for RAG embeddings |
| `chromadb` | Local persistent vector store |
