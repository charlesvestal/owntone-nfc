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

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import BadRequest

from .cardart import collect as cardart_collect
from .cards import Card, duplicate_paths, name_for_path, normalise_uid

log = logging.getLogger(__name__)


def _run_power(action: str) -> None:
    """Ask systemd to power off or reboot, in the background.

    Detached so the HTTP response is sent before the machine goes away -
    otherwise the browser reports a network error on a request that worked.
    """
    command = ["sudo", "-n", "/usr/bin/systemctl", action]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def create_app(config, controller, store, owntone=None, power=None,
               collector=None) -> Flask:
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

    # Both /api/status and /api/cards are polled once a second by the page,
    # and both land here, so this ran twice a second for as long as a tab
    # stayed open -- an os.listdir on the CIFS mount, ~6 SMB round trips a
    # call. Cached briefly rather than skipped: a NAS that drops still has to
    # surface on the page, and this TTL is the worst-case delay before it does.
    _mount_cache: dict = {"at": 0.0, "value": None}
    _MOUNT_TTL_S = 15.0

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
        now = time.monotonic()
        if (_mount_cache["value"] is not None
                and now - _mount_cache["at"] < _MOUNT_TTL_S):
            return _mount_cache["value"]
        root = str(config.library_root)
        try:
            entries = os.listdir(root)
        except OSError as exc:
            result = (False,
                      f"Music is not mounted at {root} ({exc.strerror})")
        else:
            if not entries:
                # An automount point with nothing under it is
                # indistinguishable from a mount that failed, because that is
                # what it is.
                result = (False,
                          f"Music is not mounted at {root} (nothing there)")
            else:
                result = (True, None)
        _mount_cache["at"] = now
        _mount_cache["value"] = result
        return result

    # Cached like the outputs, and for a sharper reason: during a rescan
    # OwnTone's /api/library was measured taking 13 seconds to answer on the
    # box, and this sits on the status line the page polls once a second.
    _library_cache: dict = {"at": 0.0, "value": None}
    _LIBRARY_TTL_S = 3.0

    def _library_counts():
        """What OwnTone knows about, or None if it has never said.

        Never allowed to raise, and never allowed to blank out a good answer:
        this is the status line, and it has to keep working -- and keep
        reading sensibly -- while OwnTone is busy scanning.
        """
        if owntone is None:
            return None
        now = time.monotonic()
        if (_library_cache["value"] is not None
                and now - _library_cache["at"] < _LIBRARY_TTL_S):
            return _library_cache["value"]
        try:
            body = owntone.library_status()
        except Exception:                                   # noqa: BLE001
            log.debug("Could not read the library status", exc_info=True)
            return _library_cache["value"]
        _library_cache["at"] = now
        _library_cache["value"] = {
            "songs": body.get("songs"), "albums": body.get("albums"),
            "updating": bool(body.get("updating"))}
        return _library_cache["value"]

    @app.after_request
    def _never_cache_the_page(response):
        """The admin page must always be the deployed one.

        Artwork is cached for a year because its URL carries the file's mtime;
        the page has no such version, so a cached copy means a browser running
        old JavaScript against a new API. That surfaces as the box behaving
        oddly rather than as an obviously stale page, which is worse.
        """
        if request.path == "/":
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

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
            library=_library_counts(),
            outputs=selected_outputs(),
            library_ok=library_ok,
            library_error=library_error,
        )

    @app.post("/api/library/rescan")
    def library_rescan():
        """Make OwnTone notice albums added since it last looked.

        The nightly cron only fires if the box happens to be powered on at
        04:30, which a living-room jukebox often is not, so this is the
        button that actually gets used.
        """
        if owntone is None:
            return jsonify(error="No OwnTone client."), 503
        try:
            owntone.update_library()
        except Exception as exc:                            # noqa: BLE001
            log.exception("Asking OwnTone to rescan failed")
            return jsonify(error=f"Could not reach OwnTone: {exc}"), 502
        return jsonify(started=True)

    @app.post("/api/scan")
    def scan():
        """A card read by a reader that is not the jukebox's own.

        Lets registration happen at a desk with a USB reader instead of
        kneeling next to the box. Identification only -- it cannot start,
        stop or switch a record -- so the page behaves exactly as if the card
        had been tapped on the jukebox, and the room is undisturbed.
        """
        payload = request.get_json(silent=True) or {}
        uid = normalise_uid(payload.get("uid") or "")
        if not uid:
            return jsonify(error="Which card?"), 400
        controller.note_scan(uid)
        return jsonify(uid=uid, known=store.get(uid) is not None)

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


    # The library is a network mount: 185 albums over CIFS measured 7.3s to
    # walk, and one cold walk measured ~650 SMB round trips on the box.
    # /api/cards and /api/albums are polled once a second by the page, so this
    # must not be repeated per request. Albums appear when someone copies
    # files to a NAS, which is not a once-a-second event -- at the old 15s
    # this was still four full walks a minute for a tab nobody was looking at.
    # The Register tab reloads the list after saving a card, so the delay is
    # not felt where it would matter.
    _albums_cache: dict = {"at": 0.0, "value": None}
    _ALBUMS_TTL_S = 300.0

    def _album_rows():
        """Every album in the library, and whether it already has a card.

        Shared with the artwork studio, which collects art for exactly the
        albums that do not have one yet.

        With a library far larger than the number of cards, "what still needs
        one" is the question being asked while registering - and assigning a
        second card to an album you have already done is otherwise invisible
        until you tap it.
        """
        now = time.monotonic()
        if (_albums_cache["value"] is not None
                and now - _albums_cache["at"] < _ALBUMS_TTL_S):
            found = _albums_cache["value"]
        else:
            root = Path(config.library_root)
            found = sorted(
                str(path.relative_to(root))
                for path in root.glob("*/*")
                if path.is_dir()
            )
            _albums_cache["at"] = now
            _albums_cache["value"] = found
        # Cards are read fresh: registering one must show up at once, and it
        # is a local file rather than the network mount.
        assigned = {c.path: c.name for c in store.load().values()}
        return [{"path": path,
                 "assigned": path in assigned,
                 "card_name": assigned.get(path)} for path in found]

    def _known_albums():
        """Album paths that exist right now, or None if we cannot tell.

        None is not an empty set. An unmounted library lists nothing, which is
        indistinguishable from every album having been deleted at once -- and
        acting on that would prune the whole manifest and flag every card as
        broken the first time the NAS drops.
        """
        ok, _ = library_state()
        if not ok:
            return None
        return {row["path"] for row in _album_rows()}

    # --- artwork studio ---------------------------------------------------
    #
    # Collecting artwork, judging what came back, pinning the ones a search got
    # wrong and printing the sheets are all part of making a card, and making
    # cards is what this page is for. It ran as a pile of scripts on a laptop
    # first, which meant the one machine that has the library and the network
    # was not the machine doing the work.
    #
    # `collector` is injected so tests never reach the internet: collecting one
    # album talks to three services and takes seconds, and a whole library
    # takes minutes.

    _collect_lock = threading.Lock()
    _collect_state: dict = {"running": False, "done": 0, "total": 0, "album": None}

    def _artwork_dir() -> str:
        return str(config.artwork_dir)

    def _collect_one(album_path, out_dir, library_root, overrides):
        if collector is not None:
            return collector(album_path, out_dir, library_root, overrides)
        from .cardart import collect_album
        return collect_album(album_path, out_dir, library_root, overrides)

    def _run_collection(targets):
        """Walk the albums, saving after each one.

        Saving as it goes rather than at the end: a run takes minutes, and a
        network wobble half way through should not throw away the half that
        worked.
        """
        out_dir = _artwork_dir()
        try:
            overrides = cardart_collect.load_overrides(out_dir)
            manifest = cardart_collect.load_manifest(out_dir)
            for album_path in targets:
                _collect_state["album"] = album_path
                try:
                    entry = _collect_one(album_path, out_dir,
                                         str(config.library_root), overrides)
                except Exception:                       # noqa: BLE001
                    # One album that cannot be fetched is not a reason to
                    # abandon the rest, or to leave the page believing a run is
                    # still going.
                    log.exception("Collecting artwork for %s failed", album_path)
                    entry = {"status": "missing"}
                manifest[album_path] = entry
                cardart_collect.save_manifest(out_dir, manifest)
                _collect_state["done"] += 1
        finally:
            _collect_state["running"] = False
            _collect_state["album"] = None

    def _file_mtime(out_dir, name):
        if not name:
            return None
        try:
            return os.path.getmtime(os.path.join(out_dir, name))
        except OSError:
            return None

    @app.get("/api/artwork/manifest")
    def artwork_manifest():
        from .cardart import classify
        out_dir = _artwork_dir()
        manifest = cardart_collect.load_manifest(out_dir)
        overrides = cardart_collect.load_overrides(out_dir)

        # Drop albums whose folder has gone, so a deleted folder does not
        # leave a ghost tile in the grid for ever. The override survives: it
        # is a human judgement, and the folder may come back renamed.
        # Never while a collection is in flight: that thread saves the
        # manifest after every album, and this read-modify-write would drop
        # whatever it wrote in between. The stale rows keep until it finishes.
        known = None if _collect_state["running"] else _known_albums()
        if known is not None:
            stale = [path for path in manifest if path not in known]
            for path in stale:
                name = manifest.pop(path).get("file")
                if name:
                    try:
                        os.unlink(os.path.join(out_dir, name))
                    except OSError:
                        pass          # already gone, or never written
            if stale:
                cardart_collect.save_manifest(out_dir, manifest)

        # Every album awaiting a card, not just the ones already collected.
        # Built from the manifest alone, the grid could not show the size of
        # the job or what a run was working on -- albums simply materialised
        # one at a time, and a fresh box showed an empty tab.
        #
        # `known` is None when the library cannot be read; fall back to the
        # manifest then rather than blanking a grid that has real results.
        # "all" is the reprint view: every album, including ones already done.
        # Normally the studio is the print queue and shows only what still
        # needs a card.
        want_all = request.args.get("all") in ("1", "true", "yes")
        if known is None:
            waiting = sorted(manifest)
            assigned_paths = set()
        else:
            rows = _album_rows()
            assigned_paths = {r["path"] for r in rows if r["assigned"]}
            waiting = sorted(r["path"] for r in rows
                             if want_all or not r["assigned"])
        albums = []
        for album_path in waiting:
            entry = manifest.get(album_path)
            if entry is None:
                # Not looked for yet, which is not the same as looked for and
                # not found: "bad" would put a red edge on an album nobody has
                # got to.
                albums.append({
                    "album": album_path, "status": None, "file": None,
                    "width": None, "height": None, "source": None,
                    "score": None, "verdict": "pending",
                    "why": "not collected yet",
                    "override": overrides.get(album_path), "mtime": None,
                    "assigned": album_path in assigned_paths,
                })
                continue
            verdict, why = classify(entry, 0)
            albums.append({
                "album": album_path,
                "status": entry.get("status"),
                "file": entry.get("file"),
                "width": entry.get("width"),
                "height": entry.get("height"),
                "source": entry.get("source"),
                "score": entry.get("score"),
                "verdict": verdict,
                "why": why,
                "override": overrides.get(album_path),
                "assigned": album_path in assigned_paths,
                # Versions the image URL. Artwork is replaced in place under a
                # stable filename, so without this the browser keeps whatever
                # it cached and a re-pinned cover never appears.
                "mtime": _file_mtime(out_dir, entry.get("file")),
            })
        return jsonify(albums=albums, running=_collect_state["running"],
                       done=_collect_state["done"], total=_collect_state["total"],
                       current=_collect_state["album"])

    @app.post("/api/artwork/collect")
    def artwork_collect():
        payload = request.get_json(silent=True) or {}
        with _collect_lock:
            if _collect_state["running"]:
                return jsonify(error="A collection is already running."), 409
            targets = [a["path"] for a in _album_rows()
                       if payload.get("all") or not a["assigned"]]
            if not payload.get("refetch"):
                manifest = cardart_collect.load_manifest(_artwork_dir())
                overrides = cardart_collect.load_overrides(_artwork_dir())
                targets = [t for t in targets
                           if manifest.get(t, {}).get("status") != "ok"
                           or bool(overrides.get(t)) != bool(manifest.get(t, {}).get("override"))]
            if not targets:
                # Nothing to do. Starting a run anyway briefly marks the box
                # busy and 409s the next request, which is how a "nothing to
                # collect" click blocked the collect that followed it.
                return jsonify(started=False, total=0)
            _collect_state.update(running=True, done=0, total=len(targets),
                                  album=None)
        threading.Thread(target=_run_collection, args=(targets,),
                         daemon=True).start()
        return jsonify(started=True, total=len(targets))

    @app.put("/api/artwork/override")
    def artwork_override():
        """Pin an album a search got wrong, or mark one as never needing a card.

        An empty body for an album clears its override, so the page has one
        control rather than an add and a separate remove.
        """
        payload = request.get_json(silent=True) or {}
        album = payload.get("album")
        if not album:
            return jsonify(error="Which album?"), 400
        out_dir = _artwork_dir()
        overrides = cardart_collect.load_overrides(out_dir)
        entry = {}
        for key, field in (("artist", "artist"), ("album_name", "album"),
                           ("url", "url"), ("skip", "skip")):
            if payload.get(key):
                entry[field] = payload[key]
        if entry:
            overrides[album] = entry
        else:
            overrides.pop(album, None)
        cardart_collect.save_overrides(out_dir, overrides)

        # Apply it now rather than recording the intent and waiting for a
        # Collect run. Saving without collecting leaves the page showing the
        # old art, which reads as the override having failed.
        with _collect_lock:
            if _collect_state["running"]:
                # Two writers to manifest.json would lose an update -- the bulk
                # run saves after every album. Leave it to that run instead.
                return jsonify(album=album, override=overrides.get(album),
                               collected=False,
                               error="A collection is running; it will pick "
                                     "this up.")
        try:
            collected = _collect_one(album, out_dir,
                                     str(config.library_root), overrides)
        except Exception:                               # noqa: BLE001
            log.exception("Collecting artwork for %s failed", album)
            collected = {"status": "missing"}
        manifest = cardart_collect.load_manifest(out_dir)
        manifest[album] = collected
        cardart_collect.save_manifest(out_dir, manifest)

        # A dead link is an ordinary outcome, not an exception. Report it and
        # keep the override, so the URL can be edited rather than retyped.
        error = None
        if collected.get("status") not in ("ok", "skipped"):
            error = "Saved, but nothing could be fetched for that."
        return jsonify(album=album, override=overrides.get(album),
                       entry=collected, collected=True, error=error)

    @app.post("/api/artwork/sheets")
    def artwork_sheets():
        from .cardart import build_sheets
        payload = request.get_json(silent=True) or {}
        out_dir = _artwork_dir()
        target = os.path.join(out_dir, "cards-to-print.pdf")
        # Print exactly what the grid is showing. Left to itself build_sheets
        # takes the whole manifest, so unticking "include albums that already
        # have cards" would still print them.
        known = _known_albums()
        only = None
        if not payload.get("all") and known is not None:
            only = {row["path"] for row in _album_rows() if not row["assigned"]}
        try:
            result = build_sheets(out_dir, target,
                                  int(payload.get("min_px") or 0),
                                  bool(payload.get("include_suspect")),
                                  only=only)
        except FileNotFoundError:
            return jsonify(error="No artwork collected yet."), 409
        except Exception as exc:                        # noqa: BLE001
            log.exception("Building the print sheets failed")
            return jsonify(error=f"Could not build the sheets: {exc}"), 500
        if not result["cards"]:
            return jsonify(error="Nothing to print yet."), 409
        return jsonify(**result)

    @app.get("/artwork-file/<path:name>")
    def artwork_file(name):
        """Serve a collected image, or the print sheets.

        The name arrives in a URL, so it is attacker-controlled: resolve it and
        check it really is inside the artwork directory rather than trusting
        that a ".." was caught by the router.
        """
        root = Path(_artwork_dir()).resolve()
        try:
            target = (root / name).resolve()
            target.relative_to(root)
        except (ValueError, OSError):
            return jsonify(error="No."), 400
        if not target.is_file():
            return jsonify(error="Not found."), 404
        response = send_file(target)
        # The collected art is full resolution -- 3000px squares, ~60MB for a
        # library's worth -- because that is what has to go to the printer.
        # The review grid shows the same files as thumbnails, so cache them:
        # the first look costs what it costs, and every look after is free.
        # A file only changes when it is re-collected, and then its whole
        # entry changes with it.
        # Cached forever, because the page versions the URL with the file's
        # mtime: a given URL always means the same bytes. Replaced artwork
        # arrives under a new URL and is fetched exactly once, so months
        # between prints cost no requests at all. `immutable` matters as well
        # as the max-age -- without it a plain reload revalidates everything.
        response.headers["Cache-Control"] = ("public, max-age=31536000, "
                                             "immutable")
        return response

    @app.get("/api/albums")
    def albums():
        rows = _album_rows()
        return jsonify(
            albums=rows,
            total=len(rows),
            unassigned=sum(1 for row in rows if not row["assigned"]),
        )

    @app.get("/api/cards")
    def list_cards():
        # Flagged here rather than in the page: the store is already loaded,
        # so it costs one pass, and the page stays a renderer.
        cards = store.load()
        duplicated = duplicate_paths(cards)
        # None means the library is unreachable, so nothing can be judged
        # missing -- see _known_albums.
        known = _known_albums()
        return jsonify(cards=[
            {"uid": c.uid, "name": c.name, "path": c.path,
             "duplicate": c.path in duplicated,
             "orphan": known is not None and c.path not in known}
            for c in cards.values()
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

    # Seam for tests that need to act as though a bulk run is in flight.
    app.extensions["collect_state"] = _collect_state
    return app
