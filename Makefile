PYTHON := python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

.PHONY: setup run test clean

setup:
	$(PYTHON) -m venv --clear $(VENV)
	$(VENV_PIP) install -r requirements.txt
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@echo ""
	@echo "Setup complete."
	@echo "Next: add your OpenAI API key to .env, then run: make run"

run:
	@if [ -x "$(VENV_PYTHON)" ]; then $(VENV_PYTHON) main.py; else $(PYTHON) main.py; fi

test:
	@if [ -x "$(VENV_PYTHON)" ]; then $(VENV_PYTHON) -m pytest -q; else $(PYTHON) -m pytest -q; fi

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache
