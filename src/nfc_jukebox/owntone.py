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
ALBUM_EXPRESSION = 'path includes "{path}" order by path asc'

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
        """Clear the queue and play the album at this library-relative path."""
        self._request(
            "POST",
            "/api/queue/items/add",
            params={
                "expression": ALBUM_EXPRESSION.format(
                    path=_escape(_anchor(relative_path))
                ),
                "clear": "true",
                "playback": "start",
            },
        )

    def play(self) -> None:
        self._request("PUT", "/api/player/play")

    def pause(self) -> None:
        self._request("PUT", "/api/player/pause")

    def stop(self) -> None:
        self._request("PUT", "/api/player/stop")

    def clear_queue(self) -> None:
        self._request("PUT", "/api/queue/clear")

    def player_state(self) -> dict:
        return self._request("GET", "/api/player").json()
