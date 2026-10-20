"""Compatibility entry point for source-tree dependency checks."""
import importlib.metadata
from ios_timeout.dependencies import check, main

if __name__ == "__main__":
    raise SystemExit(main())
