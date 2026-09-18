# src/nfc_jukebox/__main__.py
"""Wire reader, controller and web admin together."""
from __future__ import annotations

import logging
import threading
import time

from .cards import CardStore
from .config import Config
from .controller import Controller
from .outputs import OutputSnapshot
from .owntone import OwnTone
from .reader import Pn532Reader
from .web import create_app

TICK_INTERVAL = 1.0


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("nfc_jukebox")

    config = Config.load()
    store = CardStore(config.cards_file)
    controller = Controller(
        owntone=OwnTone(config.owntone_url),
        cards=store,
        snapshot=OutputSnapshot(config.outputs_file),
        config=config,
    )

    reader = Pn532Reader(config.reader_device)
    reader.on_present = controller.on_card_present
    reader.on_removed = controller.on_card_removed

    app = create_app(config, controller, store)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config.web_port,
                               threaded=True, use_reloader=False),
        daemon=True,
    ).start()

    def tick_forever() -> None:
        while True:
            try:
                controller.tick()
            except Exception:
                log.exception("Tick failed")
            time.sleep(TICK_INTERVAL)

    threading.Thread(target=tick_forever, daemon=True).start()

    log.info("Reader open on %s", config.reader_device)
    reader.run()


if __name__ == "__main__":
    main()
