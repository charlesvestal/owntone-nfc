"""Card registration admin.

Deliberately tiny: OwnTone's UI on :3689 owns everything player-related.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest

from .cards import Card, name_for_path, normalise_uid

log = logging.getLogger(__name__)


def create_app(config, controller, store) -> Flask:
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

    @app.get("/api/status")
    def status():
        # Resolve the last scanned card here rather than in the controller:
        # the page needs to say whether the card in the user's hand is already
        # assigned, and showing a bare UID next to a stale dropdown selection
        # reads as if that album is what the card maps to.
        uid = controller.last_seen_uid
        card = store.get(uid) if uid else None
        return jsonify(
            state=controller.state.value,
            now_playing=controller.now_playing,
            last_error=controller.last_error,
            last_seen_uid=uid,
            last_seen_known=card is not None,
            last_seen_name=card.name if card else None,
            last_seen_path=card.path if card else None,
        )

    @app.get("/api/albums")
    def albums():
        root = Path(config.library_root)
        found = sorted(
            str(path.relative_to(root))
            for path in root.glob("*/*")
            if path.is_dir()
        )
        return jsonify(albums=found)

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
