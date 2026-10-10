.PHONY: up down logs ps restart build migrate test test-unit test-integration lint fmt typecheck audit config clean up-web

up:
	docker compose up -d --build --wait

# SearXNG lives behind the "web" profile.
up-web:
	docker compose --profile web up -d --build --wait

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

# Unit + integration suites. The DB/Redis suites skip themselves unless the
# TEST_*_URL variables point at live services; see docs/development.md.
test: test-unit test-integration

test-unit:
	cd services/gateway && uv run --extra test python -m pytest ../../tests -q

test-integration:
	cd services/gateway && uv run --extra test python -m pytest ../../tests -q \
		--ignore=../../tests/test_mcp_e2e.py

# Real ruff rules, not compileall: the old target only checked syntax, which
# missed undefined names and unused imports.
lint:
	uv run ruff check services tests tools
	uv run ruff format --check services tests tools

fmt:
	uv run ruff check services tests tools --fix
	uv run ruff format services tests tools

typecheck:
	uv run --with mypy mypy --ignore-missing-imports --no-strict-optional services/gateway/app

audit:
	uv run --with pip-audit pip-audit --requirement services/gateway/pyproject.toml

# Validates compose interpolation, Prometheus config and rules, and the
# Grafana dashboard JSON without starting anything.
config:
	docker compose config --quiet
	docker run --rm --entrypoint promtool -v "$(PWD)/monitoring/prometheus:/etc/prometheus:ro" \
		prom/prometheus:v2.53.0 check config /etc/prometheus/prometheus.yml
	docker run --rm --entrypoint promtool -v "$(PWD)/monitoring/prometheus:/etc/prometheus:ro" \
		prom/prometheus:v2.53.0 check rules /etc/prometheus/alerts.yml
	python3 -c "import json; json.load(open('monitoring/grafana/provisioning/dashboards/searchos.json')); print('dashboard ok')"

clean:
	docker compose down -v
