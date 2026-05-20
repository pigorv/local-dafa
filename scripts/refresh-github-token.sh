#!/usr/bin/env bash
# Mint a fresh GitHub App installation token and write it into .env.example
# under the GITHUB_TOKEN= line.
#
# Reads GH_APP_ID, GH_APP_INSTALL_ID, GH_APP_PEM_PATH from repo-root .env.
# Run from anywhere:
#   ./scripts/refresh-github-token.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOTENV="$REPO_ROOT/.env"
ENV_FILE="$DOTENV"

[[ -f "$DOTENV" ]] || { echo "missing $DOTENV" >&2; exit 1; }

# Pull just the keys we need from .env. Plain read loop — no process
# substitution — for compatibility with the bash 3.2 shipped on macOS.
while IFS='=' read -r _key _value; do
    case "$_key" in
        GH_APP_ID|GH_APP_INSTALL_ID|GH_APP_PEM_PATH)
            # Strip optional surrounding quotes and expand a leading ~.
            _value="${_value%\"}"; _value="${_value#\"}"
            _value="${_value%\'}"; _value="${_value#\'}"
            [[ "$_value" == "~/"* ]] && _value="$HOME/${_value#~/}"
            export "$_key=$_value"
            ;;
    esac
done < "$DOTENV"
unset _key _value

: "${GH_APP_ID:?GH_APP_ID not set in .env}"
: "${GH_APP_INSTALL_ID:?GH_APP_INSTALL_ID not set in .env}"
: "${GH_APP_PEM_PATH:?GH_APP_PEM_PATH not set in .env}"

[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; exit 1; }

TOKEN="$("$SCRIPT_DIR/gh-app-token.sh")"
[[ -n "$TOKEN" ]] || { echo "gh-app-token.sh returned empty token" >&2; exit 1; }

# Replace the GITHUB_TOKEN= line in-place. macOS/BSD sed needs the '' arg.
python3 - "$ENV_FILE" "$TOKEN" <<'PY'
import sys, pathlib, re
path, token = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
new, n = re.subn(r"(?m)^GITHUB_TOKEN=.*$", f"GITHUB_TOKEN={token}", text)
if n == 0:
    sys.exit("no GITHUB_TOKEN= line found in .env.example")
if n > 1:
    sys.exit(f"expected 1 GITHUB_TOKEN= line, found {n}")
path.write_text(new)
PY

echo "updated GITHUB_TOKEN in $ENV_FILE (token len=${#TOKEN})"
