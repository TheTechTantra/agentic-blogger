.PHONY: build up down logs ps shell psql clean nuke smoke push help migrate seed register-prompts export-prompts

help:
	@echo "Targets:"
	@echo "  build       - docker compose build (all local images)"
	@echo "  up          - docker compose up -d"
	@echo "  down        - docker compose down"
	@echo "  logs        - docker compose logs -f"
	@echo "  ps          - docker compose ps"
	@echo "  shell       - docker compose exec orchestrator bash"
	@echo "  psql        - docker compose exec postgres psql -U agentic_blogger blogger"
	@echo "  migrate     - alembic upgrade head"
	@echo "  seed        - ./scripts/seed_secrets.sh"
	@echo "  smoke       - Run smoke tests (steps 7-10 of build order)"
	@echo "  register-prompts - Seed the MLflow Prompt Registry (bootstrap)"
	@echo "  export-prompts   - Back up the registry to prompts_backup/"
	@echo "  clean       - Remove containers and volumes"
	@echo "  nuke        - Clean + rm data/ directory"
	@echo "  push        - ERROR: pushing images violates requirement 7"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

shell:
	docker compose exec orchestrator bash

psql:
	docker compose exec postgres psql -U agentic_blogger blogger

migrate:
	docker compose run --rm orchestrator alembic upgrade head

seed:
	./scripts/seed_secrets.sh

smoke:
	@echo "Running smoke tests..."
	docker compose run --rm orchestrator python -m scripts.smoke_prompts
	docker compose run --rm orchestrator python -m scripts.smoke_llm
	docker compose run --rm orchestrator python -m scripts.smoke_search
	docker compose run --rm orchestrator python -m scripts.smoke_blogger

register-prompts:
	@echo "Seeding the MLflow Prompt Registry (creates new versions)..."
	docker compose run --rm orchestrator python -m scripts.register_prompts

export-prompts:
	@echo "Backing up the prompt registry to prompts_backup/ ..."
	docker compose run --rm orchestrator python -m scripts.export_prompts

clean:
	docker compose down -v

nuke: clean
	rm -rf data/

push:
	@echo "ERROR: Pushing images violates requirement 7 (no images on Docker Hub)"
	@echo "All agentic-blogger/* images are built and run locally."
	@false
