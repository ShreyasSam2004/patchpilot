.DEFAULT_GOAL := help
PY := backend/.venv/Scripts/python.exe
ifeq ($(shell test -f backend/.venv/bin/python && echo yes),yes)
PY := backend/.venv/bin/python
endif

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup
.PHONY: install
install: install-backend install-frontend ## Install everything

.PHONY: install-backend
install-backend: ## Create the venv and install the backend
	cd backend && python -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	cd backend && ../$(PY) -m pip install -e ".[dev,anthropic,openai,mcp]"

.PHONY: install-swebench
install-swebench: ## Install the SWE-bench harness and dataset tooling
	cd backend && ../$(PY) -m pip install -e ".[swebench]"

.PHONY: install-frontend
install-frontend: ## Install frontend dependencies
	cd frontend && npm install

# ------------------------------------------------------------------ run
.PHONY: dev
dev: ## Print the two commands to run locally
	@echo "Terminal 1:  make serve"
	@echo "Terminal 2:  make web"

.PHONY: serve
serve: ## Start the API with an embedded worker
	cd backend && ../$(PY) -m patchpilot.cli serve

.PHONY: worker
worker: ## Start a standalone worker process
	cd backend && ../$(PY) -m patchpilot.worker

.PHONY: web
web: ## Start the frontend in development mode
	cd frontend && npm run dev

.PHONY: docker-image
docker-image: ## Build the sandbox runner image (git + pytest, for --sandbox docker)
	docker build -f backend/docker/runner.Dockerfile -t patchpilot/runner:latest backend

.PHONY: doctor
doctor: ## Check that a real run can actually work here
	cd backend && ../$(PY) -m patchpilot.cli doctor

# ----------------------------------------------------------------- test
.PHONY: test
test: ## Run the backend test suite
	cd backend && ../$(PY) -m pytest -q

.PHONY: test-unit
test-unit: ## Run only the fast unit tests
	cd backend && ../$(PY) -m pytest -q -m "not integration"

.PHONY: lint
lint: ## Lint and type-check the backend
	cd backend && ../$(PY) -m ruff check .
	cd backend && ../$(PY) -m mypy patchpilot

.PHONY: format
format: ## Auto-fix lint findings
	cd backend && ../$(PY) -m ruff check . --fix

.PHONY: typecheck-web
typecheck-web: ## Type-check the frontend
	cd frontend && npm run typecheck

.PHONY: build-web
build-web: ## Production build of the frontend
	cd frontend && npm run build

.PHONY: check
check: lint test typecheck-web build-web ## Everything CI would run

# ------------------------------------------------------------- fixtures
.PHONY: fixtures
fixtures: ## List the bundled fixture bug tasks
	cd backend && ../$(PY) -m patchpilot.cli fixtures

.PHONY: fix-calculator
fix-calculator: ## Run the agent on the calculator fixture
	cd backend && ../$(PY) -m patchpilot.cli run --fixture calculator_bug

.PHONY: fix-auth
fix-auth: ## Run the agent on the auth-token fixture
	cd backend && ../$(PY) -m patchpilot.cli run --fixture auth_token_bug

.PHONY: fix-pagination
fix-pagination: ## Run the agent on the pagination fixture
	cd backend && ../$(PY) -m patchpilot.cli run --fixture pagination_bug

.PHONY: seed
seed: ## Seed the local database with real agent runs (for UI work)
	cd backend && ../$(PY) scripts/seed_demo_runs.py

.PHONY: screenshots
screenshots: ## Capture README screenshots (needs both servers running)
	cd frontend && node scripts/screenshots.mjs

# ------------------------------------------------------------ benchmark
.PHONY: bench-list
bench-list: ## List SWE-bench Lite instances
	cd backend && ../$(PY) -m patchpilot.cli bench list --limit 20

.PHONY: bench
bench: ## Run a 10-instance SWE-bench Lite batch and evaluate it
	cd backend && ../$(PY) -m patchpilot.cli bench run --limit 10

.PHONY: bench-bullet
bench-bullet: ## Print a resume bullet from real evaluated results
	cd backend && ../$(PY) -m patchpilot.cli bench resume-bullet

# ----------------------------------------------------------------- misc
.PHONY: tools
tools: ## Show the tool registry and the policy verdict for each tool
	cd backend && ../$(PY) -m patchpilot.cli tools list

.PHONY: clean
clean: ## Remove caches, workspaces and the local database
	rm -rf backend/data backend/workspaces backend/logs
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache
	find backend -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf frontend/.next
