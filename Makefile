.PHONY: up down logs ps restart build migrate test lint test-unit clean

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

restart:
	docker compose restart gateway worker

build:
	docker compose build

migrate:
	docker compose exec gateway alembic upgrade head

test:
	cd services/gateway && uv run --extra test python -m pytest ../../tests -v

test-unit:
	cd services/gateway && uv run --extra test python -m pytest ../../tests -x -q

lint:
	cd services/gateway && uv run python -m compileall -q app

clean:
	docker compose down -v
