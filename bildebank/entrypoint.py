from __future__ import annotations

import sys


LAUNCHER_COMMANDS = {"start", "launcher"}


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) == 1 and argv[0] in LAUNCHER_COMMANDS:
        from .launcher import main as launcher_main

        try:
            return launcher_main()
        except KeyboardInterrupt:
            print("Avbrutt.", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 - keep the lightweight launcher error readable
            print(f"Feil: {exc}", file=sys.stderr)
            return 1

    from .cli import main as cli_main

    return cli_main()
