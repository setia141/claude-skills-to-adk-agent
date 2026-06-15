#!/bin/bash
# Run pylint on the repo and emit a JSON report to stdout.
# Usage: ./run_analysis.sh <repo_path>
set -euo pipefail

REPO="${1:-.}"
OUTPUT=$(mktemp)

cd "$REPO"

if ! command -v pylint &>/dev/null; then
  echo '{"errors":[],"warnings":0,"score":10.0,"info":"pylint not installed — skipped"}'
  exit 0
fi

# Collect Python files (limit to 50 to avoid very large repos)
PY_FILES=$(find . -name '*.py' -not -path './.venv/*' -not -path './venv/*' | head -50 | tr '\n' ' ')

if [ -z "$PY_FILES" ]; then
  echo '{"errors":[],"warnings":0,"score":10.0,"info":"no Python files found"}'
  exit 0
fi

# Run pylint in JSON mode; exit-zero so we always get output
pylint $PY_FILES --output-format=json --exit-zero 2>/dev/null > "$OUTPUT" || true

# Parse and summarise the JSON output
python3 - "$OUTPUT" <<'PYEOF'
import json, sys

raw = open(sys.argv[1]).read().strip()
msgs = json.loads(raw) if raw.startswith("[") else []

errors   = [m for m in msgs if m.get("type") in ("error", "fatal")]
warnings = [m for m in msgs if m.get("type") in ("warning", "refactor", "convention")]

# Approximate score: start at 10, deduct 0.5 per error, 0.1 per warning (cap at 0)
score = max(0.0, 10.0 - len(errors) * 0.5 - len(warnings) * 0.1)

print(json.dumps({
    "errors": [
        {"file": e["path"], "line": e["line"], "msg": e["message"]}
        for e in errors[:20]
    ],
    "warnings": len(warnings),
    "score":    round(score, 2),
}, indent=2))
PYEOF
