#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="coverage-reports"
mkdir -p "$OUT_DIR"

GATEWAY_ROOT="${GATEWAY_ROOT:-adyen_direct_intergration}"
WRITE_MYSQL="${WRITE_MYSQL:-0}"

echo "Generating gateway automation coverage from: ${GATEWAY_ROOT}"
echo "WRITE_MYSQL=${WRITE_MYSQL}"

if [[ "$WRITE_MYSQL" == "1" || "$WRITE_MYSQL" == "true" || "$WRITE_MYSQL" == "TRUE" ]]; then
  python3 scripts/summary.py "$GATEWAY_ROOT" --write-mysql > "$OUT_DIR/gateway_module_coverage.txt"
else
  python3 scripts/summary.py "$GATEWAY_ROOT" > "$OUT_DIR/gateway_module_coverage.txt"
fi

echo "Reports written to: $OUT_DIR"

