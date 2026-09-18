"""Thin client for the OwnTone JSON API.

Knows nothing about cards or playback policy — it just maps method calls to
OwnTone endpoints.
"""
from __future__ import annotations


import httpx

# OwnTone runs on localhost, so a call taking seconds means it is wedged, not
# busy. This bounds how long the controller's lock can be held during a
# release: a slow release blocks the reader thread, so a generous timeout
# would park card detection for as long as it lasts.
DEFAULT_TIMEOUT_S = 3.0

# Tags and keywords checked against OwnTone's smart-playlist lexer
# (src/parsers/smartpl_lexer.l): `path` is the string tag, `disc` and `track`
# are the integer tags, and `includes` / `order by` / `asc` are its keywords.
# The JSON-API spellings `disc_number`/`track_number` are NOT expression
# grammar and do not parse.
#
# NOT YET VERIFIED AGAINST A LIVE SERVER. Nothing in this repo has ever talked
# to a real OwnTone; this is read from its source, which is a much better guess
# than the previous one but still a guess about runtime behaviour. Spike 1 is
# what turns this comment into a fact, and this is the single place to change
# if it finds otherwise.
# Ordering is deliberately absent: it is done client-side in play_album,
# because OwnTone accepts only one sort field and a multi-disc album needs two.
ALBUM_EXPRESSION = 'path includes "{path}"'

# Upper bound on tracks fetched for one album. Generous - the longest boxed set
# here is 22 - but bounded so a mistyped path cannot pull the whole library.
MAX_ALBUM_TRACKS = 500

# `type` is the `.name` field of OwnTone's `struct output_definition`
# (src/outputs/*.c): "AirPlay 2" (airplay.c), "AirPlay 1" (raop.c),
# "ALSA" (alsa.c), "Pulseaudio" (pulse.c), "Chromecast" (cast.c),
# "streaming" (streaming.c), plus fifo and dummy.
_AIRPLAY_PREFIX = "airplay"
_LOCAL_TYPES = frozenset({"alsa", "pulseaudio"})


def is_airplay(output: dict) -> bool:
    """True for both AirPlay 1 and AirPlay 2 receivers.

    Prefix match because the version number is part of the type string, and
    case-folded because the exact casing is not something we can pin down
    without a live server.
    """
    return str(output.get("type", "")).casefold().startswith(_AIRPLAY_PREFIX)


def is_local(output: dict) -> bool:
    """True only for a soundcard physically attached to this Pi.

    An allow-list, deliberately: "everything that is not AirPlay" would make a
    release select the neighbour's Chromecast and OwnTone's HTTP streaming
    endpoint, neither of which the user asked to hear.
    """
    return str(output.get("type", "")).casefold() in _LOCAL_TYPES


def _escape(value: str) -> str:
    """Escape a value for interpolation into a double-quoted query string.

    An album like `Various/12" Singles` would otherwise close the quoted
    string early and leave OwnTone parsing garbage. Backslash first, so the
    escapes we add are not themselves re-escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _anchor(path: str) -> str:
    """Anchor an album path at a directory boundary.

    `includes` is an unanchored substring match, so a bare
    `Fleetwood Mac/Rumours` also matches the sibling `Rumours (Deluxe)` and
    `Rumours - 2013 Remaster` folders that a real FLAC library is full of.
    Both albums then land in one queue, interleaved by disc/track: track 1
    twice, track 2 twice. A trailing separator makes the prefix stop at the
    album directory.
    """
    return path.rstrip("/") + "/"


class OwnTone:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_S)

    # --- internal ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, f"{self._base}{path}", **kwargs)
        response.raise_for_status()
        return response

    def _outputs(self) -> list[dict]:
        return self._request("GET", "/api/outputs").json()["outputs"]

    # --- outputs ------------------------------------------------------------

    def outputs(self) -> list[dict]:
        """Every output OwnTone knows about, as returned by the API."""
        return self._outputs()

    def selected_output_ids(self) -> list[str]:
        return [o["id"] for o in self._outputs() if o.get("selected")]

    def airplay_output_ids(self) -> list[str]:
        return [o["id"] for o in self._outputs() if is_airplay(o)]

    def local_output_ids(self) -> list[str]:
        return [o["id"] for o in self._outputs() if is_local(o)]

    def all_output_ids(self) -> list[str]:
        """Every output OwnTone knows about, of any type.

        Used to decide whether a saved output still exists. An output that is
        neither local nor AirPlay was still chosen by somebody, so a snapshot
        naming it must be restorable.
        """
        return [o["id"] for o in self._outputs()]

    def set_outputs(self, output_ids: list[str]) -> None:
        """Select exactly these outputs; OwnTone deselects all others."""
        self._request("PUT", "/api/outputs/set", json={"outputs": output_ids})

    # --- playback -------------------------------------------------------------

    def shuffle(self, enabled: bool) -> None:
        self._request(
            "PUT", "/api/player/shuffle",
            params={"state": "true" if enabled else "false"},
        )

    def repeat(self, mode: str) -> None:
        """`mode` is OwnTone's own vocabulary: off, all or single."""
        self._request("PUT", "/api/player/repeat", params={"state": mode})

    def set_vinyl_playback_mode(self) -> None:
        """Shuffle and repeat off — a record plays its sides in order, once.

        OwnTone persists both across restarts and its web UI, which this design
        deliberately hands to the user, puts a shuffle toggle one click away.
        Left on, every card would start on a random track for good.
        """
        self.shuffle(False)
        self.repeat("off")

    def play_album(self, relative_path: str) -> None:
        """Clear the queue and play the album at this library-relative path.

        Tracks are fetched, sorted here by (disc, track), and queued by explicit
        URI rather than asking OwnTone to sort. That costs one extra request -
        ~3ms on localhost - and is the only way to order a multi-disc album
        correctly, because OwnTone's expression grammar accepts exactly ONE
        sort field: `order by disc asc, track asc` is a syntax error.

        A real example this fixes: M83's "Hurry Up, We're Dreaming" is two
        discs flattened into one folder, so both discs have a track 1. Sorting
        by track interleaves them; sorting by path interleaves them too, since
        the filenames collide the same way. The tags are correct, so sorting on
        (disc, track) is the only thing that works - and it also picks up
        tracks stranded in a sibling folder.
        """
        tracks = self._request(
            "GET", "/api/search",
            params={"type": "tracks",
                    "expression": ALBUM_EXPRESSION.format(
                        path=_escape(_anchor(relative_path))),
                    "limit": MAX_ALBUM_TRACKS},
        ).json().get("tracks", {}).get("items", [])

        if not tracks:
            # Nothing matched. Let the caller's error handling see a normal
            # failure rather than silently queueing an empty album.
            raise LookupError(f"no tracks found for {relative_path!r}")

        # Disc numbering starts at 1, so 0 or missing means "unset" and must
        # sort WITH disc 1, not before it. Real case: two tracks moved into an
        # album kept disc=0 from their old tags while the rest were disc=1 -
        # treating 0 as its own disc put tracks 11 and 12 at the front.
        tracks.sort(key=lambda t: (t.get("disc_number") or 1,
                                   t.get("track_number") or 0,
                                   t.get("path") or ""))
        uris = ",".join(t["uri"] for t in tracks if t.get("uri"))

        self._request(
            "POST", "/api/queue/items/add",
            params={"uris": uris, "clear": "true", "playback": "start"},
        )

    def play(self) -> None:
        self._request("PUT", "/api/player/play")

    def pause(self) -> None:
        self._request("PUT", "/api/player/pause")

    def stop(self) -> None:
        self._request("PUT", "/api/player/stop")

    def clear_queue(self) -> None:
        self._request("PUT", "/api/queue/clear")

    def album_artwork_url(self, relative_path: str) -> str | None:
        """OwnTone-relative artwork URL for the album at this path, if any.

        Returned relative (e.g. `/artwork/group/7`) because the caller - a
        browser - must resolve it against the host it reached OwnTone on, not
        against the loopback address this client uses.
        """
        response = self._request(
            "GET", "/api/search",
            params={"type": "albums",
                    "expression": ALBUM_EXPRESSION.format(
                        path=_escape(_anchor(relative_path))),
                    "limit": 1},
        ).json()
        items = response.get("albums", {}).get("items", [])
        if not items:
            return None
        url = items[0].get("artwork_url")
        if not url:
            return None
        # OwnTone returns these as "./artwork/group/7"; normalise to a rooted
        # path so the browser cannot resolve it against the current page.
        return "/" + url.lstrip("./")

    def player_state(self) -> dict:
        return self._request("GET", "/api/player").json()
