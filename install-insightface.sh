#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_python="$repo_dir/.venv/bin/python"
config_example="$repo_dir/bildebank-config.example.toml"
config_file="$repo_dir/bildebank-config.toml"

if [[ ! -x "$venv_python" ]]; then
    echo "Fant ikke Python i .venv. Installer Bildebank først." >&2
    exit 1
fi

if ! "$venv_python" -c 'import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("GL") else 1)'; then
    echo "Fant ikke systembiblioteket libGL." >&2
    echo "På Debian/Ubuntu/WSL installerer du det med:" >&2
    echo "  sudo apt install libgl1" >&2
    exit 1
fi

cd "$repo_dir"

echo "Installerer valgfri InsightFace-støtte i Bildebanks lokale Python-miljø"
"$venv_python" -m pip install -e '.[face]'

if [[ ! -f "$config_file" ]]; then
    cp -- "$config_example" "$config_file"
    echo "Opprettet config-fil:"
    echo "  $config_file"
    echo "Endre enabled = true hvis du vil slå på testing senere."
fi

echo "Ferdig. Sjekk status med:"
echo "  bildebank doctor"
