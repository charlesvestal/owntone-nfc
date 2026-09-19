// Jukebox stand -- a slanted face that holds the card like a record on a
// shelf, with the Pi mounted directly behind it and the back left open.
//
// The open back is the whole idea. The Pi's ports face into it, so cables
// plug straight into the board: no panel cutouts to get wrong, no internal
// extension cables, and the airflow comes free. From the front you see a
// sleeve on a stand; everything else is behind it.
//
// The card leans back rather than standing upright, so gravity holds it
// against the face instead of a strip of felt doing all the work.
//
//   openscad -o stand.stl -D part=\"stand\" stand.scad
//   openscad -o coupon.stl -D part=\"coupon\" stand.scad   // print this FIRST
//
// ---------------------------------------------------------------------------
// MEASURE BEFORE PRINTING THE STAND.
//
// face_t is the only dimension that can quietly ruin this: too thick and the
// reader stops seeing 4-byte cards, which fails as intermittent flapping
// rather than an honest "no read". Print the coupon, run
// spikes/presence_check.py with your *worst* card against each step, and
// watch the presence cycle count rather than whether it reads at all.
// ---------------------------------------------------------------------------

part = "stand";          // "stand" | "coupon"

/* [Card] */
card        = 95;        // printed card, mm square (matches the A4 sheets)
card_gap    = 2.5;       // clearance each side so a card drops in easily
lean        = 15;        // face angle from vertical; 12-18 feels right

/* [Card lip] */
lip_depth   = 7;         // how far the shelf sticks out
lip_height  = 9;         // how far up the card's face it comes
felt_w      = 6;         // recess for the felt strip, 0 to leave it flat
felt_t      = 1.2;       // felt thickness, so it finishes flush

/* [Shell] */
face_t      = 3;         // panel thickness AWAY from the reader
window_t    = 1.2;       // thinned panel over the reader antenna
wall        = 3;
base_t      = 4;
margin      = 6;         // panel material around the card

/* [Raspberry Pi 4] */
pi_w        = 85;        // long edge, mounted horizontally
pi_h        = 56;
pi_hole_dx  = 58;        // mounting hole spacing
pi_hole_dy  = 49;
pi_hole_in  = 3.5;       // holes this far in from two edges
standoff_h  = 5;         // clears the solder tails under the board
standoff_d  = 6.5;
screw_d     = 2.3;       // for M2.5 self-tapping into plastic

/* [PN532 HAT antenna] */
// Where the antenna sits, as an offset from the CENTRE of the Pi board.
// On this HAT it is over to one side -- measure yours and set this, then the
// thin window and the card's resting position line up.
ant_dx      = 22;        // + is towards the Ethernet end
ant_dy      = 0;
ant_w       = 44;        // window size; a little larger than the coil
ant_h       = 40;

$fn = 48;

// --- derived -----------------------------------------------------------

face_w   = card + 2*card_gap + 2*margin;
face_h   = card + lip_height + margin;      // card sits on the lip
depth    = 62;                              // front-to-back footprint
lean_x   = face_h * sin(lean);              // how far the top leans back

// The card's centre, measured up the sloping face from the lip.
card_mid = lip_height + card/2 - lip_height/2;

// --- parts -------------------------------------------------------------

module side_profile() {
    // Seen from the side: a wedge. Vertical at the back so it prints flat and
    // does not tip; the front face carries the lean.
    polygon([
        [0, 0],
        [depth, 0],
        [depth, face_h*cos(lean) * 0.55],   // back is cut down; nothing needs it
        [lean_x + face_t/cos(lean), face_h*cos(lean)],
        [face_t/cos(lean), 0.001],
    ]);
}

module shell() {
    difference() {
        union() {
            // base
            translate([-face_w/2, 0, 0]) cube([face_w, depth, base_t]);
            // two side ribs
            for (s = [-1, 1])
                translate([s*(face_w/2) - (s<0 ? 0 : wall), 0, 0])
                    rotate([90, 0, 90])
                        linear_extrude(wall) side_profile();
            // the slanted face itself
            face_panel();
            card_lip();
        }
        // hollow out under the base for cable routing / weight
        translate([-face_w/2 + wall, wall, -1])
            cube([face_w - 2*wall, depth - 2*wall, base_t - 2 + 1]);
    }
}

module face_panel() {
    // A slab standing at `lean` from vertical, hinged at the front edge.
    rotate([0, -lean, 0])
        translate([-face_w/2, 0, 0])
            cube([face_w, face_t, face_h]);
}

module card_lip() {
    // The shelf the card sits on. Chamfered underneath so it prints without
    // support, and recessed on top for the felt.
    rotate([0, -lean, 0])
        translate([-face_w/2, 0, 0])
            difference() {
                union() {
                    cube([face_w, face_t + lip_depth, lip_height]);
                    // chamfer under the overhang
                    translate([0, face_t + lip_depth, 0])
                        rotate([0, 90, 0])
                            linear_extrude(face_w)
                                polygon([[0,0], [0,-lip_depth], [lip_depth,0]]);
                }
                if (felt_w > 0)
                    translate([margin, face_t + 1, lip_height - felt_t])
                        cube([face_w - 2*margin, felt_w, felt_t + 1]);
            }
}

module pi_mount() {
    // Standoffs on the back of the face, with the board centred.
    //
    // Deliberately NOT shifted to put the antenna behind the card's centre:
    // that pushes an 85mm board 8.5mm past the edge of the panel, and it buys
    // nothing. A card-format tag has its coil running round the whole
    // perimeter, so the reader only has to be somewhere behind the card, not
    // under its middle. Centred, the antenna lands ant_dx off centre, which
    // is still well inside the card's 95mm.
    rotate([0, -lean, 0])
        translate([0, face_t, card_mid])
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

module antenna_window() {
    // Thin the panel from BEHIND, so the outside stays flat and rigid while
    // the reader only has window_t of plastic to see through.
    rotate([0, -lean, 0])
        translate([ant_dx - ant_w/2, window_t, card_mid + ant_dy - ant_h/2])
            cube([ant_w, face_t - window_t + 0.01, ant_h]);
}

module vents() {
    for (i = [0 : 5])
        translate([-face_w/2 + 14 + i*14, depth - wall - 1, base_t + 6])
            cube([5, wall + 2, 14]);
}

module stand() {
    difference() {
        union() { shell(); pi_mount(); }
        antenna_window();
        vents();
    }
}

// A stepped coupon: print, then test each pad with your worst card.
module coupon() {
    steps = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0];
    for (i = [0 : len(steps) - 1])
        translate([i * 34, 0, 0]) {
            cube([32, 32, steps[i]]);
            translate([2, 2, steps[i]])
                linear_extrude(0.6)
                    text(str(steps[i]), size = 7);
        }
}

if (part == "coupon") coupon(); else stand();
