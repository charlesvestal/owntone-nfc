# Card artwork

Collects print-resolution cover art for albums that don't have a card yet,
so new records can be turned into cards without hunting for images by hand.

## Running it

```sh
./refresh.sh
```

That's the whole loop. It fetches artwork for every album the jukebox reports
as unassigned, copies the results to `~/Documents/_Personal/music/cardart/`,
and opens a review sheet.

Re-run it whenever you add music. Work already done is cached, so a second run
only touches albums that are new, that failed last time, or whose override you
have edited. `--refetch` starts over from scratch.

The fetching runs on the Pi, because that is where the music is: artwork
already in the library is preferred over anything downloaded, and it can only
be read there.

## Reviewing

`review.html` shows every album as a contact sheet, colour-coded:

| | |
|---|---|
| green | came from the library — cannot be a mismatch |
| blue | fetched, and the title fits |
| amber | fetched, but the matched title looks like a different album |
| red | too small to print, or nothing found |
| grey | deliberately skipped |

**Look at the amber and red ones.** A search will return a plausible, sharp,
completely wrong album — this began with a search for Liquid Mike's
self-titled record confidently returning *Paul Bunyan's Slingshot*. The
scoring catches most of those, but only your eyes catch all of them.

## Fixing what it got wrong

Edit `overrides.json` in the output directory, then run `./refresh.sh` again.

```json
{
  "Liquid Mike/S_T":      {"artist": "Liquid Mike", "album": "Liquid Mike"},
  "Jonas Alaska/Girl":    {"url": "https://example.com/sleeve.jpg"},
  "ZZ Test/Stereo Test":  {"skip": "stereo test disc, not a record"}
}
```

- **`artist` / `album`** — search for something else. The first thing to try
  when the library's folder name isn't what catalogues call the record.
- **`url`** — use this exact image. For anything too obscure to be found, or
  when you've scanned the sleeve yourself.
- **`skip`** — this album will never have a card. Say why; it shows on the
  review sheet.

Overrides are permanent and survive `--refetch`.

## Where the art comes from

Asked in this order, but **all** answers are collected and the best one wins —
first-reply-wins is how the wrong album gets printed.

1. **The library** — `cover.jpg` and art embedded in the FLACs. Always
   preferred when it's big enough, because it can't be the wrong record.
2. **iTunes** — no key needed, usually 3000×3000. The best single source.
3. **Deezer** — no key needed, 1000×1000. Fills gaps iTunes misses, including
   both CAKE albums.
4. **Cover Art Archive** — via MusicBrainz, searched at *release* level as
   well as release-group, because the scans hang off individual releases. Slow
   (rate limited to 1 request/sec) and prone to 503s, so it's only consulted
   when nothing else is convincing. It's where the real Liquid Mike sleeve
   turned up at 3000×3000.

Amazon isn't usable: unauthenticated requests are refused, and the Product
Advertising API needs an Associates account with qualifying sales.

## Printing

```sh
python3 make_sheets.py --dir ~/Documents/_Personal/music/cardart
```

Writes `covers-to-print.pdf` next to the images: **95mm squares, six to an A4
page**, two columns of three, evenly spaced. The geometry was measured from
the Affinity Publisher export that produced the existing deck, so new cards
come out the same size as the ones already in the box — verified against it to
within 0.3mm.

JPEGs are embedded as-is rather than re-encoded, so nothing is resampled on
the way through. Artwork that isn't square is centre-cropped with a clipping
path: the picture is scaled to cover the square and the square is painted.
Centre-cropping is the right call for sleeves, where the title and label sit
in the middle.

Questionable matches and anything marked skipped are left out by default;
`--include-suspect` overrides that, and `--min-px` drops low-resolution art.

## Resolution

Cards print at **95mm square**, so 1000px is 267 dpi and the common 1400px is
374 dpi. The floor of **1000px** comes from the existing deck, which runs
1000–1400px with a median of 1200 — measured from what has actually looked
fine printed, rather than a textbook number. Change it with `--min-px`.

## Files

| | |
|---|---|
| `refresh.sh` | the one command — fetch, copy back, review |
| `fetch_art.py` | collects and ranks candidates |
| `make_review.py` | builds `review.html` |
| `make_sheets.py` | builds the print-ready A4 PDF |
| `artlib.py` | image headers, search terms, match scoring (tested in `tests/test_cardart.py`) |
