#!/bin/bash
set -euo pipefail

# Ensure jq is installed
if ! command -v jq >/dev/null 2>&1; then
  echo "Error: 'jq' is not installed. Please install it first (e.g., 'sudo apt install jq' or 'brew install jq')."
  exit 1
fi

SETTINGS_FILE="$HOME/.gemini/settings.json"

# Install the clangd-mcp tool
uv tool install git+https://github.com/schuay/clangd-mcp.git

# Prompt user for configuration paths
echo "Please provide the following paths for clangd-mcp configuration (press Enter to skip):"
read -e -p "Compile commands directory (containing compile_commands.json): " raw_cc_dir
read -e -p "Workspace directory (root of your C/C++ project): " raw_ws_dir
read -e -p "Seed file (source file to trigger background indexing): " raw_sf_dir

# Resolve absolute paths and symlinks
cc_dir=""
[ -n "$raw_cc_dir" ] && cc_dir=$(realpath -e "${raw_cc_dir/#\~/$HOME}")
ws_dir=""
[ -n "$raw_ws_dir" ] && ws_dir=$(realpath -e "${raw_ws_dir/#\~/$HOME}")
sf_dir=""
[ -n "$raw_sf_dir" ] && sf_dir=$(realpath -e "${raw_sf_dir/#\~/$HOME}")

mkdir -p "$(dirname "$SETTINGS_FILE")"
[ -f "$SETTINGS_FILE" ] || echo '{}' > "$SETTINGS_FILE"
cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak"

trap 'rm -f "$SETTINGS_FILE.tmp"' EXIT

# Build the args array dynamically based on provided values
ARGS="[]"
[ -n "$cc_dir" ] && ARGS=$(echo "$ARGS" | jq --arg v "$cc_dir" '. + ["--compile-commands-dir", $v]')
[ -n "$ws_dir" ] && ARGS=$(echo "$ARGS" | jq --arg v "$ws_dir" '. + ["--workspace-dir", $v]')
[ -n "$sf_dir" ] && ARGS=$(echo "$ARGS" | jq --arg v "$sf_dir" '. + ["--seed-file", $v]')

jq --argjson args "$ARGS" \
   '.mcpServers["clangd"] = {"command": "clangd-mcp", "args": $args}' \
   "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"

echo "Successfully updated $SETTINGS_FILE"
