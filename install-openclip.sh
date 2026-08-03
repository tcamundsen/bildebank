#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_python="$repo_dir/.venv/bin/python"

if [[ ! -x "$venv_python" ]]; then
    echo "Fant ikke Python i .venv. Installer Bildebank først." >&2
    exit 1
fi

cd "$repo_dir"

echo "Installerer valgfri OpenCLIP-støtte i Bildebanks lokale Python-miljø"
"$venv_python" -m pip install -e '.[openclip]'

echo "Kontrollerer OpenCLIP-avhengighetene"
"$venv_python" - <<'PY'
import open_clip
import igraph
import sklearn
import torch
from importlib.metadata import version

print(
    "OpenCLIP klar: "
    f"open_clip_torch={version('open_clip_torch')}, "
    f"torch={torch.__version__}, scikit-learn={sklearn.__version__}, "
    f"igraph={igraph.__version__}"
)
PY

echo "Ferdig. OpenCLIP-avhengighetene er installert."
echo "Last ned valgt modell separat fra Oppsett-fanen."
