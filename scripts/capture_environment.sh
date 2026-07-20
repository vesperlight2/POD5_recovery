#!/usr/bin/env bash
set -euo pipefail

OUTDIR="${1:-pod5_environment_record}"
mkdir -p "$OUTDIR"

python --version > "$OUTDIR/python_version.txt" 2>&1
python -m pip --version > "$OUTDIR/pip_version.txt" 2>&1
python -m pip freeze > "$OUTDIR/requirements.lock.txt"

python - <<'PY' > "$OUTDIR/package_versions.txt"
import platform
import sys
import numpy
import pod5
import pyarrow

print(f"platform={platform.platform()}")
print(f"python={sys.version.replace(chr(10), ' ')}")
print(f"pod5={getattr(pod5, '__version__', 'unknown')}")
print(f"pyarrow={pyarrow.__version__}")
print(f"numpy={numpy.__version__}")
PY

uname -a > "$OUTDIR/uname.txt" 2>&1 || true
printf 'Environment record written to: %s\n' "$OUTDIR"
