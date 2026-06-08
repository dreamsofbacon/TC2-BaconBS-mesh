// ============================================================================
// TC2-BaconBS solar cache node — parametric enclosure (hardened)
// ----------------------------------------------------------------------------
// Holds three things side by side in a tray with a vented screw-down lid:
//   1. RAK19007 WisBlock base (60 x 30 mm) carrying the RAK4631 radio core
//   2. a CARRIER protoboard/PCB that the XIAO nRF52840 + microSD breakout +
//      load switches solder onto (the XIAO itself has NO mounting holes, so it
//      is never screwed down directly — it rides the carrier)
//   3. a LiPo battery bay
//
// Mounting is by CORNER-NEST, not screw holes: each board drops into a set of
// corner locators and rests on support pillars; the lid presses down to retain
// it. This is deliberately independent of exact mounting-hole coordinates
// (which for the RAK19007 live only in datasheet figures) and tolerant of board
// variation. Optional M1.2 screw posts for the RAK are noted but off by default.
//
// CONFIRMED dims: RAK19007 60x30 mm; XIAO nRF52840 21.0x17.8x3.5 mm.
// VERIFY before final print: component heights, the carrier size you actually
// build, and the USB-C / antenna positions on your RAK (edge + offset).
//
// Render: set SHOW; F6; export tray and lid separately.
// ============================================================================

SHOW = "both";              // "tray" | "lid" | "both"
$fn = 48;

// ---- Shell -----------------------------------------------------------------
wall        = 2.0;
floor_t     = 2.0;
lid_t       = 2.0;
fit         = 0.4;          // clearance around each board in its nest
part_gap    = 5.0;          // space between adjacent boards
edge_clear  = 1.5;          // gap from cavity wall to outermost board

// ---- Boards: [length_x, width_y, pcb_thickness, components_above] ----------
// RAK19007 base + RAK4631 core: 60 x 30 confirmed; core+USB stack height VERIFY.
rak     = [60.0, 30.0, 1.6, 12.0];
// Carrier protoboard (you build this): default a half-size perfboard. Set to
// whatever you use. Must be big enough for XIAO (21x18) + SD breakout + 2 load
// switches + wiring. Component height ~ XIAO 3.5 + SD breakout/headers.
carrier = [50.0, 25.0, 1.6, 12.0];
// LiPo bay: 1000-2000 mAh cell. VERIFY to your battery (these get large!).
batt    = [55.0, 38.0, 9.0];

// ---- Nest mounting ---------------------------------------------------------
standoff_h   = 4.0;         // board floats this high (room for pins/solder underneath)
support_od   = 4.0;         // support-pillar diameter
support_inset = 4.0;        // pillar inset from each board corner
locator      = 3.0;         // corner-locator post footprint (mm square)
capture      = 1.5;         // how far locators rise above the PCB top to box it in

// ---- Ports (VERIFY edge + offset against your RAK) -------------------------
// USB-C sits on a 30 mm SHORT end of the RAK -> a wall running along Y. Default:
// left wall (-X), centred on the RAK's width.
usbc_w = 10.0; usbc_h = 4.0;
antenna_d = 7.0;            // SMA bulkhead hole (IPEX->SMA pigtail from the core)

// ---- Lid fixing ------------------------------------------------------------
lid_screw_d = 2.6;          // M2.5 clearance through the lid
boss_od     = 7.0;          // corner screw boss in the tray
boss_pilot  = 2.2;          // M2.5 self-tap pilot in the boss
lid_lip     = 3.0;

// ============================================================================
// Layout: RAK (front), carrier (middle), battery (back) along +Y
// ============================================================================
inner_l = max(rak[0], carrier[0], batt[0]) + 2*edge_clear;
inner_w = rak[1] + part_gap + carrier[1] + part_gap + batt[1] + 2*edge_clear;
board_stack_h = standoff_h + max(rak[2]+rak[3], carrier[2]+carrier[3]) + 2.0;
inner_h = max(board_stack_h, batt[2] + 1.0);

outer_l = inner_l + 2*wall;
outer_w = inner_w + 2*wall;
outer_h = floor_t + inner_h;

// board origins (front-left corner, in inner coords)
function centre_x(l) = edge_clear + (inner_l - 2*edge_clear - l)/2;
rak_o     = [centre_x(rak[0]),     edge_clear];
carrier_o = [centre_x(carrier[0]), edge_clear + rak[1] + part_gap];
batt_o    = [centre_x(batt[0]),    edge_clear + rak[1] + part_gap + carrier[1] + part_gap];

// ============================================================================
// Modules
// ============================================================================
// Support pillar (under the board) at absolute inner coords.
module pillar(x, y) {
    translate([wall+x, wall+y, floor_t]) cylinder(h = standoff_h, d = support_od);
}

// One corner-locator post just OUTSIDE a board corner; its inner corner touches
// the board corner so the board nests against it. sx/sy = +/-1 corner direction.
module locator_post(bx, by, h) {
    translate([wall+bx, wall+by, floor_t]) cube([locator, locator, h]);
}

// Nest a board: 4 support pillars + 4 corner locators. board = [l,w,pcb,comp].
module nest(o, board) {
    l = board[0]; w = board[1]; pcb = board[2];
    h = standoff_h + pcb + capture;
    // support pillars (inset corners)
    for (dx = [support_inset, l - support_inset])
        for (dy = [support_inset, w - support_inset])
            pillar(o[0] + dx, o[1] + dy);
    // corner locators, placed diagonally outside each corner
    locator_post(o[0] - locator,     o[1] - locator,     h); // BL
    locator_post(o[0] + l,           o[1] - locator,     h); // BR
    locator_post(o[0] - locator,     o[1] + w,           h); // TL
    locator_post(o[0] + l,           o[1] + w,           h); // TR
}

module corner_boss(x, y) {
    translate([x, y, floor_t]) difference() {
        cylinder(h = inner_h, d = boss_od);
        translate([0,0, inner_h - 8]) cylinder(h = 8.1, d = boss_pilot);
    }
}

// ============================================================================
// Tray
// ============================================================================
module tray() {
    difference() {
        cube([outer_l, outer_w, outer_h]);
        translate([wall, wall, floor_t]) cube([inner_l, inner_w, inner_h + 1]);
        // USB-C: left wall (-X), at the RAK, centred on RAK width.
        translate([-0.1, wall + rak_o[1] + rak[1]/2 - usbc_w/2, floor_t + standoff_h + rak[2]])
            cube([wall + 0.2, usbc_w, usbc_h]);
        // Antenna: right wall (+X) near the RAK core. VERIFY position.
        translate([outer_l - wall - 0.1, wall + rak_o[1] + rak[1]/2, floor_t + standoff_h + 6])
            rotate([0,90,0]) cylinder(h = wall + 0.2, d = antenna_d);
    }
    nest(rak_o, rak);
    nest(carrier_o, carrier);
    // battery bay is just reserved empty volume (held by foam/strap); no nest.
    // lid screw bosses in the four corners
    corner_boss(wall + boss_od/2,            wall + boss_od/2);
    corner_boss(outer_l - wall - boss_od/2,  wall + boss_od/2);
    corner_boss(wall + boss_od/2,            outer_w - wall - boss_od/2);
    corner_boss(outer_l - wall - boss_od/2,  outer_w - wall - boss_od/2);
}

// ============================================================================
// Lid
// ============================================================================
module lid() {
    difference() {
        union() {
            cube([outer_l, outer_w, lid_t]);
            translate([wall + fit, wall + fit, -lid_lip])
                cube([inner_l - 2*fit, inner_w - 2*fit, lid_lip]);
        }
        for (cx = [wall + boss_od/2, outer_l - wall - boss_od/2])
            for (cy = [wall + boss_od/2, outer_w - wall - boss_od/2])
                translate([cx, cy, -lid_lip - 0.1])
                    cylinder(h = lid_t + lid_lip + 0.2, d = lid_screw_d);
        // vent slots over the boards
        for (i = [-3:3])
            translate([outer_l/2 + i*5 - 1, outer_w*0.25, -0.1])
                cube([2, outer_w*0.45, lid_t + 0.2]);
    }
}

// ============================================================================
if (SHOW == "tray" || SHOW == "both") tray();
if (SHOW == "lid"  || SHOW == "both")
    translate([0, 0, SHOW == "both" ? outer_h + 15 : 0]) lid();

echo(str("Outer footprint: ", outer_l, " x ", outer_w, " x ", outer_h, " mm"));
