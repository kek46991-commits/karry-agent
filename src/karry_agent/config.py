"""Bounded execution configuration."""
import os

DEFAULT_MAX_TOOL_STEPS = 20


def max_tool_steps(explicit: int | None = None) -> int:
    raw = explicit if explicit is not None else os.getenv("KARRY_MAX_TOOL_STEPS", str(DEFAULT_MAX_TOOL_STEPS))
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
        if str(value) != str(raw).strip() or not 1 <= value <= 50:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("KARRY_MAX_TOOL_STEPS/max_steps must be an integer from 1 to 50") from None
    return value
