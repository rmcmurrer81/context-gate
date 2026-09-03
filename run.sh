#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: bash run.sh [app|lab|demo|acceptance|doctor|test] [--dev] [--skip-install]

Creates .venv on first use and runs ContextGate in safe local mode.

  app            Start the ContextGate web console on localhost (default)
  lab            Start the legacy Streamlit operator lab
  demo           Print the deterministic acceptance decision as JSON
  acceptance     Run the fictional real-world acceptance matrix
  doctor         Check dependencies, schemas, data, and the local fallback
  test           Run the test suite
  --dev           Install contributor tooling from requirements-dev.txt
  --skip-install  Use an already-prepared .venv without installing packages
EOF
}

task="app"
dependency_profile="app"
skip_install="false"
task_was_set="false"

while (($#)); do
  case "$1" in
    app|lab|demo|acceptance|doctor|test)
      if [[ "$task_was_set" == "true" ]]; then
        echo "Only one task may be selected." >&2
        usage >&2
        exit 2
      fi
      task="$1"
      task_was_set="true"
      ;;
    --dev)
      dependency_profile="dev"
      ;;
    --skip-install)
      skip_install="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
virtual_environment="$project_root/.venv"
virtual_python="$virtual_environment/bin/python"
requirements="$project_root/requirements.txt"
if [[ "$dependency_profile" == "dev" ]]; then
  requirements="$project_root/requirements-dev.txt"
fi

cd -- "$project_root"

if [[ ! -x "$virtual_python" ]]; then
  echo "Creating local virtual environment in .venv ..."
  if command -v python3 >/dev/null 2>&1; then
    bootstrap_python="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    bootstrap_python="$(command -v python)"
  else
    echo "Python 3.11 or newer was not found. Install Python, then run this script again." >&2
    exit 1
  fi
  "$bootstrap_python" -m venv "$virtual_environment"
fi

if [[ ! -x "$virtual_python" ]]; then
  echo ".venv exists but its Python executable is missing. Recreate .venv and try again." >&2
  exit 1
fi

"$virtual_python" -c \
  "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 'ContextGate requires Python 3.11 or newer.')"

if [[ "$skip_install" != "true" ]]; then
  hash_files=("$project_root/requirements.txt")
  if [[ "$dependency_profile" == "dev" ]]; then
    hash_files+=("$requirements")
  fi
  dependency_hash="$("$virtual_python" - "${hash_files[@]}" <<'PY'
import hashlib
import pathlib
import sys

digest = hashlib.sha256()
for filename in sys.argv[1:]:
    digest.update(pathlib.Path(filename).read_bytes())
print(digest.hexdigest()[:24])
PY
)"
  ready_marker="$virtual_environment/.context-gate-$dependency_profile-$dependency_hash.ready"
  if [[ ! -f "$ready_marker" ]]; then
    echo "Installing $dependency_profile dependencies ..."
    "$virtual_python" -m pip install --disable-pip-version-check -r "$requirements"
    : >"$ready_marker"
  fi
fi

# The public launchers are deliberately local-only. Workshop cloud integration
# remains an explicit, separate action and is never reached from this script.
export CONTEXTGATE_MODE="local"
export PYTHONUTF8="1"
export STREAMLIT_SERVER_SHOW_EMAIL_PROMPT="false"
export STREAMLIT_BROWSER_GATHER_USAGE_STATS="false"

app_url="http://127.0.0.1:8501"
health_url="$app_url/api/health"

contextgate_is_running() {
  "$virtual_python" - "$health_url" <<'PY' >/dev/null 2>&1
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
        if not 200 <= response.status < 300:
            raise SystemExit(1)
        payload = json.load(response)
except (OSError, ValueError):
    raise SystemExit(1)

raise SystemExit(
    0
    if isinstance(payload, dict)
    and payload.get("service") == "ContextGate"
    and payload.get("status") == "ok"
    else 1
)
PY
}

open_contextgate() {
  if command -v open >/dev/null 2>&1; then
    open "$app_url" >/dev/null 2>&1
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$app_url" >/dev/null 2>&1 &
  else
    printf 'ContextGate is already running at %s\n' "$app_url"
  fi
}

case "$task" in
  app)
    if contextgate_is_running; then
      printf 'ContextGate is already running at %s\n' "$app_url"
      open_contextgate
      exit 0
    fi
    exec "$virtual_python" -m context_gate.web_console
    ;;
  lab)
    exec "$virtual_python" -m streamlit run app.py \
      --server.address 127.0.0.1 \
      --server.maxUploadSize 10 \
      --server.showEmailPrompt false \
      --browser.gatherUsageStats false
    ;;
  demo)
    exec "$virtual_python" -m context_gate
    ;;
  acceptance)
    exec "$virtual_python" scripts/acceptance_matrix.py
    ;;
  doctor)
    exec "$virtual_python" scripts/doctor.py
    ;;
  test)
    exec "$virtual_python" -m pytest
    ;;
esac
