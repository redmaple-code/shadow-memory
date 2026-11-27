"""Allow ``python -m shadow_memory``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())