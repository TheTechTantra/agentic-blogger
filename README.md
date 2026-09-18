# Agentic Blogger

An autonomous blog-writing pipeline that takes a topic (via Telegram), researches it with live web search, drafts a long-form post through a multi-node LangGraph pipeline, fact-checks and revises it, then publishes to Google Blogger as a draft.

** This is part of blog :https://www.think-ai.cloud/2026/08/agentic-systems-cognitive-architecture.html **

```
Topic (Telegram)
    │
    ▼
research ──► outline ──► draft ──► factcheck ──► revise
                                       │            ▲
                                       │ (loop once) │
                                       └─────────────┘
                                       │
                                  "continue"
                                       ▼
                                     seo ──► format ──► publish (Blogger draft)
```

**8 nodes**, one conditional revision loop. Every LLM call routes through the [MLflow AI Gateway](https://mlflow.org/docs/latest/llms/deployments/index.html) — workers hold no provider credentials. Research uses Anthropic's server-side `web_search` tool for live, attributed sources.

## Quick Start

### Prerequisites

- Docker and Docker Compose
- An Anthropic API key (required — research depends on server tools)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Google Blogger OAuth credentials (for publishing)

### Setup

```bash
# 1. Configure
cp .env.example .env
# Generate and paste the three bootstrap secrets (see .env.example for commands)

# 2. Build and start
make build
make up

# 3. Run migrations
make migrate

# 4. Seed secrets (interactive — prompts for each credential)
make seed

# 5. Register the AI Gateway (pushes provider keys from vault into gateway)
make register-gateway

# 6. Register prompts (seeds the MLflow Prompt Registry)
make register-prompts

# 7. Enrol TOTP for Telegram auth
docker compose run --rm telegram python -m scripts.setup_telegram_totp

# 8. Authorize Blogger (opens browser for OAuth consent)
python scripts/blogger_authorize.py

# 9. Message your bot
#    /topic <TOTP-code> What are vector embeddings and why do they matter?
```

### Makefile Targets

Run `make help` for the full list. Key targets: `build`, `up`, `down`, `logs`, `migrate`, `seed`, `register-gateway`, `register-prompts`, `smoke`, `psql`, `clean`.

## Documentation

- **[Architecture & Feature Overview](docs/ARCHITECTURE.md)** — system design, service inventory, pipeline detail, data model, configuration, model routing, observability, design decisions
- **[AI Gateway](docs/AI_GATEWAY.md)** — operator runbook for the MLflow AI Gateway
- **[Prompt Registry](docs/PROMPT_REGISTRY.md)** — editing, backup, failure modes
- **[Telegram Usage](docs/TELEGRAM_USAGE.md)** — command reference and test walkthrough

## Tech Stack

Python 3.12 · LangChain 0.3 · LangGraph 0.2 · MLflow 3.16 · PostgreSQL 16 · Docker Compose · Anthropic Claude (Opus 5, Sonnet 5, Haiku 4.5) · Google Blogger API v3 · python-telegram-bot 21.8

## License

This project is provided as-is for educational and reference purposes.
