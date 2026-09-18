"""Card registration admin.

Deliberately tiny: OwnTone's UI on :3689 owns everything player-related.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import threading
from pathlib import Path

from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest

from .cards import Card, name_for_path, normalise_uid

log = logging.getLogger(__name__)


def _run_power(action: str) -> None:
    """Ask systemd to power off or reboot, in the background.

    Detached so the HTTP response is sent before the machine goes away -
    otherwise the browser reports a network error on a request that worked.
    """
    command = ["sudo", "-n", "/usr/bin/systemctl", action]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def create_app(config, controller, store, owntone=None, power=None) -> Flask:
    # Where sound will come out. Polled once a second by the page, so cached
    # briefly rather than asking OwnTone every time - the answer changes only
    # when someone picks different speakers.
    _outputs_cache: dict = {"at": 0.0, "value": None}
    _OUTPUTS_TTL_S = 3.0

    def selected_outputs():
        if owntone is None:
            return None
        now = time.monotonic()
        if _outputs_cache["value"] is not None and now - _outputs_cache["at"] < _OUTPUTS_TTL_S:
            return _outputs_cache["value"]
        try:
            names = [o["name"] for o in owntone.outputs() if o.get("selected")]
        except Exception:
            log.warning("Could not read outputs from OwnTone", exc_info=True)
            return _outputs_cache["value"]
        _outputs_cache["at"] = now
        _outputs_cache["value"] = names
        return names

    app = Flask(__name__)
    # add_card and delete_card are read-modify-write over one YAML file, and
    # Flask serves requests from several threads. Without this, two people
    # registering cards at the same moment silently lose one of them.
    store_lock = threading.Lock()

    def _album_dir(raw_path):
        """Resolve a posted album path inside the library, or explain why not.

        Returns (path, None) or (None, error message). A typo that saved
        happily would show up only as a card that plays nothing when tapped,
        with no explanation anywhere -- exactly the failure the box must not
        have.
        """
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None, "'path' must be a non-empty album folder path."
        root = Path(config.library_root)
        candidate = root / raw_path
        try:
            resolved = candidate.resolve()
            root_resolved = root.resolve()
        except OSError as exc:
            return None, f"Could not check album path {raw_path!r}: {exc}"
        if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
            return None, f"Album path {raw_path!r} is outside the music library."
        if not resolved.is_dir():
            return None, (f"No album folder {raw_path!r} under {root}. "
                          "Pick one from the list.")
        return resolved, None

    @app.get("/")
    def index():
        # Only the PORT is passed: config.owntone_url is how the *server*
        # reaches OwnTone (127.0.0.1), which as a link would point the
        # browser at the viewer's own machine. The page builds the href from
        # whatever hostname the user actually used to get here.
        return render_template("index.html",
                               owntone_port=urlparse(config.owntone_url).port or 3689)

    def library_state():
        """Whether the music is actually reachable, as (ok, explanation).

        The jukebox deliberately starts without it -- a NAS that fails to
        resolve should leave a page that explains itself, not a silent box --
        so the page has to be the thing that says so.

        A dead CIFS mount does not raise FileNotFoundError. It raises OSError
        ENODEV, "No such device", which is what /srv/music returned the night
        the NAS stopped resolving. Catching only the tidy exception would have
        taken down the very page meant to report the problem.
        """
        root = str(config.library_root)
        try:
            entries = os.listdir(root)
        except OSError as exc:
            return False, f"Music is not mounted at {root} ({exc.strerror})"
        if not entries:
            # An automount point with nothing under it is indistinguishable
            # from a mount that failed, because that is what it is.
            return False, f"Music is not mounted at {root} (nothing there)"
        return True, None

    @app.get("/api/status")
    def status():
        # Resolve the last scanned card here rather than in the controller:
        # the page needs to say whether the card in the user's hand is already
        # assigned, and showing a bare UID next to a stale dropdown selection
        # reads as if that album is what the card maps to.
        uid = controller.last_seen_uid
        card = store.get(uid) if uid else None
        library_ok, library_error = library_state()
        return jsonify(
            state=controller.state.value,
            now_playing=controller.now_playing,
            last_error=controller.last_error,
            last_seen_uid=uid,
            last_seen_known=card is not None,
            last_seen_name=card.name if card else None,
            last_seen_path=card.path if card else None,
            management_mode=controller.management_mode,
            outputs=selected_outputs(),
            library_ok=library_ok,
            library_error=library_error,
        )

    @app.put("/api/management")
    def management():
        payload = request.get_json(silent=True) or {}
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify(error="Expected {\"enabled\": true|false}."), 400
        controller.set_management_mode(enabled)
        return jsonify(management_mode=controller.management_mode)

    # Injected so tests never shell out, and so the command is in one place.
    run_power = power if power is not None else _run_power

    @app.post("/api/power")
    def power_control():
        """Shut the box down or reboot it.

        A Pi pulled from the wall mid-write is how SD cards die, and this is an
        appliance people will unplug. Note there is no authentication on this
        page - anyone on the LAN can call this. That is a real widening, judged
        acceptable only because anyone who can reach it could also pull the
        plug, which is the worse outcome this exists to prevent.
        """
        payload = request.get_json(silent=True) or {}
        action = payload.get("action")
        if action not in ("poweroff", "reboot"):
            return jsonify(error='Expected {"action": "poweroff"|"reboot"}.'), 400
        try:
            run_power(action)
        except Exception as exc:
            log.exception("Power command failed")
            return jsonify(error=f"Could not {action}: {exc}"), 500
        return jsonify(action=action)

    @app.post("/api/startover")
    def start_over():
        """Restart the loaded album from track 1.

        The one transport control this page carries, and only because the
        vinyl model took the other one away: lifting a card used to rewind the
        record and now it pauses it. Nothing to validate - the request has no
        parameters, and the only question is whether there is a record on the
        platter, which only the controller can answer.
        """
        try:
            now_playing = controller.start_over()
        except LookupError as exc:
            # Not an error in the box: there is simply nothing loaded yet.
            return jsonify(error=str(exc)), 409
        except Exception as exc:
            log.exception("Start over failed")
            return jsonify(error=f"Could not start the album over: {exc}"), 500
        return jsonify(now_playing=now_playing)

    @app.get("/api/artwork")
    def artwork():
        """Album art for a library-relative path, as an OwnTone-relative URL.

        Its own endpoint rather than a field on /api/status: status is polled
        once a second, and this costs a search on OwnTone. The page fetches it
        only when the scanned card changes.
        """
        path = (request.args.get("path") or "").strip()
        if not path:
            return jsonify(error="path is required"), 400
        if owntone is None:
            return jsonify(url=None)
        try:
            return jsonify(url=owntone.album_artwork_url(path))
        except Exception:
            log.warning("Artwork lookup failed for %s", path, exc_info=True)
            return jsonify(url=None)

    @app.get("/api/albums")
    def albums():
        root = Path(config.library_root)
        found = sorted(
            str(path.relative_to(root))
            for path in root.glob("*/*")
            if path.is_dir()
        )
        # Which albums already have a card. With a library far larger than the
        # number of cards, "what still needs one" is the question being asked
        # while registering - and assigning a second card to an album you have
        # already done is otherwise invisible until you tap it.
        assigned = {c.path: c.name for c in store.load().values()}
        return jsonify(
            albums=[
                {"path": path,
                 "assigned": path in assigned,
                 "card_name": assigned.get(path)}
                for path in found
            ],
            total=len(found),
            unassigned=sum(1 for path in found if path not in assigned),
        )

    @app.get("/api/cards")
    def list_cards():
        return jsonify(cards=[
            {"uid": c.uid, "name": c.name, "path": c.path}
            for c in store.load().values()
        ])

    @app.post("/api/cards")
    def add_card():
        try:
            payload = request.get_json(force=True)
        except BadRequest:
            payload = None
        if not isinstance(payload, dict):
            return jsonify(error="Expected a JSON object with 'uid' and "
                                 "'path' (optional 'name')."), 400

        missing = [k for k in ("uid", "path") if k not in payload]
        if missing:
            return jsonify(error=f"Missing required field(s): "
                                 f"{', '.join(missing)}."), 400

        raw_uid = payload["uid"]
        uid = normalise_uid(str(raw_uid)) if isinstance(raw_uid, (str, int)) else ""
        if not uid:
            return jsonify(error=f"{raw_uid!r} is not a usable card UID."), 400

        _, error = _album_dir(payload["path"])
        if error:
            return jsonify(error=error), 400

        # The path already names the album, so a name is optional. Supplying
        # one is for friendly labels ("Bedtime Songs"), not routine entry.
        name = payload.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else None

        card = Card(uid=uid,
                    name=name or name_for_path(payload["path"]),
                    path=payload["path"])
        try:
            with store_lock:
                cards = store.load()
                cards[uid] = card
                store.save(cards)
        except OSError as exc:
            log.exception("Could not save card registry")
            return jsonify(error=f"Could not write the card registry: {exc}"), 500
        return jsonify(uid=uid), 201

    @app.delete("/api/cards/<uid>")
    def delete_card(uid: str):
        try:
            with store_lock:
                cards = store.load()
                cards.pop(normalise_uid(uid), None)
                store.save(cards)
        except OSError as exc:
            log.exception("Could not save card registry")
            return jsonify(error=f"Could not write the card registry: {exc}"), 500
        return "", 204

    return app
