# Every target below runs with NO AWS credentials. That is deliberate: the whole
# repository is verifiable offline, which is what makes CI meaningful.

PYTHON ?= python3

.PHONY: help lint test sim finops fmt validate all clean venv

help:
	@echo "make lint      - ruff over the Python sources"
	@echo "make test      - unit tests: FSM boundaries, adapter calls, replay-vs-telemetry"
	@echo "make sim       - replay the captured telemetry, regenerate results/fig*.png"
	@echo "make finops    - print the cost comparison, regenerate results/finops.png"
	@echo "make fmt       - terraform fmt -check (requires terraform)"
	@echo "make validate  - terraform validate + tflint + checkov (requires terraform)"
	@echo "make all       - lint test sim finops validate"
	@echo ""
	@echo "Note: fmt/validate need terraform, tflint and checkov on PATH."
	@echo "They run in CI on every push; locally 'make test sim finops' is the usual loop."

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest -q

sim:
	$(PYTHON) -m sim.replay

finops:
	$(PYTHON) -m finops.cost_model

fmt:
	terraform -chdir=terraform fmt -check -recursive

validate:
	terraform -chdir=terraform init -backend=false -input=false
	terraform -chdir=terraform validate
	tflint --chdir=terraform --init
	tflint --chdir=terraform
	checkov -d terraform --quiet --compact

all: lint test sim finops validate

venv:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	@echo "Activate with: source .venv/bin/activate"

clean:
	rm -rf .pytest_cache __pycache__ */__pycache__ *.egg-info
