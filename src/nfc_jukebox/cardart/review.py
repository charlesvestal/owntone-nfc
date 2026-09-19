"""How much to trust one piece of collected artwork."""
from __future__ import annotations

from .artlib import MATCH_FLOOR


def classify(entry: dict, min_px: int) -> tuple[str, str]:
    """(css class, human reason) for one manifest entry.

    The fit score is the fetcher's, not recomputed here: the two drifting
    apart would mean the page disagreed with the thing that chose the image.
    """
    status = entry.get("status")
    if status == "skipped":
        return "skipped", entry.get("reason", "skipped")
    if status != "ok":
        return "bad", "nothing found"
    if entry["width"] < min_px:
        return "bad", f"only {entry['width']}px"
    if entry.get("override"):
        return "local", "pinned by hand"
    if entry.get("source", "").startswith(("file:", "embedded:")):
        return "local", "from the library"
    if entry.get("score", 1.0) < MATCH_FLOOR:
        return "suspect", "matched title looks wrong"
    return "fetched", "from a search"
