#!/usr/bin/env bash
# Serial per-workspace: discover -> plan -> (stop on BLOCKED/AMBIGUOUS) -> apply -> verify
#
# Prerequisites (same as 00_instruction-process.txt):
#   source .venv/bin/activate
#   export GD_HOST="https://ringcentral-dev.cloud.gooddata.com"
#   export GD_TOKEN   # already set
#
# Usage:
#   ./scripts/run-clients-serially.sh
#   START_FROM=biz-eo-p01_20560000 ./scripts/run-clients-serially.sh
#   DRY_RUN=1 ./scripts/run-clients-serially.sh          # discover+plan only
#   NATIVE_ONLY=1 ./scripts/run-clients-serially.sh      # discover --native-only
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CLIENTS_FILE="${CLIENTS_FILE:-$ROOT/scripts/clients-batch.txt}"
LOG_DIR="${LOG_DIR:-$ROOT/runs/batch-logs}"
mkdir -p "$LOG_DIR"

if [[ -z "${GD_HOST:-}" ]]; then
  echo "ERROR: GD_HOST is not set" >&2
  exit 1
fi
if [[ -z "${GD_TOKEN:-}" ]]; then
  echo "ERROR: GD_TOKEN is not set" >&2
  exit 1
fi

CLIENTS=()
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  CLIENTS+=("$line")
done < "$CLIENTS_FILE"
if [[ ${#CLIENTS[@]} -eq 0 ]]; then
  echo "ERROR: no clients in $CLIENTS_FILE" >&2
  exit 1
fi

skipping=0
if [[ -n "${START_FROM:-}" ]]; then
  skipping=1
fi

latest_run_dir() {
  python3 - <<'PY'
from pathlib import Path
from migration_tool.runner import latest_run
print(latest_run(Path("runs")))
PY
}

check_plan_gate() {
  local run_dir="$1"
  local workspace="$2"
  python3 - "$run_dir" "$workspace" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
expected_ws = sys.argv[2]
plan_path = run_dir / "plan.json"
doc = json.loads(plan_path.read_text(encoding="utf-8"))
actual_ws = doc.get("workspace")
if actual_ws != expected_ws:
    print(
        f"ERROR: plan workspace {actual_ws!r} != GD_WORKSPACE {expected_ws!r} ({plan_path})",
        file=sys.stderr,
    )
    sys.exit(1)
counts = doc.get("row_counts") or {}
blocked = int(counts.get("BLOCKED") or 0)
ambiguous = int(counts.get("AMBIGUOUS") or 0)
print(
    f"Plan gate {expected_ws}: BLOCKED={blocked} AMBIGUOUS={ambiguous} "
    f"READY={counts.get('READY', 0)} writes={doc.get('write_count', 0)}"
)
if blocked or ambiguous:
    print(
        f"STOP: plan has BLOCKED/AMBIGUOUS rows. Inspect {run_dir / 'plan.md'}",
        file=sys.stderr,
    )
    sys.exit(1)
PY
}

echo "Host: $GD_HOST"
echo "Clients: ${#CLIENTS[@]} from $CLIENTS_FILE"
[[ -n "${START_FROM:-}" ]] && echo "Resume from: $START_FROM"
[[ "${DRY_RUN:-}" == "1" ]] && echo "DRY_RUN=1 — apply/verify skipped"
echo

for ws in "${CLIENTS[@]}"; do
  if [[ "$skipping" -eq 1 ]]; then
    if [[ "$ws" == "$START_FROM" ]]; then
      skipping=0
    else
      echo "----- skip $ws"
      continue
    fi
  fi

  export GD_WORKSPACE="$ws"
  scope="input/discovered-scope-${ws}.csv"
  log="$LOG_DIR/${ws}.log"
  discover_args=(discover --output "$scope")
  if [[ "${NATIVE_ONLY:-}" == "1" ]]; then
    discover_args+=(--native-only)
  fi

  echo "===== $ws ====="
  echo "log: $log"
  {
    echo "===== $(date +%Y-%m-%dT%H:%M:%S%z) workspace=$ws host=$GD_HOST ====="
    echo "GD_WORKSPACE=$GD_WORKSPACE"

    echo
    echo "--- 1/4 discover -> $scope"
    python3 migrate.py "${discover_args[@]}"

    echo
    echo "--- 2/4 plan --scope $scope"
    python3 migrate.py --scope "$scope" plan

    run_dir="$(latest_run_dir)"
    echo "run: $run_dir"

    echo
    echo "--- 3/4 plan gate (stop on BLOCKED/AMBIGUOUS)"
    check_plan_gate "$run_dir" "$ws"

    if [[ "${DRY_RUN:-}" == "1" ]]; then
      echo "DRY_RUN: skip apply/verify"
    else
      echo
      echo "--- 4/4 apply + verify"
      python3 migrate.py --run "$run_dir" apply \
        --confirm-host "$GD_HOST" \
        --confirm-workspace "$GD_WORKSPACE"
      python3 migrate.py --run "$run_dir" verify
      echo "DONE $ws"
    fi
  } 2>&1 | tee "$log"
done

echo
echo "All requested clients finished."
