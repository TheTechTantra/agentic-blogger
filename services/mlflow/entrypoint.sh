#!/bin/bash
# MLflow entrypoint — fetch secrets from TinyDB, then start the server.
#
# This server is three things to the pipeline: the Prompt Registry (the only
# source of prompt text), the tracing backend, and — since the AI Gateway —
# the only path from the workers to an LLM provider. The first and third are
# hard dependencies: if this container is down, jobs fail rather than degrade.

set -e

TINYDB_URL="${TINYDB_URL:-http://tinydb:28080}"
SECRET_STORE_TOKEN="${SECRET_STORE_TOKEN:-}"

# Reads one credential from TinyDB. urllib rather than curl: this image is
# python:3.12-slim, which ships neither curl nor wget, and an apt layer to
# fetch two strings is not worth the image size. Prints an empty string when
# the key is missing so each caller decides whether that is fatal.
fetch_secret() {
    TINYDB_URL="$TINYDB_URL" SECRET_STORE_TOKEN="$SECRET_STORE_TOKEN" \
    SECRET_KEY="$1" python3 - <<'PYFETCH'
import json, os, sys, urllib.request

url = os.environ["TINYDB_URL"].rstrip("/")
key = os.environ["SECRET_KEY"]
req = urllib.request.Request(
    f"{url}/credential/{key}", headers={"X-API-Key": os.environ["SECRET_STORE_TOKEN"]}
)
try:
    print(json.load(urllib.request.urlopen(req, timeout=10)).get("value", ""))
except Exception as e:
    print("", end="")
    print(f"fetch_secret({key}) failed: {type(e).__name__}: {e}", file=sys.stderr)
PYFETCH
}

# Fetch POSTGRES_MLFLOW_DSN from TinyDB if not set
if [ -z "$POSTGRES_MLFLOW_DSN" ]; then
    if [ -z "$SECRET_STORE_TOKEN" ]; then
        echo "ERROR: Neither POSTGRES_MLFLOW_DSN nor SECRET_STORE_TOKEN set"
        exit 1
    fi

    echo "Fetching POSTGRES_MLFLOW_DSN from TinyDB..."
    POSTGRES_MLFLOW_DSN=$(fetch_secret POSTGRES_MLFLOW_DSN)

    if [ -z "$POSTGRES_MLFLOW_DSN" ]; then
        echo "ERROR: Could not fetch POSTGRES_MLFLOW_DSN from TinyDB"
        exit 1
    fi

    echo "✓ Fetched POSTGRES_MLFLOW_DSN"
fi

# The AI Gateway encrypts every stored provider credential with a key derived
# from this passphrase. MLflow falls back to a hardcoded development default
# when it is unset — a value every MLflow installation in the world shares —
# which would leave the provider API keys encrypted with a public key. Refuse
# to start instead.
#
# It is also effectively not rotatable: change it and every secret already in
# the gateway store becomes undecryptable, surfacing as endpoints that exist
# but fail at invocation rather than at startup.
if [ -z "$MLFLOW_CRYPTO_KEK_PASSPHRASE" ]; then
    if [ -z "$SECRET_STORE_TOKEN" ]; then
        echo "ERROR: Neither MLFLOW_CRYPTO_KEK_PASSPHRASE nor SECRET_STORE_TOKEN set"
        exit 1
    fi

    echo "Fetching MLFLOW_CRYPTO_KEK_PASSPHRASE from TinyDB..."
    MLFLOW_CRYPTO_KEK_PASSPHRASE=$(fetch_secret MLFLOW_CRYPTO_KEK_PASSPHRASE)

    if [ -z "$MLFLOW_CRYPTO_KEK_PASSPHRASE" ]; then
        echo "ERROR: Could not fetch MLFLOW_CRYPTO_KEK_PASSPHRASE from TinyDB."
        echo "       Generate and seed one:  ./scripts/seed_secrets.sh"
        exit 1
    fi

    echo "✓ Fetched MLFLOW_CRYPTO_KEK_PASSPHRASE"
fi
export MLFLOW_CRYPTO_KEK_PASSPHRASE

# `mlflow server` refuses to start against an out-of-date backend schema
# rather than migrating it, and the 2.x -> 3.x upgrade changes that schema.
# Idempotent: a no-op once the database is current. The gateway's tables
# (secrets, model definitions, endpoints, budget policies) are part of it.
echo "Upgrading MLflow backend schema if needed..."
mlflow db upgrade "$POSTGRES_MLFLOW_DSN"

# MLflow 3.x added Host-header validation (DNS-rebinding protection). Its
# default allows localhost and private IPs but NOT service hostnames, so
# in-network calls to http://mlflow:5000 come back as
# 403 "Invalid Host header - possible DNS rebinding attack detected".
# Enumerated rather than wildcarded so the protection still means something;
# the published port is bound to 127.0.0.1 on the host regardless.
ALLOWED_HOSTS="${MLFLOW_ALLOWED_HOSTS:-mlflow,mlflow:5000,localhost,localhost:5000,localhost:25000,127.0.0.1,127.0.0.1:5000,127.0.0.1:25000}"

# --workers is pinned rather than left at uvicorn's default of 4, for two
# independent reasons that happen to point the same way:
#   1. Memory. 3.x at 4 workers plus the job pool sat at the container ceiling
#      (see the note on the memory limit in docker-compose.yml).
#   2. Budgets. The gateway's default budget tracker strategy is `local`,
#      which accumulates spend inside each worker process. With N workers a
#      $50 budget is enforced at roughly $50*N. Sharing that state needs Redis
#      (MLFLOW_GATEWAY_BUDGET_REDIS_URL); one worker gets it for free.
# Raise this only alongside a Redis budget tracker, never on its own.
GATEWAY_WORKERS="${MLFLOW_SERVER_WORKERS:-1}"

exec mlflow server \
    --host 0.0.0.0 \
    --port 5000 \
    --workers "$GATEWAY_WORKERS" \
    --allowed-hosts "$ALLOWED_HOSTS" \
    --backend-store-uri "$POSTGRES_MLFLOW_DSN" \
    --serve-artifacts \
    --artifacts-destination /mlartifacts \
    --default-artifact-root mlflow-artifacts:/
