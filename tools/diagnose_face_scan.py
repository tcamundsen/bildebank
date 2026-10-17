from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from importlib import metadata
import os
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


def collect_report(repo_root: Path, image_path: Path | None = None) -> str:
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
        if image_path is not None:
            lines.extend(("", "Direkte test av valgt bilde (ingen databaseendringer):"))
            lines.extend(
                image_worker_report(image_path, model_dir / "scrfd_10g_bnkps.onnx")
            )
    except (OSError, ValueError) as exc:
        lines.append(f"FEIL: Kunne ikke lese modelloppsettet: {exc}")
    return "\n".join(lines) + "\n"


def image_worker_report(image_path: Path, detector_path: Path) -> list[str]:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
                "--image-worker",
                str(image_path),
                "--detector",
                str(detector_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"FEIL: Kunne ikke fullføre bildetesten: {exc}"]
    lines = result.stdout.strip().splitlines()
    if result.returncode:
        lines.append(
            f"FEIL: Bildetesten feilet: {result.stderr.strip() or result.returncode}"
        )
    return lines


def inspect_image(image_path: Path, detector_path: Path) -> None:
    import cv2
    from insightface.model_zoo import get_model
    from bildebank.face import read_image

    data = image_path.read_bytes()
    print(f"Bildefil: {image_path}")
    print(f"Bildefil SHA256: {hashlib.sha256(data).hexdigest()}")
    image = read_image(data, image_path)
    if image is None:
        raise ValueError("Kunne ikke dekode bildefilen")
    print(f"Lastet OpenCV: {cv2.__version__}")
    print(f"OpenCV-modul: {cv2.__file__}")
    print(
        f"OpenCV-bygg SHA256: {hashlib.sha256(cv2.getBuildInformation().encode()).hexdigest()}"
    )
    print(f"Dekodet bilde: {image.shape}, {image.dtype}")
    print(f"Dekodede piksler SHA256: {hashlib.sha256(image.tobytes()).hexdigest()}")

    # Load only this existing local detector; never download models or write scan results.
    if not detector_path.is_file():
        raise ValueError(f"Fant ikke antelopev2-detektoren: {detector_path}")
    detector = get_model(
        str(detector_path), providers=["CPUExecutionProvider"], download=False
    )
    if detector is None:
        raise ValueError("Kunne ikke laste ansiktsdetektoren")
    detector.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.5)
    print(f"Faktiske providers: {detector.session.get_providers()}")
    for threshold in (0.5, 0.1):
        detector.det_thresh = threshold
        boxes, _ = detector.detect(image)
        print(f"Deteksjonsgrense {threshold}: {len(boxes)} ansikter")
        for index, box in enumerate(boxes[:20], start=1):
            print(
                f"  {index}: score={float(box[4]):.9f}, boks="
                + ", ".join(f"{float(value):.3f}" for value in box[:4])
            )


def choose_image() -> Path | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(
            title="Velg bildet som gir ulikt antall ansikter"
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lag en rapport for sammenligning av face-scan på to maskiner."
    )
    parser.add_argument("--output", type=Path, help="Valgfri plassering av rapporten.")
    parser.add_argument(
        "image",
        nargs="?",
        type=Path,
        help="Valgfritt bilde som skal testes uten databaseendringer.",
    )
    parser.add_argument("--image-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--detector", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.image_worker is not None:
        try:
            if args.detector is None:
                raise ValueError("Detektorstien mangler")
            inspect_image(args.image_worker, args.detector)
            return 0
        except Exception as exc:  # noqa: BLE001 - retain the main report if native image analysis fails
            print(str(exc), file=sys.stderr)
            return 1
    image_path = args.image
    if image_path is None and os.name == "nt":
        try:
            image_path = choose_image()
        except Exception as exc:  # noqa: BLE001 - a missing file picker must not prevent a basic report
            print(f"Kunne ikke åpne bildefilvelgeren: {exc}", file=sys.stderr)
    output = args.output or REPO_ROOT / "face-scan-diagnose.txt"
    report = collect_report(REPO_ROOT, image_path)
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
