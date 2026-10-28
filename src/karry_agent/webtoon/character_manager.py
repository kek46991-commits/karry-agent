"""Character consistency: one fixed character sheet is prepended to every panel prompt of a story."""
from __future__ import annotations

import hashlib
from typing import Any

STYLE = "clean Japanese webtoon style, full color, crisp line art, soft cel shading, same art style in every panel"
RULES = ("Vertical 9:16 composition. Draw NO text, letters, speech bubbles, captions or sound effects. "
         "Keep the top 20% and bottom 15% of the frame visually simple (sky, wall, floor) for later text overlays. "
         "Follow the character sheet exactly; never add or remove glasses or change hair or clothing.")


class CharacterManager:
    def __init__(self, characters: list[dict[str, Any]], style: str = STYLE) -> None:
        if not characters:
            raise ValueError("characters required")
        self.characters = {c["name"]: c for c in characters}
        self.style = style
        self.sheet = self._sheet()
        self.fingerprint = hashlib.sha256(self.sheet.encode()).hexdigest()

    @staticmethod
    def describe(c: dict[str, Any]) -> str:
        accessories = ", ".join(c.get("accessories") or []) or "none"
        return (f"- {c['name']}: face: {c.get('face') or 'consistent'}; hair: {c.get('hair')}; clothing: {c.get('clothing')}; "
                f"accessories: {accessories}; glasses: {'ON (always wearing glasses)' if c.get('glasses') else 'OFF (never wears glasses)'}")

    def _sheet(self) -> str:
        return "CHARACTER SHEET (fixed for every panel of this story, do not change):\n" + \
            "\n".join(self.describe(c) for c in self.characters.values())

    def panel_prompt(self, panel: dict[str, Any]) -> str:
        present = [n for n in panel.get("characters_present") or [] if n in self.characters]
        return (f"{self.sheet}\nSTYLE: {self.style}\nRULES: {RULES}\n"
                f"CHARACTERS IN THIS PANEL: {', '.join(present) or 'none (scenery)'}\n"
                f"SCENE: {panel.get('visual_description', '')}")

    def is_consistent(self, prompts: list[str]) -> bool:
        return all(p.startswith(self.sheet) for p in prompts)
