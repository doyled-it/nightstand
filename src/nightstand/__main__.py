"""Allow ``python -m nightstand``."""

from __future__ import annotations

import sys

from nightstand.cli import main

if __name__ == "__main__":
    sys.exit(main())
