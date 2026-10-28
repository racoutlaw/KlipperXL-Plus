# KlipperXL-Plus calibration wizards

A fresh install has nothing to carry over, so every calibration is a guided
dialog, like stock Prusa's wizards: **Macros -> CALIBRATION_WIZARDS** in
Mainsail, Fluidd or KlipperScreen. Each one saves into your own
`printer.cfg` (SAVE_CONFIG). Where the released KlipperXL already worked out
the Klipper way, that is the base; Prusa's wizards are the reference for the
prompts.

The dialogs are Klipper `action:prompt` messages (RESPOND TYPE=command
MSG="action:prompt_begin ..." / prompt_text / prompt_button / prompt_show /
prompt_end), which KlipperScreen, Mainsail and Fluidd all show.

Sources: Prusa-Firmware-Buddy 6.4.0; released KlipperXL @1b681b3.

## The wizards, in fresh-install order

| # | Calibration | Wizard | Based on |
|---|---|---|---|
| 1 | Loadcell reference, every tool (Z homing needs it) | `loadcell` | XL_LOAD_CELL_REFERENCE with the nozzle hot and clear of the bed |
| 2 | Heater PID, every tool (optional - Prusa defaults are seeded) | `pid` | each tool cold -> picked -> PID_CALIBRATE 215 -> parked -> saved |
| 3 | Filament sensors: side sensor + tool sensor, every tool | `fsensor` | Prusa's filament sensor wizard (section below) |
| 4 | Z alignment: the two Z motors against the top frame | `zalign` | released PRUSA_Z_LEVEL |
| 5 | Dock positions, one dock or all (stock: after Z alignment) | `docks` | released DOCK_CALIBRATE (section 1 below) |
| 6 | Tool offsets on the calibration pin (multi-tool) | `offsets` | released CALIBRATE_TOOL_OFFSETS (section below) |
| 7 | Input shaper: X then Y, T0's accelerometer | `shaper` | released macros + Klipper's lis3dh on the tool board |
| 8 | Pressure advance, every tool, filament loaded (~6 min each) | `pa` | released PA tuner + analyser (pa_analysis/) |

⚠ **Pick or park problems? Run 5. Dock positions first** - every other
wizard picks and parks tools (owner 10/13: the order stays stock's, with this
warning - the wizard menu says it too).

Then the first layer per tool: babystep while printing, then Save in
Mainsail (no wizard). Docks start at the stock Prusa XL positions until the
`docks` wizard (or the console `DOCK_CAL [DOCK=n]`) measures them. Not included:
gearbox alignment (done once on stock firmware, nothing in KlipperXL-Plus
changes it), phase stepping (Klipper has no equivalent), and Prusa's
fan/axis/heater self-tests.

## 1. Dock positions

### Base: released KlipperXL `DOCK_CALIBRATE [DOCK=n]` (puppy_bootloader.py @1b681b3 l.6181-6812)
Already interactive (DOCK_CAL_CONTINUE / DOCK_CAL_ABORT; abort reverts the
saved positions).  Docks: all booted unless DOCK=n.  Constants (= Prusa
toolchanger_utils.h): DOCK_FIRST_X 25, DOCK_OFFSET_X 82, DOCK_Y 455,
SAFE_Y_WITH_TOOL 360, SAFE_Y_WITHOUT_TOOL 425, PARK_X_OFFSET 1/2/3 = -10 /
-9 / +0.5, SLOW 50 mm/s, TRAVEL 200, PARKING_CURRENT 950 mA, NORMAL 650,
DOCK_CAL_UNLOCK_X -9.9 (Prusa X_UNLOCK_DISTANCE_MM = PICK_X_OFFSET_3),
DOCK_CAL_RETREAT_Y 95 (Prusa SAFE_Y_WITH_TOOL - DOCK_DEFAULT_Y_MM), unlock /
retreat at 30 mm/s (Prusa FORCE_MOVE_MM_S), DOCK_INVALID_OFFSET 6 mm
(Prusa DOCK_INVALID_OFFSET_MM), DOCK_CAL_VERIFY_CYCLES 3.

Per dock:
0. Light that dock's nozzle LED (always on).  If Z homed and < 100: Z100.
   If a tool is picked: XY motors off (M84 X Y), "Manually park the tool".
1. "Remove the dock pins from Dock n"
2. "Loosen BOTH screws on the right side of Dock n's pillar"
3. XY motors off - "Slide the carriage to align with Tn and lock it onto the
   tool in Dock n"
4. SET_KINEMATIC_POSITION X=25+82n Y=455 Z=100; check dock sensors (picked
   expected); "DO NOT MOVE THE CARRIAGE - Tighten the TOP dock screw ONLY"
5. Measure ("DO NOT TOUCH"): tool picked state; record stepper_x/stepper_y
   MCU positions; G91; G1 X-9.9 F1800; G1 Y-95 F1800; G90; check undocked;
   G28 X Y; record again; CoreXY: dA,dB = (pre-post)*step_dist,
   dx = (dA+dB)/2, dy = (dA-dB)/2; dock = home position + (dx,dy).
   |dock - default| > 6 mm -> FAIL ("Check dock alignment").  Y clamped to
   460.  Stage dock_pos_tN for SAVE_CONFIG.
6. "Tighten the BOTTOM dock screw on Dock n"
7. "Install and tighten dock pins on Dock n"
8. Park verification ("DO NOT TOUCH"): approach X+(-10) at Y360, Y=dock,
   950 mA, X+(-9) then X+0.5 @50, 650 mA, X,Y=dock @50, check parked, Y425,
   check not picked.  Fail -> stop.
9. 3x pick/park with the normal commands, checking the dock sensors each
   time; any failure reverts the positions.
10. Dock LED back to normal; next dock: carriage to X180 Y350, motors off.
End: list positions, "Run SAVE_CONFIG".

### KlipperXL-Plus changes (built 10/12, xl_toolchanger.py + xl_wizards `docks`)
Ported from the release @1e677ba (l.6361-7013) - its steps, texts, moves and
maths - owner 10/12: "just like the klipperxl version only changing what
needs to be changed".  Console: `DOCK_CAL [DOCK=n]` (released wrapper, in
xl_macros.cfg) or `DOCK_CALIBRATE`, then DOCK_CAL_CONTINUE / DOCK_CAL_ABORT;
`DOCK_POSITIONS` lists them.
- Wizard `docks`: each step's released text in a dialog, Continue =
  DOCK_CAL_CONTINUE, Cancel = DOCK_CAL_ABORT; at the end Save (SAVE_CONFIG)
  or Don't save.
- dock_pos_tN = x, y is read from [xl_toolchanger]; pick, park and the park
  test use each dock's own X and Y.
- Dock LED = nozzle light ALWAYS on that tool, every light back to its own
  setting at the end; tool picked state = the dock sensors (xl_dock_sensors).
- TMC enable / is_selected coils do not exist (Klipper Dwarfs).
- Z keeps its real height and is only marked homed if it was (SET_HOMED);
  the release set Z=100.
- Steps 3/4 hold the carriage with the motors (SET_STEPPER_ENABLE), as Prusa
  does (M18 S0 above) and the release's own comment meant to: a motor that
  is only switched on after the dock is tightened can snap the carriage up to
  2 full steps (0.4 mm) that the step count never sees.
- A dock is staged for SAVE_CONFIG once its pick/park checks pass, and a
  failed dock reverts only itself (the release staged at the measurement, so
  a dock that then failed was still saved by the next SAVE_CONFIG).

### Prusa dock wizard (selftest_dock*.cpp, gui/wizard/selftest_frame_dock.cpp)
Order: ask "needs calibration?" -> move away -> manual park 1/2/3 ("park
current tool manually: move the tool changing mechanism to the rear and
align it with pins" / "move it to the right, the tool will be locked in the
dock" / "can now move freely, move it a little bit to the front") ->
"The calibrated dock is illuminated ... Loosen and remove the dock pins." ->
"Loosen the two screws on the right side of the dock pillar (marked in
orange) using the uni-wrench." -> "Align the tool changing mechanism with the
tool and lock it by sliding both metal bars to the right." -> hold position ->
"Tighten the top dock screw at the right side of the pillar. Be careful in
next step the printer will be moving" -> measure ("Do not touch the
printer!") -> "Tighten only the bottom screw on the right side of the dock
pillar." -> "Install and tighten the dock pins. Be careful in next step the
printer will be moving." -> park test (to dock, unlock, check state, away,
check state) -> pick/park cycles -> "Dock successfully calibrated." / "Parking
test failed. Please try again."  Guide link prusa.io/dock-setup.
Measure (selftest_dock.cpp:194-260): same as the release (X_UNLOCK -9.9,
Y back to SAFE_Y_WITH_TOOL, G28 XY R0, M18 S0 so the motors hold while the
user tightens), out of bounds = fatal.

## 2. Tool offsets - calibration pin

### Base: released KlipperXL `cmd_CALIBRATE_TOOL_OFFSETS` (puppy_bootloader.py @1b681b3 l.1063-1717)
Parameters: PIN_X 180, PIN_Y 180, PIN_DIAMETER 6, PIN_HEIGHT 9, SPEED 3 (XY
probe mm/s), PROBE_SPEED (Z, probe default 1.0), TOOLS 0,1,2,3,4, SKIP_CLEAN 0,
CLEAN_TEMP 200, CAL_TEMP 70, CLEAN_TIME 30 s/tool, WAIT_TIME 30 s, DRY_RUN 0.
Max accel limited to 500 for the whole command.

Sequence:
1. Heaters off on all tools; any cooling fan from an aborted run forced off.
2. (unless SKIP_CLEAN) "INSTALL HEATBED SHEET" - WAIT_TIME pause.
3. Home all if not homed (G28 - sheet is on). Heat all tools to CLEAN_TEMP,
   wait each >= CLEAN_TEMP-5. "CLEAN ALL NOZZLES with brass brush, CLEAN
   PARKING PLATES". Each tool: T<n>, move X180 Y50 Z100 F6000, "CLEAN T<n>
   NOW" for CLEAN_TIME. Park.
4. Set all tools to CAL_TEMP now; any tool > CAL_TEMP+5 gets its print fan
   latched at 100 % (background cooling).
5. Pick T0 (if none picked), G28 (Z homed on the sheet via T0 loadcell), park
   (carriage to Y425).
6. "REMOVE the heatbed sheet, INSTALL the calibration pin at bed center" -
   WAIT_TIME pause.
7. Wait all tools within CAL_TEMP+-5 (fan on while > CAL_TEMP+5; fans
   released in try/finally on success or abort).
8. BED_MESH_CLEAR; remember current offsets (for the report / DRY_RUN);
   zero all offsets, SET_GCODE_OFFSET X0 Y0 Z0.
9. Per tool: T<n> (+0.5 s), verify active tool; Z to 50 @10; XY to pin @50;
   Z to safe_z = 9+5 = 14 @10; 3 phases:
   - phase XY hits [3, 3, 12], Z hits [1, 0, 20], clearance [8, 5, 1] mm
   - Z (if any): RAISE Z FIRST to pin_height+clearance @10 (the 2026-08-21
     lateral crash), then XY to current center @25; each sample: run_probe
     with travel clearance+5, retract to hit+clearance @10; average.
   - XY: probe Z = center Z - 1 (bore); for i in num_xy: angle 2*pi*i/n;
     _probe_xy_verified: up to 5 attempts x 2 samples, accept when both
     within 0.03 mm of their mean (else warn and use the last mean). Each
     sample _probe_xy_at_angle: ARC around the pin at start_radius = 3+5 = 8
     mm (10 deg steps, @25, Z untouched), assert probe Z, settle 0.5 s,
     tare, verify stable (< 50 % of threshold, re-tare up to 5x), probing
     move toward target = center - (3+2 = 5 mm) past the center along the
     bearing @SPEED with the XY loadcell endstop (40 g); warn if travel <
     0.1 mm; retract 5 mm radially @25. Between samples: back to the start
     point radially (no Z change - the nozzle cone makes Z error a radius
     error). NO Z retract between angles.
   - after the sweep: LIFT to center Z + clearance @10 (crash lesson).
   - circle fit: iterative least squares, 10 iterations, centroid start,
     correction damped x0.5 (release _fit_circle_center l.1024-1061).
   - final phase: deviation from mean radius > 0.2 mm -> WARNING.
   - store center; Z to 14 @10.
10. Offsets = T0 center - tool center; bounds X/Y +-1, Z -2..+1.45 ->
    WARNING only.
11. Park; heaters off; fans released.
12. "REMOVE the calibration pin, INSTALL the heatbed sheet" - WAIT_TIME pause.
13. Report MEASURED vs CURRENT Z. DRY_RUN: restore the old offsets, write
    nothing. Else apply + save (SAVE_CONFIG path).

run_probe (l.462-632): accel 500 (restored in finally), settle 0.2 s,
standalone = double tare (0.3 s between), threshold 125 g, target Z =
max(cur - travel(15), position_min), probing_move, retract min(hit +
sample_retract_dist 0.2, start).

### KlipperXL-Plus changes (only these)
- WAIT_TIME pauses -> prompt dialogs with [Continue] (owner: walk-through).
  Same steps, same order.
- XY touch detection: released = XLBuddy firmware loadcell XY endstop (MODBUS
  tare/probe_start xy=1, trsync).  KlipperXL-Plus = Klipper load_cell_probe on
  the Dwarf -> a Klipper-side XY probing move (endstop with the X/Y (A/B)
  steppers, 40 g lateral threshold - Prusa loadcell.hpp XY_PROBE_THRESHOLD 40,
  XY_PROBE_HYSTERESIS 20) must be designed and tested; motion/distances stay
  the released ones.
- SET_DWARF_TEMP / fan registers -> M104 Tn / Klipper fans (each tool is a
  Klipper extruder; print fan T<n>_print_fan, heatbreak fan is a heater_fan).
- Offsets saved as [xl_toolchanger] offset_tN via SAVE_CONFIG (release
  config layout, docs/RELEASE_CONFIG.md).

### KlipperXL-Plus build (10/08) - xl_tool_offsets.py + xl_wizards `offsets`
Owner 10/08: "keep all movements the same" - a straight port, no changes
to how it moves.  Checked by running the released functions (extracted
from puppy_bootloader.py @1b681b3) and the port against one recording
toolhead + fake pin: 1709 motion events (moves, touches, picks/parks,
G1/G28), every target, speed and the acceleration in force identical, 0
differences; offsets math identical.
- Accel as released: 500 from the start until the step-4 G28 (homing_override
  puts max_accel back), then 500 only inside each touch (Klipper's
  set_max_velocities instead of writing max_accel; restored on a stop).
- Z touch: the tool's Klipper load cell tare (TRIGGER_FORCE 125) + probing
  move to max(Z - travel, position_min) at 1.0 mm/s, trigger position as the
  result, lift 0.2 mm at 5 mm/s - the released run_probe, not Klipper's tap.
- XY touch: same trigger, TRIGGER_FORCE 40 (Klipper's trigger is abs_ge =
  released xy_mode=1).  For the move the trigger's trsync stops stepper_x /
  stepper_y instead of stepper_z (_xy_probing_move).  A load cell has one
  MCU_trigger_analog; a second dispatch can't share its command queue and
  X/Y left on it would fail every Z probe (homing.py check_no_movement).
  Uses Klipper internals TriggerDispatch._trsyncs / MCU_trsync._steppers and
  LoadCellProbingMove._pause_and_tare - re-check on every Klipper update.
  Startup log lists each tool's trsyncs (printer 10/08: dwarf_tN [] ; mcu
  [stepper_z]; XY motors stepper_x/stepper_y on mcu).
- Stability pre-check before an XY touch: mean force over 0.1 s < 20 g,
  re-tare up to 5x (released < 50 % of 40 g).
- Cooling fans: xl_toolchanger hold_fan (pick/park/M106 leave a held fan).
- Picks with XL_PICK (a leftover spool join would remap T<n>).
- CLEAN_TIME stays a timed 30 s per tool (released); only the three
  WAIT_TIME pauses became Continue dialogs.
- A failed probing step or a stop puts the previous offsets back (released
  left them zeroed until a restart).  Nothing is saved until the end; one
  SAVE_CONFIG writes [xl_toolchanger] offset_t0..tN.
- CALIBRATE_TOOL_OFFSETS [DRY_RUN=1] [TOOLS=] [SKIP_CLEAN=1] ... starts the
  same wizard with the released parameters; TOOLS must include T0.

### Prusa G425 notes - NOT applied unless the owner says so
(src/marlin_stubs/G425.cpp, src/common/selftest/selftest_tool_offsets*.cpp,
gui/wizard/selftest_frame_tool_offsets.cpp, Configuration_XL(_adv).h,
utils/center_approx.hpp)
- Constants identical to the release: bore 1, Z clearance 5/1, pin 6x6x9 at
  180,180, XY dists 6/7/8, probe 3 mm/s, interprobe 25, 5 tries, 0.03 mm,
  2 samples, 500 mm/s^2, settle 500 ms, min travel 0.1, max dev 0.2, temps
  clean 200 / cal 70, offset bounds X/Y +-1, Z -2/+1.45.
- Prusa probes TOWARD the pin center (release: 5 mm past it).
- Prusa discards a hit < 0.1 mm travel; redoes ONE sample per try; fatal
  error after 5 unstable tries; fatal if a hit is > 3 mm
  (PROBE_FAIL_THRESHOLD_MM) from the expected pin edge; fatal "pin not
  reached" if no hit.
- Prusa Z: first sample at center, the rest on a circle of r = 1.5 mm;
  clearance drops to 1 mm after the first sample.
- Prusa center fit: same iteration, NOT damped.
- Prusa: bounds violation = fatal; E steppers disabled, crash recovery and PA
  off during probing; soft endstops and bed leveling off.
- Prusa wizard: confirm start -> steppers off + heaters 0 + bed down 100 ->
  "Nozzle have to be perfectly clean ... Clean all nozzles, Clean parking
  plate, Press continue" with Heatup (200) / Cooldown buttons -> on Continue
  all tools 70 C + print & heatbreak fans 100 % on hot tools -> "Install
  sheet on heatbed" -> Z+30, T0, G28 O, park -> "- Remove heatbed sheet. -
  Install calibration pin." -> wait all at 70 -> G425 -> park tool + head ->
  "- Remove calibration pin. - Install sheet on heatbed."

## 3. Loadcell reference (per tool) - Klipper way (owner decision 10/04)
Exists: XL_LOAD_CELL_REFERENCE [TOOL] [TEMP=150] (xl_load_cell_probe):
refuses unless the nozzle is within 5 C of TEMP; averages counts;
set_calibration(counts_per_gram, avg) -> SAVE_CONFIG.  Wizard: per installed
tool: pick -> M109 150 -> prompt "nothing touching the nozzle" [Continue] ->
XL_LOAD_CELL_REFERENCE -> next; heaters off; SAVE_CONFIG.  Order: AFTER
docks (needs picks), BEFORE anything that probes (Z home, pin calibration).

## 4. Z alignment
### Base: released `PRUSA_Z_LEVEL` (printer.cfg @1b681b3 l.379-437; alias STALLGUARD_Z_ALIGN)
_ASSERT_NOT_PRINTING; SET_KINEMATIC_POSITION Z=0; G1 Z10 F900 (bed down);
G28 X Y; G0 X10 Y10 F6000; park a picked tool (SET_KINEMATIC_POSITION Z=100
first); save stepper_z run_current; SET_TMC_CURRENT stepper_z 0.7 (Prusa
Z_CURRENT 700, Configuration_XL_adv.h:1120); G4 P300; FORCE_MOVE stepper_z
DISTANCE=-400 VELOCITY=15 (ram the bed into the top frame); FORCE_MOVE
DISTANCE=5 VELOCITY=15 (back off, Prusa Z_CALIB_EXTRA_HIGHT 5); restore
current; G4 P300; SET_KINEMATIC_POSITION Z=5; G1 Z50 F900; "run G28 Z".
KlipperXL-Plus: same moves; T<n>/XL_PARK for the park; prompts "This rams the
bed into the top frame to align the Z motors - nothing on the bed"
[Continue], then G28 Z offered.  Checked 10/06: KlipperXL-Plus stepper_z
run_current/hold_current = 0.700 A = the released push current = Prusa
Z_CURRENT 700 (G162 keeps it) - so the push runs at full stock current.
Why it matters (owner 10/06): both Z motors share ONE driver; the side that
reaches the top first stalls and skips while the other must keep pushing to
its own limit - the current decides whether the lagging side gets there.
### Prusa G162 calib_Z (src/marlin_stubs/G162.cpp) - note only
G28 XY if needed, Z down 5, to X_MIN+2 Y_MIN+2, HOME Z UPWARD with stall
detection (+loadcell check if a tool is picked; fatal if sitting on the
nozzle), then move 5 mm past (Z_MIN_POS - 5) @15 mm/s and back; Z marked not
homed.

## 5. Heater PID (per tool)
Prusa has none (stock Dwarfs regulate themselves).  Owner procedure 10/04
(done by hand): each tool COLD, PICKED, PID_CALIBRATE HEATER=extruderN
TARGET=215, XL_PARK, SAVE_CONFIG after EACH tool (a restart loses pending
values).  Wizard: per installed tool: wait until cold (within 3 C of the
coolest tool, all reported - temps read 0.0 right after a restart), G28 X Y
if needed, T<n>, PID_CALIBRATE, XL_PARK, SAVE_CONFIG -> next.  control: pid
is seeded in printer.cfg (c9c2dcf) so SAVE_CONFIG accepts it.

## 7. Input shaper
### Base: released (printer.cfg @1b681b3 l.645-650, 971-998)
[resonance_tester] accel_chip "adxl345 dwarf_accel" (dwarf_accelerometer.py:
LIS2DH12 over MODBUS FIFO, 1344 Hz, +-2 g, axes map z,y,x),
probe_points 180,180,20, accel_per_hz 75, hz_per_sec 1.
CALIBRATE_INPUT_SHAPER: _ASSERT_NOT_PRINTING, G28 if not homed,
SHAPER_CALIBRATE.  TEST_RESONANCES_X/_Y likewise.  Stock XL defaults
(Prusa input_shaper_config.hpp XL branch): mzv 35.8 / 35.4.
### KlipperXL-Plus
The Dwarfs run Klipper: Klipper's own [lis3dh] driver (klippy/extras/
lis3dh.py, src/sensor_lis2dw.c, dev id 0x33 = LIS2DH12's WHO_AM_I) on the
picked tool's Dwarf, SPI.  Dwarf firmware already has CONFIG_WANT_LIS2DW=y
and SPI2 buses (spi2 = PB14,PB15,PB13 among others).  TODO before use: the
accelerometer SPI bus + CS pin from Prusa hwio_pindef_XL_dwarf.h; axes map
(release z,y,x); which tool carries it during the test (T0 picked, as the
release's probe point).  Same macros/prompts as released.

## 6. Filament sensors (side + tool)

Prusa (selftest_fsensor.cpp, gui/wizard/selftest_frame_fsensor.cpp), per
tool, both sensors together: pick tool -> "Is there any filament in the tool
or side filament sensors?" -> unload if yes -> empty calibration -> "Insert
the filament through the side filament sensor into the extruder until the
tool filament sensor detects the filament." (the XL's calibration never
turns the extruder) -> loaded calibration -> "Remove filament to finish."
Stock XL also blocks autoload while the Calibrations menu or the wizard is
open (filament_sensors_handler.cpp).

The wizard follows those steps, by hand like stock (10/05 the owner thought
a hand could not reach the tool sensor; 10/10 on his printer it did on T0
and T1 - the tool sensor sees the filament before the gears can grab it, so
an extruder turning there pulls nothing).  Like stock it waits on the
sensors, not on a button: "sees it" with only the empty reading known =
more than the stock span from empty (extruder 1000, side 310 -
filament_sensor_adc.cpp), then Record while it is held in, then it waits
until both sensors read empty with their new calibration (stock checks the
tool sensor here; the wizard checks both).  Autoload is held off during the
wizard.

## 8. Pressure advance (per tool)

Stock Prusa XL has no PA calibration (not in selftest_snake_config.hpp).
### Base: released "XLPATuner Road B" @1b681b3 - read in full
config/pa_tuner.cfg (CALIBRATE_PA, CALIBRATE_PA_FINE), config/pa_menu.cfg
(_PA_TUNE_RUN, PA_TUNE_T0..T4), klippy/dwarf_pa_tuner.py (recorder +
ANALYZE_PA), klippy/tool_pa.py (storage), docs/PRESSURE_ADVANCE.md,
pa_analysis/ (pa_analyze.py adapter + CNC Kitchen PrusaPATuner 0.1.0 vendored
unmodified, AGPL-3.0, run in its own venv: analysis.py, alignment.py,
gcode_gen.py, gcode_preamble.py).

PA_TUNE_Tn [TEMP=215] [FILAMENT=PLA] [SAVECONFIG=0]:
1. Refuse while printing OR paused (it homes Z = probes onto the part; cost a
   real 88-minute print).
2. CALIBRATE_PA TOOL=n TEMP (values ported from his stock PrusaPATuner gcode,
   PETG 240, verified line by line; range widened 08/16):
   - K 0.020..0.120 step 0.005 = 21 values x CYCLES 6.  CYCLES 6 is a measured
     floor: the analyser drops a K with < 4 included segments, inclusion runs
     67-85 %; CYCLES 4 rejected every K (live 08/15).
   - Flow is volumetric: slow 3.0 mm^3/s for 2.0 s, fast 14.0 mm^3/s for
     0.714286 s, first slow leg x5 (warm-up), 1.75 mm filament.  Derived:
     slow E2.4945 F30.00, fast E4.1575 F84.00, warm-up E12.4726 F6.00;
     X oscillates 30 <-> 31 (1 mm) so Klipper applies PA (composite XYE
     moves, F = XY speed, E slaved).  Accel 5000.
   - Order: SAVE_GCODE_STATE; G90; G28 COLD (a failure must not leave a hot
     nozzle) and BEFORE the pick (homing picks T0 for Z - pick-then-home would
     record T0's loadcell as Tn's K); T<n>; M400; G1 X30 Y30 Z80 F6000; M400;
     M104/M109 S{TEMP+10}; M83; SET_VELOCITY_LIMIT ACCEL=5000; G4 P500;
     START_PA_RECORDING; G4 P2000 (baseline); M104 S{TEMP}; Z marker G1 Z82
     F1800, G4 P100, G1 Z80 F1800; G4 P500 (= the analyser's 0.5 s pre-roll);
     per K: SET_PRESSURE_ADVANCE ADVANCE=K, M118 K=, 6 x (slow leg to X31,
     fast leg to X30); one trailing slow leg; M400; STOP_PA_RECORDING with
     every sweep parameter (written into the CSV header); SET_PRESSURE_ADVANCE
     ADVANCE=0; M104 S0; accel restored; RESTORE_GCODE_STATE.
   - By the macro's own formula: ~848 mm of filament (~2.5 g PLA) into free
     air at Z80, dripping on the cold bed; sweep ~6 min.  (pa_menu.cfg says
     "~500mm" and the doc "~4 g" - stale, from older grids.)
   - Extrusion guard: max_extrude_cross_section 4.0 (catches the prime-line
     bug, ~5.4 mm^2) is raised to 50 mm^2 on the active extruder for the
     recording only and restored in an idempotent teardown (sweep moves are
     6 / 10 / 30 mm^2).
3. Recorder (STOP_PA_RECORDING): rejects |raw| > 2^23 (first record after
   enable came back INT32_MIN); tare = mean of the first n/20 samples (max
   100); writes /tmp/pa_recording_T<n>_<stamp>.csv (+ /tmp/pa_recording.csv
   = latest): "# tool= dwarf= samples= elapsed= tare= scale=0.0192", one
   "# param NAME=VALUE" line per sweep parameter, "time_s,grams", rows
   time from the first sample, grams = (raw - tare) x 0.0192.  Reports rate,
   min/max grams, gaps > 100 ms.  Hard stop at 1800 s.
4. ANALYZE_PA TOOL SAVE=1 SAVECONFIG FILAMENT: runs ~/pa_analysis_env python
   ~/pa_analysis/pa_analyze.py <csv> as a detached process, polled every 1 s
   from a reactor timer (never blocks Klipper); reads /tmp/pa_result.json;
   prints bd_k_opt (headline) plus every fit with r^2 (>= 0.9 strong, < 0.2
   "IGNORE") and the per-segment K; no K -> "too few cycles"; SAVE ->
   SAVE_TOOL_PA TOOL K FILAMENT (live at once); SAVECONFIG=1 -> SAVE_CONFIG
   issued from inside the module (a macro line would run before the analysis
   ends); otherwise a boxed "ACTIVE NOW but NOT YET SAVED" reminder.
5. The analyser rebuilds the sweep timing from the CSV header
   (build_sweep(SweepParams)): 0.5 s pre-roll, K segments back to back, K[0]
   longer by the warm-up.  With no position streams it locates the sweep in
   the FORCE trace: rolling-std burst detector, then rising-edge cycle
   detection - which needs >= 50 units between the 10th and 90th percentile
   and the FAST leg reading HIGHER.  Per (K, cycle) step-response metrics;
   the first cycle of every K is excluded; segments < 40 Hz or with a dropout
   (> 50 ms gap AND a big force jump) near the transitions are excluded.
   Estimators: composite-cost argmin (bd_k_opt), signed fall/rise area zero
   crossings, integral area, phase lag, per-segment K median.  Estimators
   AGREEING is the quality signal; the optimum must lie INSIDE the swept range
   (released 08/16: with 0.02-0.07 one tool's bd_k_opt was clipped at the
   edge; 0.02-0.12 fixed it).
6. CALIBRATE_PA_FINE: step 0.002 x 10 cycles - narrow the range around the
   coarse K first (full range ~23 min).

### KlipperXL-Plus state (checked on the Pi 10/05)
- ~/pa_analysis + ~/pa_analysis_env survive (numpy 2.5.2, scipy 1.18.0); the
  analysis package is byte-identical to the release; pa_analyze.py is the
  pre-release copy (home path hard-coded, otherwise identical) - the installer
  ships the release one.
- Storage is DONE: each tool is its own Klipper extruder, so PA is per tool
  natively; SAVE_TOOL_PA / SET_TOOL_PA / GET_TOOL_PA / APPLY_TOOL_PA in
  xl_toolchanger write [extruderN] pressure_advance (+ pa_label_tN).  The
  owner's released per-tool values were migrated into HIS printer.cfg only;
  a fresh install starts at 0 and runs PA_TUNE_Tn.  Smooth time is Klipper's
  default in both.

### KlipperXL-Plus changes (only these)
- Recorder: the released one reads the XLBuddy MODBUS FIFO stream
  (loadcell_stream_*), which does not exist here.  KlipperXL-Plus = a client on
  the picked tool's Klipper load cell (xl_load_cell: the selected tool streams
  at 320 sps over the RS485 bridge; rows [print_time, grams, counts,
  tare_counts]).  It writes the SAME CSV, from the RAW counts with the
  release's formula ((counts - head tare) x 0.0192, independent of Klipper's
  tare), same header, files and out-of-range reject.
- Measure before trusting it: (a) delivered rate and gaps over a full sweep
  WITH extrusion running (release: 300 Hz, zero gaps in 12 min); (b) polarity
  - the fast plateau must read above the slow one, as the analyser assumes;
  if it does not, show the owner before changing anything.
- Extrusion guard relax/restore on the PICKED tool's extruder (each has its
  own max_extrude_cross_section 4.0).
- After the sweep the release set ADVANCE=0 and relied on its tool-change
  wrapper to re-apply the stored K at the next pick.  KlipperXL-Plus has no
  re-apply (nothing needs it), so the sweep restores that extruder's previous
  pressure_advance instead - otherwise a failed analysis would leave the tool
  at 0 until a restart.
- GET_TOOL_PA: print "-- (never tuned)" for 0.0 with no label, as released
  (0.0 is a legitimate K; a fresh install seeds 0).
- Wizard prompts around the same commands: pick tools -> "Filament loaded in
  Tn?" (load wizard if not) -> "Nothing on the bed - ~850 mm will be extruded
  in free air at Z80" [Start] -> sweep -> result: K + whether the estimators
  agree [Save] (SAVE_TOOL_PA, SAVE_CONFIG) / [Discard] / [Fine sweep around
  K] -> next tool.  (The release used Mainsail's parameter panel instead of a
  dialog; KlipperScreen needs the dialog.)

## 9. First layer / Z fine-tune (per tool)

Stock Prusa XL has no first-layer calibration (selftest_snake_config.hpp XL
list; the loadcell sets Z).  Released KlipperXL had no test print either -
README "Per-Tool Z Offset" workflow: CALIBRATE_TOOL_OFFSETS first; start a
first-layer test print with T0; adjust Mainsail's Z offset +/-; Save (saves
to the ACTIVE tool); SAVE_CONFIG; repeat per tool.  Bounds -0.3..+0.5 mm.
Slicer Z offset stays 0.
KlipperXL-Plus has it (xl_toolchanger, released behaviour): Z_OFFSET_APPLY_PROBE
(Mainsail/KlipperScreen Save), SAVE_TOOL_Z_OFFSET / SET_TOOL_Z_OFFSET /
GET_TOOL_Z_OFFSETS; two slots per tool - z_adjust_tN (multi-tool jobs) and
z_adjust_single_tN (single-tool jobs).  Every job homes Z with its first tool
(stock); until 10/24 multi-tool jobs homed with T0.  Used by the owner 10/05
(saved in his printer.cfg).
Owner decisions: 10/05 no built-in first-layer pattern ("slice your own", as
released); 10/06 NO wizard at all - "first layer is just something you can do
while printing, that's babystepping": +/- during the first layer, then Save
(Z_OFFSET_APPLY_PROBE -> the active tool's slot), then SAVE_CONFIG.
