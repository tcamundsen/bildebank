from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from importlib import metadata
from pathlib import Path
import platform
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bildebank.config import load_config  # noqa: E402


PACKAGES = (
    "insightface",
    "onnxruntime",
    "onnxruntime-gpu",
    "opencv-python",
    "opencv-python-headless",
    "numpy",
    "Pillow",
)


def collect_report(repo_root: Path) -> str:
    lines = [
        "Bildebank: feilsøking av face-scan",
        f"Tidspunkt: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Maskin: {platform.node()}",
        f"System: {platform.platform()}",
        f"Prosessor: {platform.processor()}",
        f"Programmappe: {repo_root}",
        f"Python: {sys.version}",
        f"Python-program: {sys.executable}",
    ]
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines.append(
            f"Git-revisjon: {revision.stdout.strip() if revision.returncode == 0 else 'ukjent'}"
        )
    except (OSError, subprocess.TimeoutExpired):
        lines.append("Git-revisjon: ukjent")

    lines.extend(("", "Installerte biblioteker:"))
    for name in PACKAGES:
        try:
            lines.append(f"{name}: {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            lines.append(f"{name}: ikke installert")

    lines.extend(("", "Modell og konfigurert kjøring:"))
    try:
        config = load_config(repo_root, migrate_legacy=False).face_recognition
        model_dir = config.model_root / "models" / config.model_name
        lines.extend(
            (
                f"Modell: {config.model_name}",
                f"Konfigurert provider: {config.provider}",
                f"Modellmappe: {model_dir}",
                "",
                "SHA-256 av modellfiler:",
            )
        )
        models = sorted(model_dir.glob("*.onnx"))
        if not models:
            lines.append("FEIL: Fant ingen ONNX-modellfiler i modellmappen.")
        for path in models:
            try:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                lines.append(f"{path.name}: {digest}")
            except OSError as exc:
                lines.append(f"FEIL: Kunne ikke lese {path.name}: {exc}")
    except (OSError, ValueError) as exc:
        lines.append(f"FEIL: Kunne ikke lese modelloppsettet: {exc}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lag en rapport for sammenligning av face-scan på to maskiner."
    )
    parser.add_argument("--output", type=Path, help="Valgfri plassering av rapporten.")
    args = parser.parse_args(argv)
    output = args.output or REPO_ROOT / "face-scan-diagnose.txt"
    report = collect_report(REPO_ROOT)
    try:
        with output.open("w", encoding="utf-8") as stream:
            stream.write(report)
    except OSError as exc:
        print(f"Kunne ikke lagre rapporten: {exc}", file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print(report)
    print(f"Rapport lagret i: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
