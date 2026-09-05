# GG Orchestrator — AI Engineering Mission Control
.PHONY: dev backend frontend test test-backend test-frontend lint typecheck build install smoke

BACKEND_DIR := backend
FRONTEND_DIR := frontend
UV := uv

install:
	cd $(BACKEND_DIR) && $(UV) sync
	cd $(FRONTEND_DIR) && npm install

dev:
	@echo "Starting backend (8787) + frontend (5173)..."
	@trap 'kill 0' INT TERM; \
	(cd $(BACKEND_DIR) && $(UV) run gg-backend) & \
	(cd $(FRONTEND_DIR) && npm run dev) & \
	wait

backend:
	cd $(BACKEND_DIR) && $(UV) run gg-backend

frontend:
	cd $(FRONTEND_DIR) && npm run dev

test: test-backend test-frontend

test-backend:
	cd $(BACKEND_DIR) && $(UV) run pytest -q

test-frontend:
	cd $(FRONTEND_DIR) && npm run test -- --run

lint:
	cd $(BACKEND_DIR) && $(UV) run ruff check src tests
	cd $(FRONTEND_DIR) && npm run lint

typecheck:
	cd $(BACKEND_DIR) && $(UV) run mypy src
	cd $(FRONTEND_DIR) && npm run typecheck

build:
	cd $(BACKEND_DIR) && $(UV) build
	cd $(FRONTEND_DIR) && npm run build

smoke:
	cd $(BACKEND_DIR) && $(UV) run python -m orchestrator.smoke
