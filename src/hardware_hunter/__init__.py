"""hardware-hunter package root.

The full typer CLI skeleton lands in Story 1.8. This stub gives Story 1.3
a working ``--version`` flag so the Docker image satisfies its AC.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version


def main() -> None:
    if "--version" in sys.argv[1:]:
        try:
            print(version("hardware-hunter"))
        except PackageNotFoundError:
            print("unknown")
        return
    print("Hello from hardware-hunter!")
