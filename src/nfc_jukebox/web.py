"""Card registration admin.

Deliberately tiny: OwnTone's UI on :3689 owns everything player-related.
"""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from .cards import Card, normalise_uid


def create_app(config, controller, store) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html", owntone_url=config.owntone_url)

    @app.get("/api/status")
    def status():
        return jsonify(
            state=controller.state.value,
            now_playing=controller.now_playing,
            last_error=controller.last_error,
            last_seen_uid=controller.last_seen_uid,
        )

    @app.get("/api/albums")
    def albums():
        root = config.library_root
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
        payload = request.get_json(force=True)
        uid = normalise_uid(payload["uid"])
        cards = store.load()
        cards[uid] = Card(uid=uid, name=payload["name"], path=payload["path"])
        store.save(cards)
        return jsonify(uid=uid), 201

    @app.delete("/api/cards/<uid>")
    def delete_card(uid: str):
        cards = store.load()
        cards.pop(normalise_uid(uid), None)
        store.save(cards)
        return "", 204

    return app
