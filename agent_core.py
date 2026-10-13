"""Compatibility imports for the supplied agent_core.py interface."""
from karry_agent.agent_core import (
    AGENT_TOOLS,
    add_calendar_event,
    dispatch_tool,
    execute_command,
    get_google_credentials,
    get_map_directions,
    list_dir,
    read_file,
    search_photos,
    write_file,
)

__all__ = ["AGENT_TOOLS", "add_calendar_event", "dispatch_tool", "execute_command",
           "get_google_credentials", "get_map_directions", "list_dir", "read_file",
           "search_photos", "write_file"]
