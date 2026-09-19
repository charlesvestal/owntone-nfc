# Brief: card stand for the NFC record player

## What the object is

A printed 95 × 95 mm album-art card stands on it, leaning back, displayed like
a sleeve on a shelf. A PN532 NFC HAT behind the card reads a tag in the card; a
Raspberry Pi 4 hangs off the HAT on its GPIO header. Put the card on, the album
plays; take it off, it stops. It lives in a living room next to speakers. **The
card is the interface and the hero — the object should be quiet.**

Design language: Dieter Rams. Continuous lines, nothing applied, nothing fussy.

## Hard constraints — all measured, none negotiable

| | |
|---|---|
| Card | 95 × 95 mm, ~0.5 mm thick, leans against the face |
| Tag in the card | centre **+17.5 mm right, −17.5 mm down** from the card's centre, **viewed from the front** |
| PN532 HAT | 85 × 56 mm board |
| HAT coil panel | 37.4 × 37.8 mm, centred **+17.0 mm along the long axis, +2.3 mm across** from the board's centre, on the opposite side from the GPIO connector |
| HAT tall parts | 1×17 header and 2×3 jumper block stand **7.6 mm proud** of the board and face the panel |
| Coil to card | ~14 mm. **Confirmed to read reliably — do not trade complexity to reduce it.** |
| Raspberry Pi 4 | 85 × 56 mm, hangs behind the HAT on the GPIO header; ~30 mm of stack |
| Pi ports | **face upward.** Forced, not chosen — see below |

**The HAT is the board nearest the card**, with the Pi behind it. Mounting the
Pi to the face instead puts the HAT ~20 mm further away with the Pi's ground
planes in between; it will not read.

**The coil must land on the tag.** That admits exactly one HAT orientation
(long axis horizontal, GPIO edge down), which is what forces the Pi's port edge
to face **up**. Everything else follows from this.

**Cables: the owner will not buy any.** Straight USB-C and 3.5 mm leads only.
A straight plug stands ~20–25 mm above the port plus bend radius, so either the
body is ~120 mm tall or **the back is genuinely open** and cables leave
rearward. The back should be open.

**No fasteners.** The owner has no M2.5 hardware and does not want to buy any.
Current solution, which works: the board's bottom edge drops into two seats and
leans **backward** onto two ribs — gravity holds it, as it does the card.

**Enclosed from normal view.** Solid face, solid sides. Nothing on the front
face but the card — no screws, no caps, no features.

## Printing

- FDM, Bambu, printed **base-down in one piece, no supports**.
- Nothing may overhang more than ~45° from vertical.
- Watertight: one connected component, zero non-manifold edges, flat bottom at z=0.

## The unresolved problem

**Where the lip meets the face, at the corners.** Everything else is settled.

The owner's words, in order, over six failed attempts:

> "the lip's edges and the body's don't meet well. there's a curve into a hard edge, instead of anything continuous"
> "think continuous smooth lines", "dieter rams curves"
> "where the lip connects also needs to curve out to meet the face / edge of the body"
> "when viewed from above the contour of the body and the lip should look like this" — a sketch of a long straight front with an **S at each end** sweeping back into the body's sides
> "do you see how it reaches the OUTER flat edge as opposed to the FRONT flat face"
> "either make the lip the width of the uncurved part of the face or make that part of the face transition to the outer curve of the lip. the sharp transition has to go"

**Do not guess at this from the description — it has been guessed at six times
and got worse.** A plan view with a millimetre scale is at
`~/Desktop/jukebox-stand/plan-with-scale.png`; get the owner to mark the two
numbers, and build to them.

### What has already been tried, and why each failed

| Attempt | Why it failed |
|---|---|
| Lip as a separate solid, clipped to its own plan | Its corner arcs sat at a different depth from the body's; across the body's corner radius the lip stayed at full projection while the face was already turning away, so they met at an angle |
| Lip inset by `corner_r`, returning on a 15 mm arc | Crossed the face at ~66°, leaving a cusp; the return radius is not the problem |
| Larger return radii (26, 32 mm) | Shallower crossing but the shelf under the card's outer corners disappears |
| Cove (morphological closing) at the junction | Rounds concave plan corners, but the visible cusp is where the lip's chamfered *underside* converges — a 3D feature a 2D closing cannot touch |
| Explicit tangent S drawn as a polygon | Correct in 2D, but destroyed by `hull()` — see traps |
| Lip narrowed to the face's flat width | Latest state. Still reads as reaching the body's outer edge |

## Traps — every one of these cost a cycle

- **`hull()` takes the CONVEX hull.** It silently deletes concave plan geometry. An explicit S-curve was erased by it twice before this was spotted. Loft with successive hulls only where the outline is convex.
- **Exact coincidence or tangency between solids produces non-manifold edges** in CGAL. A lip meeting a panel at exactly zero volume became a separate shell; cone bases sitting exactly on a wall produced bad edges; a lip ending exactly on a tangent point produced four. Always overlap by a few tenths.
- **`offset(r = -n)` on a 2D profile shrinks it in BOTH axes.** Using it to compensate for a Minkowski sum ate 5 mm of the lip's height and deleted it.
- **Features built in the face's tilted frame do not respect the base plane.** The lip and the boss cones both hung below z=0. Trim the whole part to z ≥ 0.
- **A cutter that runs past its target keeps cutting.** With the coil window's thinning disabled, its cutter still carved 4 mm behind the panel and put gaps in the standoff cones.
- **The panel leans back, so gravity pulls the board AWAY from it.** An earlier design had the board resting on the panel; it would have fallen off. It must lean backward onto ribs.
- **Printed bosses off the face are infeasible.** The coil reaches within ~3.3 mm of the HAT's mounting holes, and a boss 11 mm off a near-vertical face needs ~15 mm of 45° gusset, which lands on the coil. A snap cantilevered off the panel is the same mistake.
- **Two modules with the same name:** OpenSCAD silently uses the last one. A stale `body()` was rendered for several rounds.

## Verification — not optional

OpenSCAD (native, universal binary; the Homebrew one is Intel-only and Rosetta
is not installed):

    /Users/charlesvestal/.claude/jobs/323f8796/tmp/oscadmnt/OpenSCAD.app/Contents/MacOS/OpenSCAD

**Render front, three-quarter, back, side and a TOP/PLAN view, and LOOK at
every one with the Read tool.** Several faults here were invisible in a
three-quarter view and obvious in plan. Do not declare anything done without
looking. Save renders where the owner can open them — they cannot see images
that are only read.

**Then run the owner's own slicer**, which is the authority:

    /Applications/BambuStudio.app/Contents/MacOS/BambuStudio --info file.stl

It reports `manifold` and `number_of_parts`. It caught a floating snap hook
that both a render and a hand-rolled mesh check had missed. Nothing goes to the
owner without `manifold = yes` and `number_of_parts = 1`.

## Where things are

- `hardware/stand.scad` — current design, v1 shape: tapered wedge, raked sides, open back, integral lip, seats and ribs, no fasteners. **This is the shape the owner wants.**
- `hardware/stand2.scad` — an alternative that was rejected (open at the top).
- `~/Desktop/jukebox-stand/` — STLs and renders the owner looks at.

## Deliver

A corrected `hardware/stand.scad`, renders in `~/Desktop/jukebox-stand/`, an
STL that passes both the mesh check and the slicer check, and a plain statement
of what was changed and anything still unresolved. Do not commit to git.
