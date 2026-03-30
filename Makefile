.PHONY: help bootstrap install uninstall dev release linux-release win-release venv deps build-deps clean clean-build

VENV := .venv
APP := tui_email.py
SPEC := tuiemail.spec
PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin

ifeq ($(OS),Windows_NT)
PYTHON := py
VENV_PY := $(VENV)/Scripts/python.exe
DIST_BIN := dist/tuiemail.exe
INSTALL_CMD := copy
UNINSTALL_CMD := del /F
else
PYTHON := python3
VENV_PY := $(VENV)/bin/python
DIST_BIN := dist/tuiemail
INSTALL_CMD := install
UNINSTALL_CMD := rm -f
endif

help:
	@echo "Targets:"
	@echo "  make bootstrap      - Create/update venv and install runtime + build deps"
	@echo "  make install        - Build and install binary to $(BINDIR) (unix only)"
	@echo "  make uninstall      - Remove installed binary from $(BINDIR)"
	@echo "  make dev            - Create/update venv, install app deps, run app"
	@echo "  make release        - Build for current platform (auto OS detect)"
	@echo "  make linux-release  - Build Linux binary"
	@echo "  make win-release    - Build Windows binary"
	@echo "  make deps           - Install runtime dependencies"
	@echo "  make build-deps     - Install build dependencies"
	@echo "  make clean          - Remove Python cache files"
	@echo "  make clean-build    - Remove build/dist artifacts"

$(VENV_PY):
	$(PYTHON) -m venv $(VENV)

venv: $(VENV_PY)

deps: $(VENV_PY)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements.txt

build-deps: $(VENV_PY)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements-build.txt

bootstrap: deps build-deps

dev: deps
	$(VENV_PY) $(APP)

release: build-deps clean-build
	$(VENV_PY) -m PyInstaller --clean $(SPEC)
	@echo "Built binary: $(PWD)/$(DIST_BIN)"

linux-release: build-deps clean-build
	$(VENV_PY) -m PyInstaller --clean --name tuiemail $(SPEC)
	@echo "Built binary: $(PWD)/dist/tuiemail"

win-release: build-deps clean-build
	$(VENV_PY) -m pip install windows-curses
	$(VENV_PY) -m PyInstaller --clean --name tuiemail $(SPEC)
	@echo "Built binary: $(PWD)/dist/tuiemail.exe"

install: release
	@if [ "$(OS)" = "Windows_NT" ]; then echo "install is not supported on Windows via make"; exit 1; fi
	install -d "$(DESTDIR)$(BINDIR)"
	install -m 755 "$(DIST_BIN)" "$(DESTDIR)$(BINDIR)/tuiemail"
	@echo "Installed: $(DESTDIR)$(BINDIR)/tuiemail"

uninstall:
	$(UNINSTALL_CMD) "$(DESTDIR)$(BINDIR)/tuiemail"
	@echo "Removed: $(DESTDIR)$(BINDIR)/tuiemail"

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f -name "*.py[co]" -delete

clean-build:
	rm -rf build dist *.spec.bak
