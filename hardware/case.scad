// ============================================================================
// Bacon BBS Pico cache node — parametric enclosure
// ----------------------------------------------------------------------------
// Holds a RAK19007 WisBlock base board (with RAK4631 core) and a Raspberry Pi
// Pico side-by-side in a tray, with a snap/screw lid, USB-C and antenna
// cutouts. Everything is driven by the variables below — print a test, check
// fit, tweak the numbers. Dimensions marked "VERIFY" should be confirmed
// against the board you actually have (calipers or datasheet) before printing
// the final.
//
// Render: set SHOW to "tray", "lid", or "both".
// Export each part to its own STL (set SHOW, then F6 -> export).
// ============================================================================

SHOW = "both";              // "tray" | "lid" | "both"
$fn = 48;                   // curve smoothness

// ---- Global shell ----------------------------------------------------------
wall        = 2.0;          // side wall thickness
floor_t     = 2.0;          // tray floor thickness
lid_t       = 2.0;          // lid top thickness
gap         = 0.4;          // print clearance between mating parts
board_clear = 1.0;          // clearance around each PCB inside the tray
part_gap    = 6.0;          // space between the two boards

// ---- Boards (L x W x PCB-thickness, mm) ------------------------------------
// RAK19007 base board: datasheet says 60 x 30 mm. VERIFY thickness/components.
rak_l = 60.0; rak_w = 30.0; rak_pcb = 1.6;
rak_comp_h = 12.0;          // tallest stuff above the RAK PCB (core module + USB) VERIFY

// Raspberry Pi Pico: 51.0 x 21.0 mm, 1.0 mm PCB.
pico_l = 51.0; pico_w = 21.0; pico_pcb = 1.0;
pico_comp_h = 6.0;          // headers / components above the Pico PCB VERIFY

// ---- Standoffs / mounting --------------------------------------------------
standoff_h  = 4.0;          // PCB sits this high off the floor (room for pins underneath)
standoff_od = 5.0;          // standoff outer diameter
screw_d     = 2.2;          // pilot hole for an M2 self-tapping screw (VERIFY hole pattern!)
hole_inset  = 3.0;          // mounting-hole inset from each PCB corner (VERIFY per board)

// ---- Port cutouts ----------------------------------------------------------
// USB-C on the RAK19007 is on one short (30 mm) end. Cutout in that wall.
usbc_w = 10.0; usbc_h = 4.0; usbc_z = standoff_h + rak_pcb;   // bottom of port above floor
antenna_d = 7.0;            // SMA bulkhead / antenna hole diameter (VERIFY)
antenna_z = standoff_h + 6; // antenna hole height up the wall

// ---- Lid fixing ------------------------------------------------------------
lid_screw_d   = 2.6;        // M2.5 clearance through the lid
lid_boss_od   = 7.0;        // screw boss diameter in the tray corners
lid_lip       = 3.0;        // how far the lid lip drops inside the tray

// ============================================================================
// Derived inner cavity
// ============================================================================
inner_l = max(rak_l, pico_l) + 2*board_clear;
inner_w = rak_w + part_gap + pico_w + 2*board_clear;
inner_h = standoff_h + max(rak_pcb + rak_comp_h, pico_pcb + pico_comp_h) + 2.0;

outer_l = inner_l + 2*wall;
outer_w = inner_w + 2*wall;
outer_h = floor_t + inner_h;

// Board origins (front-left corner of each PCB, in inner coordinates)
rak_x  = board_clear + (inner_l - 2*board_clear - rak_l)/2;
rak_y  = board_clear;
pico_x = board_clear + (inner_l - 2*board_clear - pico_l)/2;
pico_y = board_clear + rak_w + part_gap;

// ============================================================================
// Helpers
// ============================================================================
module standoff(x, y) {
    translate([x, y, floor_t])
        difference() {
            cylinder(h = standoff_h, d = standoff_od);
            translate([0,0,-0.1]) cylinder(h = standoff_h + 0.2, d = screw_d);
        }
}

// Four standoffs at a board's mounting-hole corners.
module board_standoffs(ox, oy, l, w) {
    for (dx = [hole_inset, l - hole_inset])
        for (dy = [hole_inset, w - hole_inset])
            standoff(wall + ox + dx, wall + oy + dy);
}

module lid_boss(x, y) {
    translate([x, y, floor_t])
        difference() {
            cylinder(h = inner_h, d = lid_boss_od);
            translate([0,0, inner_h - 8]) cylinder(h = 8.1, d = screw_d);
        }
}

// ============================================================================
// Tray
// ============================================================================
module tray() {
    difference() {
        // outer shell
        cube([outer_l, outer_w, outer_h]);
        // inner cavity
        translate([wall, wall, floor_t])
            cube([inner_l, inner_w, inner_h + 1]);
        // USB-C cutout in the front wall (RAK USB end, -Y side, over the RAK board)
        translate([wall + rak_x + rak_l/2 - usbc_w/2, -0.1, floor_t + usbc_z])
            cube([usbc_w, wall + 0.2, usbc_h]);
        // antenna hole in the left wall
        translate([-0.1, wall + rak_y + rak_w/2, floor_t + antenna_z])
            rotate([0,90,0]) cylinder(h = wall + 0.2, d = antenna_d);
    }
    // mounting standoffs
    board_standoffs(rak_x,  rak_y,  rak_l,  rak_w);
    board_standoffs(pico_x, pico_y, pico_l, pico_w);
    // lid screw bosses in the four corners
    lid_boss(wall + lid_boss_od/2,            wall + lid_boss_od/2);
    lid_boss(outer_l - wall - lid_boss_od/2,  wall + lid_boss_od/2);
    lid_boss(wall + lid_boss_od/2,            outer_w - wall - lid_boss_od/2);
    lid_boss(outer_l - wall - lid_boss_od/2,  outer_w - wall - lid_boss_od/2);
}

// ============================================================================
// Lid (printed separately, flipped)
// ============================================================================
module lid() {
    difference() {
        union() {
            // top plate
            cube([outer_l, outer_w, lid_t]);
            // lip that drops into the cavity
            translate([wall + gap, wall + gap, -lid_lip])
                cube([inner_l - 2*gap, inner_w - 2*gap, lid_lip]);
        }
        // screw clearance holes over the four bosses
        for (cx = [wall + lid_boss_od/2, outer_l - wall - lid_boss_od/2])
            for (cy = [wall + lid_boss_od/2, outer_w - wall - lid_boss_od/2])
                translate([cx, cy, -lid_lip - 0.1])
                    cylinder(h = lid_t + lid_lip + 0.2, d = lid_screw_d);
        // vent slots
        for (i = [-2:2])
            translate([outer_l/2 + i*5 - 1, outer_w*0.30, -0.1])
                cube([2, outer_w*0.40, lid_t + 0.2]);
    }
}

// ============================================================================
// Assembly preview
// ============================================================================
if (SHOW == "tray" || SHOW == "both") tray();
if (SHOW == "lid"  || SHOW == "both")
    translate([0, 0, SHOW == "both" ? outer_h + 15 : 0]) lid();

echo(str("Outer footprint: ", outer_l, " x ", outer_w, " x ", outer_h, " mm"));
