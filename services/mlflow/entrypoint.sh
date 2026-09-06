#!/bin/bash
# MLflow entrypoint — fetch Postgres DSN from TinyDB, then start server

set -e

TINYDB_URL="${TINYDB_URL:-http://tinydb:28080}"
SECRET_STORE_TOKEN="${SECRET_STORE_TOKEN:-}"

# Fetch POSTGRES_MLFLOW_DSN from TinyDB if not set
if [ -z "$POSTGRES_MLFLOW_DSN" ]; then
    if [ -z "$SECRET_STORE_TOKEN" ]; then
        echo "ERROR: Neither POSTGRES_MLFLOW_DSN nor SECRET_STORE_TOKEN set"
        exit 1
    fi

    echo "Fetching POSTGRES_MLFLOW_DSN from TinyDB..."
    POSTGRES_MLFLOW_DSN=$(curl -s -H "X-API-Key: $SECRET_STORE_TOKEN" \
        "$TINYDB_URL/credential/POSTGRES_MLFLOW_DSN" | \
        python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

    if [ -z "$POSTGRES_MLFLOW_DSN" ]; then
        echo "ERROR: Could not fetch POSTGRES_MLFLOW_DSN from TinyDB"
        exit 1
    fi

    echo "✓ Fetched POSTGRES_MLFLOW_DSN"
fi

exec mlflow server \
    --host 0.0.0.0 \
    --port 5000 \
    --backend-store-uri "$POSTGRES_MLFLOW_DSN" \
    --serve-artifacts \
    --artifacts-destination /mlartifacts \
    --default-artifact-root mlflow-artifacts:/
