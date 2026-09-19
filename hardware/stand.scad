// Jukebox stand -- a card sits in it the way a record sits on a hi-fi shelf.
//
// Design intent: this lives in a room, next to speakers, with album art on
// the front. It should read as a made object, not a printed bracket. So:
//
//   * every visible edge is chamfered, and the vertical corners are radiused,
//     so the form catches light along its arrises instead of showing a raw
//     printed corner;
//   * a shadow reveal runs round the base, which lifts the body optically and
//     hides the one joint where a print's first layers look worst;
//   * the card drops into a slot in a full-width rail rather than leaning on
//     an applied shelf, so the front is two clean horizontal lines;
//   * a thumb scoop in the rail lets you lift a card out without scratching
//     the art or clawing at the edge;
//   * the back is open, so the Pi's ports are simply reachable -- no cutouts
//     to misalign, no internal extension cables, and ventilation for free.
//
//   openscad -o coupon.stl -D 'part="coupon"' stand.scad   // print FIRST
//   openscad -o stand.stl  stand.scad
//
// ---------------------------------------------------------------------------
// MEASURE BEFORE PRINTING THE STAND.
// window_t is the only dimension that can quietly ruin this: too thick and the
// reader stops seeing 4-byte cards -- and it fails as intermittent flapping
// rather than an honest "no read", which is far harder to diagnose once it is
// assembled. Print the coupon, test with your worst card, watch the presence
// cycle count rather than whether it reads at all.
// ---------------------------------------------------------------------------

part = "stand";

/* [Card] */
card       = 95;      // printed card, mm square
card_t     = 1.6;     // slot width: card plus a little, so it drops in
lean       = 14;      // face angle from vertical

/* [Proportions] */
side_margin = 12.5;   // face material either side of the card
top_margin  = 12;     // above the card
lip_h       = 11;     // the lip the card stands on
depth       = 66;
back_h      = 0.74;   // side walls' height at the back, as a fraction of the
                      // front. Lower looks lighter; too low and the Pi and the
                      // whole cavity are on show from the side.

/* [Detail] */
corner_r   = 5;       // radius on the vertical corners
chamfer    = 1.2;     // on every visible edge
reveal_h   = 2.5;     // shadow groove round the base
reveal_d   = 1.8;
reveal_z   = 2.5;     // sits BELOW the lip, so the two read as separate
                      // lines rather than merging into one heavy plinth
taper      = 9;       // how far the sides rake in, per side, bottom to top
lip_depth  = 9;       // how far the lip projects from the face
lip_face   = 3.5;     // height of the lip's own front face, above its chamfer
felt_inset = 1.5;     // felt recess held back from the front edge
felt_w     = 6;       // recess for the felt strip, 0 to leave the lip flat
felt_t     = 1.2;

/* [Shell] */
face_t     = 3.2;     // panel thickness away from the reader
window_t   = 1.2;     // thinned panel over the reader antenna
wall       = 3;
base_t     = 4;

/* [Raspberry Pi 4] */
pi_w = 85; pi_h = 56;
pi_hole_dx = 58; pi_hole_dy = 49;
standoff_h = 5; standoff_d = 6.4; screw_d = 2.3;

/* [PN532 HAT antenna] */
// Offset of the coil from the CENTRE of the Pi board, in the face's own
// frame: +x right, +z up, seen from the front. MEASURE YOURS -- the cards
// carry their tag in one corner, so this is what makes the two line up.
ant_dx = -24; ant_dy = -22;
ant_w  = 46;  ant_h  = 42;

$fn = 64;

// --- derived -----------------------------------------------------------
// Wide enough for the card, and for the Pi wherever the tag offset puts it.
//
// Taking the max rather than trusting side_margin, because the failure it
// prevents is silent: a mounting boss that lands on the side wall merges into
// it and simply disappears from the render, so you discover it when the Pi
// will not screw down.
boss_reach = abs(ant_dx) + pi_hole_dx/2 + standoff_d/2 + wall + 2.5;
face_w   = max(card + 2*side_margin, 2*boss_reach);
face_h   = lip_h + card + top_margin;
card_mid = lip_h + card/2;                  // card centre, up the face
lean_x   = face_h * sin(lean);
top_z    = face_h * cos(lean);      // the body's height in world z

// Width still available where the card's top corners sit. The taper must not
// eat into them -- a card that fouls the rake would sit proud at the top and
// the whole thing would look like a mistake.
card_top_z = (lip_h + card) * cos(lean);
width_at_card_top = face_w - 2*taper * card_top_z/top_z;

// --- helpers -----------------------------------------------------------

// A rectangle with radiused corners, used in plan to round the body.
module rounded_rect(w, d, r) {
    offset(r = r) offset(r = -r) square([w, d], center = true);
}

// The side elevation: slanted front, flat chamfered top, vertical back.
// offset(+r) offset(-r) is an opening, which rounds the convex corners --
// the ones you can actually see and run a thumb along.
module side_profile() {
    r = 2.5;
    // The front face must run at exactly `lean`, because the cavity, the card
    // slot and the standoffs are all built in a frame rotated by that angle.
    // An earlier version fudged the top point by +6mm to get a flat top,
    // which quietly made the real face 16.7 degrees -- the cavity was then no
    // longer parallel to it and sliced the whole panel away.
    // The outer face passes through the origin, so in the face's own frame the
    // panel runs from y=0 (outside) to y=face_t (inside) and everything built
    // in that frame -- cavity, window, standoffs -- measures from the surface
    // you can actually touch. Offsetting this edge instead put the *outer*
    // surface at y=face_t, exactly where the cavity began, and the hollowing
    // then removed the entire panel.
    top_y = face_h * sin(lean);
    offset(r = r) offset(r = -r)
        polygon([
            [0, 0],
            [depth, 0],
            [depth, top_z * back_h],         // sides carry back to here
            [top_y, top_z],
        ]);
}

module body() {
    intersection() {
        // the wedge, extruded wide enough to be trimmed
        translate([-face_w, 0, 0])
            rotate([90, 0, 90])
                linear_extrude(face_w * 2) side_profile();
        // ...trimmed in plan, which rounds the four vertical corners and
        // rakes the sides inward as they rise -- the taper an arcade cabinet
        // uses, and for the same reason: straight sides read as a box, angled
        // ones as a made object. Depth is left alone (scale [sx, 1]) so the
        // footprint stays stable and the back stays square to the world.
        translate([0, depth/2, 0])
            linear_extrude(top_z, scale = [(face_w - 2*taper) / face_w, 1])
                rounded_rect(face_w, depth, corner_r);
    }
}

module reveal_groove() {
    // A shadow line round the base. Optically it lifts the body off the
    // surface, and it hides the layer banding that a first print always shows
    // low down where the part is widest.
    translate([0, depth/2, reveal_z])
        linear_extrude(reveal_h)
            difference() {
                rounded_rect(face_w + 4, depth + 4, corner_r);
                rounded_rect(face_w - 2*reveal_d, depth - 2*reveal_d, corner_r);
            }
}

// Face features are placed in the face's own frame: local x across the
// width, y into the panel, z up the slope. The body leans in the YZ plane
// (side_profile is extruded along X), so that frame is a rotation about the
// X axis -- rotating about Y instead tilts everything sideways and leaves the
// standoffs hanging in front of the panel.
module card_lip() {
    // A ledge the card stands on, full width, with the felt recessed into it
    // so the strip finishes flush rather than sitting proud.
    //
    // The underside is chamfered back to the face. A ledge projecting from a
    // panel that already leans back is a full overhang, and would otherwise
    // need support exactly where the finish shows most.
    intersection() {
        rotate([-lean, 0, 0])
            rotate([90, 0, 90])
                linear_extrude(face_w * 2, center = true)
                    polygon([
                        [0, 0],
                        [0, lip_h],
                        [-lip_depth, lip_h],
                        [-lip_depth, lip_h - lip_face],
                    ]);
        // Clipped to the body's plan, so the lip ends flush with the raked
        // sides and picks up the same corner radius instead of standing out
        // past them as a slab.
        translate([0, (depth - lip_depth - 6)/2, 0])
            linear_extrude(top_z, scale = [(face_w - 2*taper) / face_w, 1])
                rounded_rect(face_w, depth + lip_depth + 6, corner_r);
    }
}

module felt_recess() {
    if (felt_w > 0)
        rotate([-lean, 0, 0])
            translate([-face_w, -lip_depth + felt_inset, lip_h - felt_t])
                cube([face_w * 2, felt_w, felt_t + 1]);
}

module antenna_window() {
    // Thinned from BEHIND: the outside stays flat and rigid, the reader only
    // sees window_t of plastic.
    // Clipped to stay above the lip. The window leaves window_t of material
    // measured from the FRONT, and the card rebate removes rebate_d from that
    // same front face -- so anywhere the two overlap there is nothing left at
    // all, whatever the panel thickness, and the face opens into a slot. The
    // coil can lose its bottom few millimetres harmlessly; a hole in the front
    // of the stand it cannot.
    intersection() {
        rotate([-lean, 0, 0])
            translate([ant_dx - ant_w/2, window_t, card_mid + ant_dy - ant_h/2])
                // Runs past the panel's inner surface on purpose: ending flush
                // with the cavity leaves coincident faces, which render as
                // speckle and can confuse a slicer.
                cube([ant_w, face_t - window_t + 4, ant_h]);
        rotate([-lean, 0, 0])
            translate([-face_w, -face_t, lip_h + 1.5])
                cube([face_w * 2, face_t * 6, face_h * 2]);
    }
}

module cavity() {
    // Hollow the body from the back, leaving face_t behind the slanted face,
    // `wall` at the sides and base_t underneath.
    //
    // Intersected with a world-space box rather than used bare: the cutter is
    // rotated with the face, so towards the back its "floor" has dropped well
    // below the base and it takes the bottom of the stand with it.
    intersection() {
        // behind the panel
        rotate([-lean, 0, 0])
            translate([-face_w, face_t, -face_h])
                cube([face_w * 2, depth * 1.6, face_h * 3]);
        // inside the side walls -- and tapering with them.
        //
        // A constant-width cavity is wrong once the body rakes inward: above
        // the height where the shrinking body is narrower than the cavity, the
        // cavity takes the entire cross-section and the side walls simply do
        // not exist. That is invisible from the front and obvious the moment
        // you look at a side elevation.
        translate([0, depth/2, 0])
            linear_extrude(top_z * 1.2, scale = [(face_w - 2*taper) / face_w, 1])
                rounded_rect(face_w - 2*wall, depth * 3, corner_r);
        // above the base
        translate([-face_w, -depth, base_t])
            cube([face_w * 2, depth * 3, face_h * 2]);
    }
}

module pi_standoffs() {
    rotate([-lean, 0, 0])
        translate([ant_dx, face_t, card_mid + ant_dy])
            rotate([-90, 0, 0])
                for (x = [-pi_hole_dx/2, pi_hole_dx/2],
                     y = [-pi_hole_dy/2, pi_hole_dy/2])
                    translate([x, y, 0])
                        difference() {
                            cylinder(d = standoff_d, h = standoff_h);
                            translate([0, 0, -1])
                                cylinder(d = screw_d, h = standoff_h + 2);
                        }
}

module vents() {
    // Underneath only. Nothing that shows from the front or the sides.
    for (i = [-2 : 2])
        translate([i * 13, depth * 0.62, -1])
            cube([6, 26, base_t + 2], center = false);
}

// The Pi's position is driven by the tag in the card, via ant_dx/ant_dy. Push
// it too far and the mounting bosses disappear into the side wall, which is
// invisible in a render -- they simply merge with it.
assert(abs(ant_dx) + pi_hole_dx/2 + standoff_d/2 < face_w/2 - wall,
       "ant_dx puts the Pi's bosses into the side wall - widen side_margin, or turn the Pi 180 degrees to flip the antenna offset");
assert(card_mid + ant_dy - pi_hole_dy/2 - standoff_d/2 > base_t,
       "ant_dy puts the Pi's lower bosses into the base - raise lip_h");

assert(width_at_card_top > card + 6,
       "taper is too steep - the sides close in on the card's top corners");

module stand() {
    difference() {
        union() {
            difference() {
                body();
                cavity();
                reveal_groove();
            }
            card_lip();
            pi_standoffs();
        }
        felt_recess();
        antenna_window();
        vents();
    }
}

module coupon() {
    steps = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0];
    for (i = [0 : len(steps) - 1])
        translate([i * 34, 0, 0]) {
            cube([32, 32, steps[i]]);
            translate([2.5, 3, steps[i]])
                linear_extrude(0.6) text(str(steps[i]), size = 7);
        }
}

if (part == "coupon") coupon(); else stand();
