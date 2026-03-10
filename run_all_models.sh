#!/usr/bin/env bash
set -euo pipefail

TASK_FILE="${1:?Usage: $0 <task_file> <timeout_seconds> <tool: ccr|opencode|both>}"
TIMEOUT="${2:?Usage: $0 <task_file> <timeout_seconds> <tool: ccr|opencode|both>}"
TOOL="${3:?Usage: $0 <task_file> <timeout_seconds> <tool: ccr|opencode|both>}"
CONFIG="$HOME/.claude-code-router/config.json"

if [[ "$TOOL" != "ccr" && "$TOOL" != "opencode" && "$TOOL" != "both" ]]; then
    echo "Error: tool must be one of: ccr, opencode, both" >&2
    exit 1
fi

# Extract model list from config using python3
MODELS=$(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
for p in cfg['Providers']:
    if p['name'] == 'openrouter':
        print('\n'.join(p['models']))
        sys.exit(0)
")

TOTAL=$(echo "$MODELS" | wc -l | tr -d ' ')
i=0
FAILED=()

while IFS= read -r MODEL; do
    i=$((i + 1))
    echo ""
    echo "[$i/$TOTAL] Running: openrouter/$MODEL"
    echo "----------------------------------------"
    if python3 bench.py --tool "$TOOL" --model "openrouter/$MODEL" --task "$TASK_FILE" --timeout "$TIMEOUT" < /dev/null; then
        echo "[$i/$TOTAL] Done: openrouter/$MODEL"
    else
        echo "[$i/$TOTAL] FAILED: openrouter/$MODEL (exit $?)"
        FAILED+=("openrouter/$MODEL")
    fi
done <<< "$MODELS"

echo ""
echo "========================================"
echo "All $TOTAL models run."
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "Failed models:"
    for m in "${FAILED[@]}"; do echo "  - $m"; done
else
    echo "All runs completed successfully."
fi
