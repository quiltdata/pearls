#!/usr/bin/env bash
# Shared helpers for the TerminusDB spike harness (09c -> 09d).
set -euo pipefail

BASE="http://localhost:6363"
AUTH="admin:root"
ORG="admin"
DB="spike"
EVID="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/evidence"

curl_json() {
  # curl_json METHOD PATH BODY_JSON_OR_EMPTY
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -u "$AUTH" -X "$method" "$BASE$path" \
      -H "Content-Type: application/json" -D /tmp/spike_headers.$$ -d "$body" \
      -w '\nHTTP_STATUS:%{http_code}\n'
  else
    curl -sS -u "$AUTH" -X "$method" "$BASE$path" \
      -D /tmp/spike_headers.$$ \
      -w '\nHTTP_STATUS:%{http_code}\n'
  fi
  echo "---headers---"
  cat /tmp/spike_headers.$$
  rm -f /tmp/spike_headers.$$
}

branch_tip() {
  # branch_tip BRANCH -> prints commit identifier
  local branch="$1"
  curl -sS -u "$AUTH" "$BASE/api/log/$ORG/$DB/local/branch/$branch?count=1" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["identifier"])'
}

reset_branch() {
  # reset_branch BRANCH COMMIT_ID
  local branch="$1" commit="$2"
  curl -sS -u "$AUTH" -X POST "$BASE/api/reset/$ORG/$DB/local/branch/$branch" \
    -H "Content-Type: application/json" \
    -d "{\"commit_descriptor\":\"$ORG/$DB/local/commit/$commit\"}"
}

read_widget() {
  # read_widget BRANCH
  local branch="$1"
  curl -sS -u "$AUTH" "$BASE/api/document/$ORG/$DB/local/branch/$branch?id=Product/Widget"
}

put_widget() {
  # put_widget BRANCH PRICE STOCK MESSAGE
  local branch="$1" price="$2" stock="$3" msg="$4"
  curl -sS -u "$AUTH" -X PUT "$BASE/api/document/$ORG/$DB/local/branch/$branch?author=spike&message=$msg" \
    -H "Content-Type: application/json" \
    -d "{\"@type\":\"Product\",\"@id\":\"Product/Widget\",\"name\":\"Widget\",\"price\":$price,\"stock\":$stock}"
}
