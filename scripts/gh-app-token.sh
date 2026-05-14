#!/usr/bin/env bash
# Mint a GitHub App installation access token (ghs_...) from a private key.
#
# Required env vars:
#   GH_APP_ID            App ID (numeric) or Client ID (Iv23...)
#   GH_APP_INSTALL_ID    Installation ID, from https://github.com/settings/installations/<id>
#   GH_APP_PEM_PATH      Path to the downloaded .pem private key
#
# Usage:
#   ./scripts/gh-app-token.sh              # prints token to stdout
#   ./scripts/gh-app-token.sh --check      # also prints repos the installation can see
#   export GITHUB_TOKEN=$(./scripts/gh-app-token.sh)

set -euo pipefail

: "${GH_APP_ID:?GH_APP_ID is required (App ID or Client ID)}"
: "${GH_APP_INSTALL_ID:?GH_APP_INSTALL_ID is required (installation id)}"
: "${GH_APP_PEM_PATH:?GH_APP_PEM_PATH is required (path to .pem private key)}"

APP_ID="$GH_APP_ID"
INSTALLATION_ID="$GH_APP_INSTALL_ID"
PEM_PATH="$GH_APP_PEM_PATH"

for cmd in ruby curl jq; do
  command -v "$cmd" >/dev/null || { echo "missing dependency: $cmd" >&2; exit 1; }
done
ruby -e 'require "jwt"' 2>/dev/null || {
  echo "ruby gem 'jwt' not installed - run: gem install --user-install jwt" >&2
  exit 1
}
[[ -r "$PEM_PATH" ]] || { echo "cannot read PEM: $PEM_PATH" >&2; exit 1; }

# Step 1: sign App JWT with the private key
JWT=$(APP_ID="$APP_ID" PEM_PATH="$PEM_PATH" ruby <<'RUBY'
require 'openssl'
require 'jwt'

private_pem = File.read(ENV.fetch('PEM_PATH'))
private_key = OpenSSL::PKey::RSA.new(private_pem)

payload = {
  iat: Time.now.to_i - 60,           # 60s skew
  exp: Time.now.to_i + (10 * 60),    # 10 min max
  iss: ENV.fetch('APP_ID'),
}

puts JWT.encode(payload, private_key, 'RS256')
RUBY
)

# Step 2: exchange JWT for installation access token
RESPONSE=$(curl -fsS -X POST \
  -H "Authorization: Bearer $JWT" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/app/installations/$INSTALLATION_ID/access_tokens")

TOKEN=$(echo "$RESPONSE" | jq -r .token)
[[ -n "$TOKEN" && "$TOKEN" != "null" ]] || {
  echo "failed to mint token. response:" >&2
  echo "$RESPONSE" >&2
  exit 1
}

if [[ "${1:-}" == "--check" ]]; then
  echo "token: ${TOKEN:0:10}... (len=${#TOKEN})" >&2
  echo "expires: $(echo "$RESPONSE" | jq -r .expires_at)" >&2
  echo "repos this installation can see:" >&2
  GH_TOKEN="$TOKEN" gh api /installation/repositories \
    | jq -r '.repositories[].full_name' | sed 's/^/  - /' >&2
fi

echo "$TOKEN"
