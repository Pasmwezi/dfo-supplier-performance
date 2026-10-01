"""Organization-neutral configuration with backwards-compatible deployment keys."""
import os
from typing import overload


@overload
def env(name: str, default: str) -> str: ...


@overload
def env(name: str, default: None = None) -> str | None: ...


def env(name: str, default: str | None = None) -> str | None:
    """Prefer SPM_* keys, falling back to the original deployment prefix.

    An explicitly empty canonical value does not revive a legacy secret/value.
    """
    canonical = name.removeprefix("DFO_") if name.startswith("DFO_SPM_") else name
    if canonical in os.environ:
        return os.environ[canonical]
    legacy = "DFO_" + canonical if canonical.startswith("SPM_") else canonical
    return os.environ.get(legacy, default)
