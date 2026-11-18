#!/usr/bin/env bash
# Rerun of the independent tester's scenario (Padaria Vila, 3 invoices, owner approves R6 after a delay).
# A fresh `claude -p` session gets only the guide, the ATL MCP server and the task; a scripted owner acts
# from ATL's log after OWNER_DELAY seconds. No messages to the agent during the run.
#
# Usage: coldstart/run_bakery.sh ATL_CHECKOUT GUIDE_FILE [model] [owner_delay_s]
#   ATL_CHECKOUT  the ATL code the MCP server and the owner CLI run (e.g. a checkout of tag hw8, or this repo)
#   GUIDE_FILE    the agent guide given to the agent as AGENT_GUIDE.md
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ATL="$(cd "$1" && pwd)"
GUIDE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
MODEL="${3:-claude-opus-5-5}"
DELAY="${4:-180}"
RUN="$(mktemp -d -t atl-bakery)"
mkdir -p "$RUN/var" "$RUN/runs" "$RUN/work"
export ATL_RAIL=simulated ATL_ENV=sandbox ATL_VAR_DIR="$RUN/var" ATL_RUNS_DIR="$RUN/runs" ATL_RUN_ID=coldstart_test

python3 - "$HERE/bakery/mandate-test.json" "$RUN/mandate.json" <<'EOF'
import json, sys, datetime as d
m = json.load(open(sys.argv[1]))
m["valid_until"] = (d.datetime.now(d.UTC) + d.timedelta(days=30)).isoformat()
json.dump(m, open(sys.argv[2], "w"))
EOF
uv run --directory "$ATL" atl mandate register "$RUN/mandate.json"
cp "$GUIDE" "$RUN/work/AGENT_GUIDE.md"
cat > "$RUN/mcp.json" <<EOF
{"mcpServers": {"atl": {"command": "$(command -v uv)", "args": ["run", "--directory", "$ATL", "python", "-m", "atl.mcp_server"],
  "env": {"ATL_RAIL": "simulated", "ATL_ENV": "sandbox", "ATL_VAR_DIR": "$RUN/var", "ATL_RUNS_DIR": "$RUN/runs", "ATL_RUN_ID": "coldstart_test"}}}}
EOF

python3 "$HERE/owner_bot.py" --repo "$ATL" --delay "$DELAY" --until-seconds 1500 > "$RUN/owner.log" 2>&1 &
OWNER=$!

cd "$RUN/work"
START=$(date +%s)
claude -p "$(cat "$HERE/bakery/task.txt")" --model "$MODEL" --strict-mcp-config --mcp-config "$RUN/mcp.json" \
  --setting-sources project \
  --allowedTools "Read,Write,Glob,mcp__atl__get_mandate,mcp__atl__pay_check,mcp__atl__execute_payment,mcp__atl__get_decision,mcp__atl__get_receipt,mcp__atl__verify_receipt" \
  --disallowedTools "Bash,WebFetch,WebSearch,Task,mcp__atl__register_mandate" \
  --output-format stream-json --verbose > "$RUN/transcript.jsonl" || true
echo "agent ran $(( $(date +%s) - START )) s"
kill "$OWNER" 2>/dev/null || true

echo "--- owner actions"; cat "$RUN/owner.log"
echo "--- ATL log (tool, verdict/status, decision)"
python3 - "$RUN/runs/coldstart_test.jsonl" <<'EOF'
import json, sys
for line in open(sys.argv[1]):
    e = json.loads(line); r = e.get("result") or {}
    print(e["ts"][11:19], e.get("tool") or e.get("event"), (e.get("args") or {}).get("invoice_ref", ""),
          r.get("verdict", ""), r.get("status", ""), r.get("rule_hits", ""), r.get("valid", ""))
EOF
echo "run folder: $RUN"
