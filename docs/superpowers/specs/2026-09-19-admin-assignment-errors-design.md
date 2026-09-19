# Catching assignment mistakes in the admin page

Three corrections to the admin page, all about the same thing: making a wrong
assignment visible at the moment it matters rather than at the printer.

1. **Duplicate cards** — surface albums given more than one card.
2. **Artwork overrides apply immediately** — pinning an image shows the image.
3. **Artwork caching** — cache forever, and update the moment art changes.

## Part 1: duplicate cards

### The problem

`cards.yaml` maps UID to `{name, path}`. Nothing stops two UIDs holding the
same `path`, and nothing shows it. With ~132 cards registered, a duplicate is
invisible until two physical cards turn out to play the same record.

A duplicate is **always a mistake**. Every album should have exactly one card.

### Detection

A pure function in `cards.py`:

```python
def duplicate_paths(cards: dict[str, Card]) -> dict[str, list[str]]:
    """Album paths claimed by more than one card, path -> sorted UIDs."""
```

`path` is the identity, matching how the rest of the system already treats a
card — `name` is a label and is deliberately ignored, so two cards labelled
differently for the same album still count as duplicates.

Returns only paths with two or more UIDs. Empty dict when the registry is
clean. No Flask, no I/O, so it tests directly.

### API

`/api/cards` gains one field per card:

```json
{"uid": "...", "name": "...", "path": "...", "duplicate": true}
```

The handler already loads the whole store to build its response, so this costs
one extra pass over the cards and no new endpoint. The page stays a renderer
and does no analysis of its own.

### Cards table

- `cardRow()` sets a CSS class on rows whose card is flagged.
- A count line above the table: *"2 albums have more than one card."*
  Hidden entirely when there are none, so a clean registry stays quiet.

The table is **not** re-sorted. Rows must not move while the user is deleting
one of a pair.

### Registration warning

When the album selected for a new card already has a card, show an inline note
next to the register button naming the existing card. It does **not** block —
the user can proceed. Prevention at the point of the mistake, without a wall.

## Part 2: artwork overrides apply immediately

### The problem

`PUT /api/artwork/override` records the override and returns. The image on
disk and the manifest entry are untouched, so the albums view keeps showing
the old art until a separate Collect run. The page says *"Saved. Collect again
to pick it up."* and that message is easy to miss. The user reasonably reads
the result as "it didn't work".

### Behaviour

Setting or clearing an override collects **that one album, immediately**, and
returns the resulting manifest entry so the page can refresh the tile from
what was actually saved.

Chosen over showing the pasted URL directly: the tile must show what will
print. Hot-linking the remote URL would display art that is not collected, and
a dead link would look correct right up until the sheets were built.

### Endpoint

`PUT /api/artwork/override` after saving `overrides.json`:

1. If a bulk collection is running, skip the collect and return
   `collected: false` with the existing 409-style message. Two writers to
   `manifest.json` would lose an update — `_run_collection` saves after every
   album. The page then falls back to today's "Collect again to pick it up."
2. Otherwise call the existing `_collect_one(album, out_dir, library_root,
   overrides)`, write the entry into the manifest, and save.
3. Return `{album, override, entry, collected: true}`.

Clearing an override re-collects too, so removing a bad pin restores the
searched result rather than stranding the pinned image.

A `skip:` override needs no fetch — `collect()` already short-circuits it —
and goes through the same path for one code path rather than two.

### Failure

A dead link or a URL that is not an image is a normal outcome, not an
exception: `collect()` returns no candidates. The endpoint reports it as an
error message on the page while **keeping the saved override**, so the user
can edit the URL rather than retype it from scratch.

Fetching the image over the box's Wi-Fi takes a second or two. The request is
synchronous and the page shows a "Fetching…" state; this is an admin action
taken by one person at a keyboard, so a short block is simpler than another
background job with its own state to poll.

### Not doing: the assigned-album filter

`artwork_collect` builds targets as `payload.get("all") or not a["assigned"]`,
which runs before the override-mismatch rule, so an override on an album that
already has a card would never be collected by a normal run.

This is unreachable in practice and is being left alone. Collection is scoped
to albums **awaiting a card** by design — that is the print queue — so an
assigned album never appears in the studio to be overridden in the first
place. Part 2 makes it moot regardless, since the override endpoint collects
directly rather than going through target selection.

## Part 3: artwork caching

### The problem

`/artwork-file/<name>` serves `Cache-Control: private, max-age=86400`, and
artwork is **replaced in place under a stable filename**. Within the 24-hour
window the browser does not revalidate at all, so a replaced image keeps
showing the old copy. `ETag` and `Last-Modified` are both correct and both
unused, because nothing asks.

This is what made Part 2's bug look worse than it was: the override had
applied, the file on disk was the new one, and the page showed the old.

### Behaviour

Cache **forever**, and change the URL whenever the bytes change.

- Each manifest row carries the file's modification time.
- The page requests `artwork-file/<name>?v=<mtime>`.
- The response serves `Cache-Control: public, max-age=31536000, immutable`.

A given URL always denotes the same bytes, so it is safe to keep for a year
and the browser never revalidates it. Months between prints cost zero
requests, which is the point. When art is replaced, the mtime changes, the URL
changes, and that one image is fetched exactly once — per-image invalidation
with nothing to remember to invalidate.

`immutable` also stops a reload from revalidating, which a plain long
`max-age` does not.

A full refresh remains available: `POST /api/artwork/collect` with `refetch`
re-fetches everything, and every changed file lands under a new URL.

The `?v=` parameter is ignored by the route — it exists only to key the
browser cache — so the existing path-traversal check is untouched.

## Testing

**Part 1**
- `duplicate_paths`: empty registry; all unique; one pair; a three-way; two
  separate pairs; same path with different `name` values still duplicates.
- `/api/cards`: flag is true on both members of a pair, absent/false on
  singletons.

**Part 2**
- Override on an album collects it and returns the entry.
- Clearing an override re-collects.
- A URL that fetches nothing returns an error and leaves `overrides.json`
  holding the override.
- A collection already running returns `collected: false` and does not write
  the manifest.

**Part 3**
- A manifest row carries an mtime for an album with a collected file, and
  omits it when the file is missing.
- `/artwork-file/...` responds with `immutable` and a one-year `max-age`.
- A `?v=` parameter does not defeat the path-traversal check.

The existing collector seam (`collector is not None` in `_collect_one`) means
none of these tests touch the network.

## Out of scope

- Merging or auto-resolving duplicates. The page reports; the user decides.
- Duplicate detection on `name`.
- Re-sorting or grouping the cards table.
- Any change to how the sheets are built.
