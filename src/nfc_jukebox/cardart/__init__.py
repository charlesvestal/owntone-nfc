"""Collecting and laying out the artwork that goes on the cards.

Lives in the package rather than in tools/ because the admin page drives it:
collecting artwork, reviewing what came back, pinning the ones a search got
wrong and printing the sheets are all part of making a card, and making cards
is what the admin page is for.

The scripts under tools/cardart/ are thin command-line wrappers over this.
"""
from .artlib import (CONFIDENT, MATCH_FLOOR, flac_picture, image_size,
                     jpeg_size, png_size, rank_candidates, search_terms, slug,
                     title_match)
from .collect import DEFAULT_MIN_PX, collect_album, load_manifest, save_manifest
from .review import classify
from .sheets import CARD_MM, PAGE_H_MM, PAGE_W_MM, build_sheets

__all__ = [
    "CONFIDENT", "MATCH_FLOOR", "DEFAULT_MIN_PX", "CARD_MM",
    "PAGE_W_MM", "PAGE_H_MM",
    "flac_picture", "image_size", "jpeg_size", "png_size", "rank_candidates",
    "search_terms", "slug", "title_match",
    "collect_album", "load_manifest", "save_manifest", "classify",
    "build_sheets",
]
