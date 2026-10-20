"""Cross-platform launcher; only the Python standard library is needed here."""
from ios_timeout.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
