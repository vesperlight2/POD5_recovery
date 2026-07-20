#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  batch_rebuild_pod5.sh INPUT_DIR OUTPUT_DIR WORK_ROOT

Behavior:
  - Recursively finds *.pod5 below INPUT_DIR.
  - Preserves relative subdirectories below OUTPUT_DIR.
  - Creates one independent work directory and log per input file.
  - Uses --validate-signal for every recovered read.
  - Never overwrites an existing rebuilt POD5.

Run this script sequentially first. Parallel rebuilding can saturate storage I/O.
USAGE
}

if [[ $# -ne 3 ]]; then
  usage >&2
  exit 2
fi

INPUT_DIR=$(cd "$1" && pwd)
mkdir -p "$2" "$3"
OUTPUT_DIR=$(cd "$2" && pwd)
WORK_ROOT=$(cd "$3" && pwd)
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REBUILDER="$SCRIPT_DIR/rebuild_pod5_from_sections_v2.py"
VALIDATOR="$SCRIPT_DIR/validate_rebuilt_pod5.py"

SUMMARY="$WORK_ROOT/batch_summary.tsv"
if [[ ! -e "$SUMMARY" ]]; then
  printf 'input\toutput\tstatus\trecovered_reads\tskipped_reads\tlog\n' > "$SUMMARY"
fi

while IFS= read -r -d '' input; do
  relative=${input#"$INPUT_DIR"/}
  relative_no_ext=${relative%.pod5}
  output="$OUTPUT_DIR/$relative_no_ext.rebuilt.pod5"
  work="$WORK_ROOT/$relative_no_ext.sections"
  log="$WORK_ROOT/$relative_no_ext.rebuild.log"
  validation="$WORK_ROOT/$relative_no_ext.validation.json"

  mkdir -p "$(dirname "$output")" "$(dirname "$work")" "$(dirname "$log")"

  if [[ -e "$output" ]]; then
    printf '%s\t%s\tSKIPPED_OUTPUT_EXISTS\t\t\t%s\n' \
      "$input" "$output" "$log" | tee -a "$SUMMARY"
    continue
  fi

  echo "===== REBUILD: $input =====" | tee "$log"
  set +e
  python "$REBUILDER" \
    "$input" "$output" \
    --work-dir "$work" \
    --validate-signal \
    2>&1 | tee -a "$log"
  rc=${PIPESTATUS[0]}
  set -e

  if [[ $rc -ne 0 || ! -s "$output" ]]; then
    rm -f "$output"
    printf '%s\t%s\tFAILED\t\t\t%s\n' \
      "$input" "$output" "$log" | tee -a "$SUMMARY"
    continue
  fi

  recovered=$(awk -F= '/^recovered_reads=/{v=$2} END{print v}' "$log")
  skipped=$(awk -F= '/^skipped_reads=/{v=$2} END{print v}' "$log")

  set +e
  python "$VALIDATOR" "$output" --json "$validation" >> "$log" 2>&1
  validate_rc=$?
  set -e

  if [[ $validate_rc -eq 0 ]]; then
    status=PASS
  else
    status=VALIDATION_FAILED
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$input" "$output" "$status" "${recovered:-}" "${skipped:-}" "$log" \
    | tee -a "$SUMMARY"
done < <(find "$INPUT_DIR" -type f -name '*.pod5' -print0)

echo "Batch summary: $SUMMARY"
