.PHONY: up down logs ps test lint format-check check

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

test:
	uv run pytest -q

lint:
	uv run ruff check .

format-check:
	uv run ruff format --check .

check: lint format-check test
