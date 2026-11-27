# PowerManager: common developer tasks. `make help` lists them.
PY ?= python3
VENV ?= server/.venv
BIN := $(VENV)/bin

.PHONY: help venv test test-server test-firmware syntax-check e2e run sim firmware-setup firmware docker clean

help:
	@echo "make venv            create $(VENV) with the server + test requirements"
	@echo "make test            server tests + firmware PC tests (what CI runs, without the ESP32 builds)"
	@echo "make test-server     pytest only"
	@echo "make test-firmware   shared-file check, AUX automation, hardware profile, ESP32 syntax check, MQTT link test"
	@echo "make syntax-check    ESP32 sources against Arduino stubs (no arduino-cli needed)"
	@echo "make e2e             browser test of the hardware pages (server + simulated meter + Playwright)"
	@echo "make run             development server on http://localhost:5000"
	@echo "make firmware-setup  install the ESP32 core + libraries for arduino-cli (once)"
	@echo "make firmware        compile both sketches and the build variants with arduino-cli"
	@echo "make docker          build the server image"

venv: $(BIN)/pytest
$(BIN)/pytest: server/requirements.txt server/requirements-dev.txt
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q -r server/requirements-dev.txt
	@touch $(BIN)/pytest

test: test-server test-firmware

test-server: venv
	cd server && ../$(BIN)/python -m pytest -q

test-firmware: venv
	PYTHON=$(abspath $(BIN))/python firmware/tools/host_tests.sh

syntax-check:
	firmware/tools/syntax_check/run.sh

e2e: venv
	PYTHON=$(abspath $(BIN))/python server/tools/e2e/run.sh

run: venv
	cd server && ../$(BIN)/python wsgi.py

firmware-setup:
	firmware/tools/setup_arduino.sh

firmware:
	firmware/tools/build.sh PowerMeterPZEM
	firmware/tools/build.sh PowerMeterPZEM dc
	firmware/tools/build.sh PowerMeterRBAmp
	firmware/tools/build.sh PowerMeterRBAmp example
	firmware/tools/build.sh PowerMeterRBAmp all-parts
	firmware/tools/build.sh PowerMeterRBAmp ws-only
	firmware/tools/build.sh PowerMeterPZEM mqtt-only

docker:
	docker build -t powermanager:dev server

clean:
	rm -rf firmware/build firmware/tools/.cache server/.pytest_cache
