#!/usr/bin/env bash
# PostToolUse hook on Read/Edit/Write: reports vault note activity to
# vault-graph's local activity endpoint so the matching node can pulse.
#
# Setup: register this script in your Claude Code settings.json under
# PostToolUse for the Read and Edit|Write matchers, and set VAULT_GRAPH_CONFIG
# to the vault-graph.json you're actually running against, e.g.:
#
#   "hooks": {
#     "PostToolUse": [
#       { "matcher": "Read",       "hooks": [{ "type": "command",
#         "command": "VAULT_GRAPH_CONFIG=/path/to/vault-graph/vault-graph.json /path/to/vault-graph/integrations/claude-code/report-vault-activity.sh" }] },
#       { "matcher": "Edit|Write", "hooks": [{ "type": "command",
#         "command": "VAULT_GRAPH_CONFIG=/path/to/vault-graph/vault-graph.json /path/to/vault-graph/integrations/claude-code/report-vault-activity.sh" }] }
#     ]
#   }
#
# Fire-and-forget - the POST runs in the background with a short timeout and
# every exit path returns 0, so a slow/missing/crashed vault-graph server
# never blocks or fails the actual tool call. Path validation for anything
# security-relevant lives server-side (server.py); this script only decides
# whether to bother reporting.
set -euo pipefail

[[ -n "${VAULT_GRAPH_CONFIG:-}" && -f "$VAULT_GRAPH_CONFIG" ]] || exit 0

input="$(cat)"
tool_name="$(jq -r '.tool_name // empty' <<<"$input")"
file_path="$(jq -r '.tool_input.file_path // empty' <<<"$input")"

[[ -n "$file_path" ]] || exit 0

case "$tool_name" in
  Read) action="read" ;;
  Edit|Write) action="write" ;;
  *) exit 0 ;;
esac

vault_dir="$(jq -r '.vault_dir // empty' "$VAULT_GRAPH_CONFIG")"
port="$(jq -r '.port // empty' "$VAULT_GRAPH_CONFIG")"
[[ -n "$vault_dir" && -n "$port" ]] || exit 0

case "$file_path" in
  "$vault_dir"/*.md) ;;
  *) exit 0 ;;
esac

relpath="${file_path#"$vault_dir"/}"
timestamp_ms="$(( $(date +%s%N) / 1000000 ))"

payload="$(jq -n --arg action "$action" --arg path "$relpath" --argjson ts "$timestamp_ms" \
  '{action:$action, path:$path, timestamp:$ts}')"

nohup curl -s --max-time 0.3 -X POST "http://127.0.0.1:${port}/api/activity" \
  -H 'Content-Type: application/json' \
  -d "$payload" >/dev/null 2>&1 &

exit 0
