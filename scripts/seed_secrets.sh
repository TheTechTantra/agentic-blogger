#!/bin/bash
# Seed TinyDB with secrets for the agentic-blogger system

set -e

TINYDB_URL="${TINYDB_URL:-http://localhost:28080}"
SECRET_STORE_TOKEN="${SECRET_STORE_TOKEN:-}"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

if [ -z "$SECRET_STORE_TOKEN" ]; then
    echo -e "${RED}ERROR: SECRET_STORE_TOKEN not set${NC}"
    echo "Set it in .env or export it: export SECRET_STORE_TOKEN=<value>"
    exit 1
fi

echo -e "${YELLOW}Seeding TinyDB at $TINYDB_URL${NC}"

# Function to store a secret
store_secret() {
    local key="$1"
    local prompt="$2"
    local sensitive="${3:-true}"

    echo ""
    echo -n "$prompt: "
    if [ "$sensitive" = "true" ]; then
        read -rs value
        echo ""
    else
        read value
    fi

    if [ -z "$value" ]; then
        echo -e "${YELLOW}Skipping $key (empty)${NC}"
        return
    fi

    response=$(curl -s -X POST \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $SECRET_STORE_TOKEN" \
        -d "{\"key\": \"$key\", \"value\": \"$value\"}" \
        "$TINYDB_URL/key/")

    if echo "$response" | grep -q '"result":"ok"'; then
        echo -e "${GREEN}✓ Stored $key${NC}"
    else
        echo -e "${RED}✗ Failed to store $key${NC}"
        echo "Response: $response"
        exit 1
    fi
}

# Seed secrets
echo -e "${YELLOW}=== LLM KEYS ===${NC}"
store_secret "ANTHROPIC_API_KEY" "Enter ANTHROPIC_API_KEY" true
store_secret "OPENAI_API_KEY" "Enter OPENAI_API_KEY (leave empty to skip)" true
store_secret "GEMINI_API_KEY" "Enter GEMINI_API_KEY (leave empty to skip)" true
store_secret "TAVILY_API_KEY" "Enter TAVILY_API_KEY (leave empty to skip)" true

echo ""
echo -e "${YELLOW}=== TELEGRAM ===${NC}"
store_secret "TELEGRAM_BOT_TOKEN" "Enter TELEGRAM_BOT_TOKEN" true
store_secret "TELEGRAM_ALLOWED_USER_IDS" "Enter TELEGRAM_ALLOWED_USER_IDS (comma-separated numeric IDs)" false
store_secret "TELEGRAM_ADMIN_USER_ID" "Enter TELEGRAM_ADMIN_USER_ID (your user ID for /setmodel)" false

echo ""
echo -e "${YELLOW}=== BLOGGER OAUTH (from scripts/blogger_authorize.py) ===${NC}"
store_secret "BLOGGER_CLIENT_ID" "Enter BLOGGER_CLIENT_ID (from OAuth console)" false
store_secret "BLOGGER_CLIENT_SECRET" "Enter BLOGGER_CLIENT_SECRET" true
store_secret "BLOGGER_REFRESH_TOKEN" "Enter BLOGGER_REFRESH_TOKEN (run blogger_authorize.py first)" true
store_secret "BLOGGER_BLOG_ID" "Enter BLOGGER_BLOG_ID" false

echo ""
echo -e "${YELLOW}=== DATABASE ===${NC}"
store_secret "POSTGRES_APP_DSN" "Enter POSTGRES_APP_DSN (or auto-generated: postgresql://agentic_blogger:PASSWORD@postgres:5432/blogger)" false
store_secret "POSTGRES_MLFLOW_DSN" "Enter POSTGRES_MLFLOW_DSN (or auto-generated: postgresql://agentic_blogger:PASSWORD@postgres:5432/mlflow)" false

echo ""
echo -e "${GREEN}Done! Secrets seeded to $TINYDB_URL${NC}"
echo -e "${YELLOW}Next step: docker compose run --rm orchestrator alembic upgrade head${NC}"
