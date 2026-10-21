"""Bounded execution configuration."""
import os

DEFAULT_MAX_TOOL_STEPS = 100
MAX_ALLOWED_TOOL_STEPS = 200
DEFAULT_COMPACT_EVERY = 4


def _bounded_int(raw: object, low: int, high: int, message: str) -> int:
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)  # type: ignore[arg-type]
        if str(value) != str(raw).strip() or not low <= value <= high:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(message) from None
    return value


def max_tool_steps(explicit: int | None = None) -> int:
    raw = explicit if explicit is not None else os.getenv("KARRY_MAX_TOOL_STEPS", str(DEFAULT_MAX_TOOL_STEPS))
    return _bounded_int(raw, 1, MAX_ALLOWED_TOOL_STEPS,
                        f"KARRY_MAX_TOOL_STEPS/max_steps must be an integer from 1 to {MAX_ALLOWED_TOOL_STEPS}")


def compact_every() -> int:
    """Tool steps between state compactions; 0 disables compaction."""
    raw = os.getenv("KARRY_COMPACT_EVERY", str(DEFAULT_COMPACT_EVERY))
    return _bounded_int(raw, 0, MAX_ALLOWED_TOOL_STEPS,
                        f"KARRY_COMPACT_EVERY must be an integer from 0 to {MAX_ALLOWED_TOOL_STEPS}")
