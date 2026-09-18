"""Thin client for the OwnTone JSON API.

Knows nothing about cards or playback policy — it just maps method calls to
OwnTone endpoints.
"""
from __future__ import annotations


import httpx

# Verified in Spike 1 (Task 5). If that spike found a different working syntax,
# this is the single place to change it.
ALBUM_EXPRESSION = 'path includes "{path}" order by disc_number asc, track_number asc'


class OwnTone:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10.0)

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
        return [o["id"] for o in self._outputs() if o.get("type") == "airplay"]

    def local_output_ids(self) -> list[str]:
        return [o["id"] for o in self._outputs() if o.get("type") != "airplay"]

    def set_outputs(self, output_ids: list[str]) -> None:
        """Select exactly these outputs; OwnTone deselects all others."""
        self._request("PUT", "/api/outputs/set", json={"outputs": output_ids})

    # --- playback -------------------------------------------------------------

    def play_album(self, relative_path: str) -> None:
        """Clear the queue and play the album at this library-relative path."""
        self._request(
            "POST",
            "/api/queue/items/add",
            params={
                "expression": ALBUM_EXPRESSION.format(path=relative_path),
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
