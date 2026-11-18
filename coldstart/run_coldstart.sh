#!/usr/bin/env bash
# Cold-start usability test (MIT AI Studio HW9): a fresh Claude Code session gets only docs/AGENT_GUIDE.md,
# the ATL MCP server and a task. No scripted call sequence, no expected answer, no help during the run.
#
# Usage (from the repo root):  coldstart/run_coldstart.sh coldstart/task-NF-1042.md [model]
# Needs: uv, Claude Code (`claude`) logged in. Uses the simulated Pix rail: no money, no keys.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TASK="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
MODEL="${2:-claude-sonnet-5-5}"
RUN="$(mktemp -d -t atl-coldstart)"
mkdir -p "$RUN/atl-var" "$RUN/atl-runs" "$RUN/work/inbox" "$RUN/work/outbox"

export ATL_RAIL=simulated ATL_ENV=sandbox ATL_VAR_DIR="$RUN/atl-var" ATL_RUNS_DIR="$RUN/atl-runs"

# Owner setup (not the agent): sign and register the mandate, fund the simulated rail with R$ 5.000,00.
python3 - "$REPO/coldstart/mandate.json" "$RUN/mandate.json" <<'EOF'
import json, sys, datetime as d
m = json.load(open(sys.argv[1]))
m["valid_until"] = (d.datetime.now(d.UTC) + d.timedelta(days=30)).isoformat()
json.dump(m, open(sys.argv[2], "w"))
EOF
uv run --directory "$REPO" atl mandate register "$RUN/mandate.json"
printf '{"balance_cents": 500000, "transfers": []}\n' > "$RUN/atl-var/sim_rail.json"

# The agent's workspace: the guide and the invoices, nothing else.
cp "$REPO/docs/AGENT_GUIDE.md" "$RUN/work/AGENT_GUIDE.md"
cp "$REPO"/coldstart/inbox/*.txt "$RUN/work/inbox/"
cat > "$RUN/mcp.json" <<EOF
{"mcpServers": {"atl": {"command": "$(command -v uv)", "args": ["run", "--directory", "$REPO", "python", "-m", "atl.mcp_server"],
  "env": {"ATL_RAIL": "simulated", "ATL_ENV": "sandbox", "ATL_VAR_DIR": "$RUN/atl-var", "ATL_RUNS_DIR": "$RUN/atl-runs"}}}}
EOF

cd "$RUN/work"
claude -p "$(cat "$TASK")" --model "$MODEL" --strict-mcp-config --mcp-config "$RUN/mcp.json" --setting-sources project \
  --allowedTools "Read,Write,Glob,mcp__atl__get_mandate,mcp__atl__pay_check,mcp__atl__execute_payment,mcp__atl__get_decision,mcp__atl__get_receipt,mcp__atl__verify_receipt" \
  --disallowedTools "Bash,WebFetch,WebSearch,Task,mcp__atl__register_mandate" \
  --output-format stream-json --verbose > "$RUN/transcript.jsonl"

python3 "$REPO/coldstart/trace.py" "$RUN/transcript.jsonl"
uv run --directory "$REPO" python "$REPO/coldstart/check.py" "$RUN/work/outbox"
echo "run folder: $RUN"
