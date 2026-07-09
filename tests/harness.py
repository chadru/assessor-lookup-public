"""Repository CLI wrapper for the packaged regression harness."""

from assessor_lookup.harness import *  # noqa: F401,F403
from assessor_lookup.harness import main


if __name__ == "__main__":
    raise SystemExit(main())
