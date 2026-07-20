#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_python="$repo_dir/.venv/bin/python"
model_root="$repo_dir/.bildebank-openclip"
model_names=("ViT-B-32" "ViT-L-14")
pretrained_names=("laion2b_s34b_b79k" "laion2b_s32b_b82k")

if [[ ! -x "$venv_python" ]]; then
    echo "Fant ikke Python i .venv. Installer Bildebank først." >&2
    exit 1
fi

cd "$repo_dir"

echo "Installerer valgfri OpenCLIP-støtte i Bildebanks lokale Python-miljø"
"$venv_python" -m pip install -e '.[openclip]'

mkdir -p -- "$model_root"
smoke_test="$(mktemp "${TMPDIR:-/tmp}/bildebank-openclip.XXXXXX.py")"
trap 'rm -f -- "$smoke_test"' EXIT

cat >"$smoke_test" <<'PY'
import sys
from pathlib import Path

import open_clip

model_root = Path(sys.argv[1])
model_name = sys.argv[2]
pretrained = sys.argv[3]

model_root.mkdir(parents=True, exist_ok=True)
open_clip.create_model_and_transforms(
    model_name,
    pretrained=pretrained,
    device="cpu",
    cache_dir=str(model_root),
)
open_clip.get_tokenizer(model_name)
print(f"OpenCLIP klar: {model_name} ({pretrained})")
PY

echo "Modellmappe:"
echo "  $model_root"
for index in "${!model_names[@]}"; do
    model_name="${model_names[$index]}"
    pretrained="${pretrained_names[$index]}"
    echo "Laster ned og tester OpenCLIP-modell:"
    echo "  $model_name ($pretrained)"
    "$venv_python" "$smoke_test" "$model_root" "$model_name" "$pretrained"
done

echo "Ferdig. OpenCLIP er installert med modeller:"
for index in "${!model_names[@]}"; do
    echo "  ${model_names[$index]} (${pretrained_names[$index]})"
done
