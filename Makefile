.PHONY: help bootstrap install uninstall dev release venv deps build-deps clean clean-build

VENV := .venv
VENV_PY := $(VENV)/bin/python
APP := tui_email.py
SPEC := tuiemail.spec
DIST_BIN := dist/tuiemail
PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin

help:
	@echo "Targets:"
	@echo "  make bootstrap - Create/update venv and install runtime + build deps"
	@echo "  make install   - Build and install binary to $(BINDIR) using install(1)"
	@echo "  make uninstall - Remove installed binary from $(BINDIR)"
	@echo "  make dev       - Create/update venv, install app deps, run app"
	@echo "  make release   - Create/update venv, install build deps, build binary"
	@echo "  make deps      - Install runtime dependencies"
	@echo "  make build-deps - Install build dependencies"
	@echo "  make clean     - Remove Python cache files"
	@echo "  make clean-build - Remove build/dist artifacts"

$(VENV_PY):
	python3 -m venv $(VENV)

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

install: release
	install -d "$(DESTDIR)$(BINDIR)"
	install -m 755 "$(DIST_BIN)" "$(DESTDIR)$(BINDIR)/tuiemail"
	@echo "Installed: $(DESTDIR)$(BINDIR)/tuiemail"

uninstall:
	rm -f "$(DESTDIR)$(BINDIR)/tuiemail"
	@echo "Removed: $(DESTDIR)$(BINDIR)/tuiemail"

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f -name "*.py[co]" -delete

clean-build:
	rm -rf build dist *.spec.bak
