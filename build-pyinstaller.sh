#!/usr/bin/env sh
set -eu

if [ -x ./.venv/bin/python ]; then
	PYTHON=./.venv/bin/python
else
	PYTHON=python3
fi

"$PYTHON" -m pip install -r requirements-build.txt
"$PYTHON" -m PyInstaller --clean tuiemail.spec

printf '\nBuilt binary: %s\n' "$(pwd)/dist/tuiemail"