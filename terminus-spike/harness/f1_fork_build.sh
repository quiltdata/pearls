#!/usr/bin/env bash
# F1 - fork readiness: build terminusdb/terminusdb from source on macOS (arm64).
# This is the exact procedure that worked in this spike, including the two
# undocumented prerequisites discovered along the way (protoc, tus pack).
set -euo pipefail

REPO_DIR="${1:-./terminusdb}"

echo "== toolchain versions =="
rustc --version
cargo --version
swipl --version || echo "swipl NOT FOUND - install via: brew install swi-prolog"
protoc --version || echo "protoc NOT FOUND - install via: brew install protobuf"

if [ ! -d "$REPO_DIR" ]; then
  git clone --branch main https://github.com/terminusdb/terminusdb.git "$REPO_DIR"
fi
cd "$REPO_DIR"

# Undocumented prerequisite #1: protoc (protobuf compiler).
# Not mentioned in GETTING_STARTED.md or docs/CONTRIBUTING.md. Without it,
# `cargo build` fails deep in terminusdb-grpc-labelstore-proto's build script
# with "Could not find `protoc` installation".
#   brew install protobuf

# Undocumented prerequisite #2 (or rather, easy to miss the ordering):
# the Prolog bootstrap fails with "source_sink `library(tus)' does not exist"
# unless `make install-deps` has been run first to fetch the tus SWI-Prolog pack.
make install-deps

# macOS-friendly development build (avoids Gatekeeper/code-signing issues
# that the production `make` target hits).
time make dev

echo "== binary produced =="
ls -la terminusdb
file terminusdb

echo "== smoke test: serve and hit /api/info =="
STORAGE_DIR="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$STORAGE_DIR"
}
trap cleanup EXIT

TERMINUSDB_SERVER_PORT=6364 TERMINUSDB_STORAGE_PATH="$STORAGE_DIR" ./terminusdb store init --force
TERMINUSDB_SERVER_PORT=6364 TERMINUSDB_STORAGE_PATH="$STORAGE_DIR" TERMINUSDB_ADMIN_PASS=root ./terminusdb serve &
SERVER_PID=$!
sleep 6
curl -sS -u admin:root http://localhost:6364/api/info -w '\nHTTP_STATUS:%{http_code}\n'
