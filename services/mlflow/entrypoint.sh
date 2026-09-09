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

# `mlflow server` refuses to start against an out-of-date backend schema
# rather than migrating it, and the 2.x -> 3.x upgrade changes that schema.
# Idempotent: a no-op once the database is current.
echo "Upgrading MLflow backend schema if needed..."
mlflow db upgrade "$POSTGRES_MLFLOW_DSN"

# MLflow 3.x added Host-header validation (DNS-rebinding protection). Its
# default allows localhost and private IPs but NOT service hostnames, so
# in-network calls to http://mlflow:5000 come back as
# 403 "Invalid Host header - possible DNS rebinding attack detected".
# Enumerated rather than wildcarded so the protection still means something;
# the published port is bound to 127.0.0.1 on the host regardless.
ALLOWED_HOSTS="${MLFLOW_ALLOWED_HOSTS:-mlflow,mlflow:5000,localhost,localhost:5000,localhost:25000,127.0.0.1,127.0.0.1:5000,127.0.0.1:25000}"

exec mlflow server \
    --host 0.0.0.0 \
    --port 5000 \
    --allowed-hosts "$ALLOWED_HOSTS" \
    --backend-store-uri "$POSTGRES_MLFLOW_DSN" \
    --serve-artifacts \
    --artifacts-destination /mlartifacts \
    --default-artifact-root mlflow-artifacts:/
