UV ?= uv
RUN = $(UV) run --locked

.PHONY: sync check demo demo-live test-configs test-lab clean-lab format
sync:
	$(UV) sync --locked

check:
	$(RUN) ruff format --check src tests scripts deploy
	$(RUN) ruff check src tests scripts deploy
	$(RUN) pytest
	$(UV) build
	$(RUN) wayvia --help

format:
	$(RUN) ruff format src tests scripts deploy

demo:
	$(RUN) python -m wayvia.demo

demo-live:
	$(RUN) python -m wayvia.demo --serve

test-configs:
	$(RUN) python scripts/test_configs.py

test-lab:
	$(RUN) python scripts/lab.py

clean-lab:
	$(RUN) python scripts/lab.py --clean
