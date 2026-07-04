.PHONY: help proto install dev test check clean run config

help:
	@echo "Qbert0G - Makefile"
	@echo ""
	@echo "Available targets:"
	@echo "  make install  - Install the package (pip install .)"
	@echo "  make dev      - Editable install with dev extras"
	@echo "  make proto    - Regenerate protobuf stubs from .proto files"
	@echo "  make test     - Run the test suite"
	@echo "  make check    - Lint (ruff) + tests"
	@echo "  make run      - Run the server (needs ./config.yaml)"
	@echo "  make config   - Copy example config to config.yaml"
	@echo "  make clean    - Clean caches"

install:
	pip install .

dev:
	pip install -e .[dev]
	@echo ""
	@echo "NOTE: hardware devices additionally need pyqcc (wheel from Crypta Labs):"
	@echo "  pip install /path/to/pyqcc-x.y.z-py3-none-any.whl"

proto:
	python -m grpc_tools.protoc -Isrc \
		--python_out=src --grpc_python_out=src \
		src/qbert0g/proto/qrng.proto src/qbert0g/proto/entropy_service.proto \
		src/qbert0g/proto/purity_service.proto
	@echo "Regenerated src/qbert0g/proto/*_pb2*.py"

test:
	python -m pytest tests/ -v

check:
	python -m ruff check .
	python -m pytest tests/ -q

run:
	qbert0g serve

config:
	@if [ -f config.yaml ]; then \
		echo "config.yaml already exists. Not overwriting."; \
	else \
		cp config.yaml.example config.yaml; \
		echo "Created config.yaml from example. Please edit it!"; \
	fi

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info src/*.egg-info
