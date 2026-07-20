#!/usr/bin/env bash
set -euo pipefail

# Edit these paths before running.
INPUT='/path/to/damaged.pod5'
OUTDIR='/path/to/rebuild_output'
SCRIPT_DIR='/path/to/POD5_salvage_toolkit/scripts'

mkdir -p "$OUTDIR"
BASE=$(basename "$INPUT" .pod5)

# 1) Inspect embedded Arrow sections without writing a POD5.
python "$SCRIPT_DIR/rebuild_pod5_from_sections_v2.py" \
  "$INPUT" "$OUTDIR/${BASE}.inspect-placeholder.pod5" \
  --work-dir "$OUTDIR/${BASE}.inspect.sections" \
  --inspect-only \
  | tee "$OUTDIR/${BASE}.inspect.log"

# 2) Rebuild and validate 100 reads.
python "$SCRIPT_DIR/rebuild_pod5_from_sections_v2.py" \
  "$INPUT" "$OUTDIR/${BASE}.test100.pod5" \
  --work-dir "$OUTDIR/${BASE}.test100.sections" \
  --max-reads 100 \
  --validate-signal \
  | tee "$OUTDIR/${BASE}.test100.rebuild.log"

python "$SCRIPT_DIR/validate_rebuilt_pod5.py" \
  "$OUTDIR/${BASE}.test100.pod5" \
  --expected-reads 100 \
  --json "$OUTDIR/${BASE}.test100.validation.json"

# 3) Full rebuild. Run only after the test POD5 passes validation and Dorado.
python "$SCRIPT_DIR/rebuild_pod5_from_sections_v2.py" \
  "$INPUT" "$OUTDIR/${BASE}.rebuilt.pod5" \
  --work-dir "$OUTDIR/${BASE}.full.sections" \
  --validate-signal \
  | tee "$OUTDIR/${BASE}.full.rebuild.log"

python "$SCRIPT_DIR/validate_rebuilt_pod5.py" \
  "$OUTDIR/${BASE}.rebuilt.pod5" \
  --json "$OUTDIR/${BASE}.full.validation.json"
