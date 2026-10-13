# KlipperXL-Plus release config layout (agreed with owner 2026-10-04)

## Goal
- Any XL, 1-5 tools: the installer generates config only for the tools it detects.
- Updates (KlipperXL-Plus and Klipper) never touch a printer's calibrations.
- No owner calibration values ship. (Released KlipperXL shipped the owner's
  `variables.cfg` and its installer `cp`'d it and `printer.cfg` over the user's -
  structurally impossible here.)

## Files
| File | Owner | Holds |
|---|---|---|
| `kxlplus/xl_base.cfg` | installer (replaced on update) | everything shared: XLBuddy mcu, steppers, homing, bed mesh, LEDs, bridge, probe router, macros |
| `kxlplus/tools/tN.cfg` (detected tools only) | installer | everything for one tool: bridge/mcu, extruder, fans, light, TMC, tool filament sensor, dock sensors, loadcell probe |
| `printer.cfg` | the user (created once, never rewritten) | `[include ...]` lines, user tweaks, seeded required values, Klipper's `#*#` SAVE_CONFIG block = all calibrations |
| `variables.cfg` | runtime | runtime state only (melt flags etc.), never calibrations |

## Rules
1. Installer files never set a per-printer calibration option.  Klipper enforces
   it: SAVE_CONFIG refuses an option set in an included file
   (`configfile.py _disallow_include_conflicts`: "conflicts with included value"),
   while options in the main printer.cfg are commented out and saved
   (`_strip_duplicates`).
2. Calibration options are optional in the modules, with a safe "not calibrated"
   behaviour, and saved with SAVE_CONFIG (`configfile.set`).
3. Options Klipper REQUIRES (heater `pid_Kp/Ki/Kd`) and Prusa constants a user may
   recalibrate (`counts_per_gram` 52.083) are seeded ONCE into the user's
   printer.cfg when the installer creates it; SAVE_CONFIG owns them afterwards.
4. Never hand-edit the `#*#` block; migrations go through Klipper's own SAVE_CONFIG.

## The owner's values never ship (owner 2026-10-06)
"my saved values dont need to go out on github version as they will have to
run their own for their printer".  The public release carries NO value
measured on the owner's XL - every printer calibrates itself (calibration
wizards, PA_TUNE_Tn); the installer seeds only Prusa's own defaults (PID
14/1/100, counts_per_gram 52.083) and the released KlipperXL's generic
starting points (pressure_advance 0.025, stock Prusa XL input shaper mzv
35.8 / 35.4).  So the public release
must NOT include:
- `owner/` - copies of the owner's printer.cfg (his records);
- `config/test-M*.cfg` - bring-up test configs with his loadcell references;
- `firmware/xlbuddy/FACTS.md` - the engineering log, full of his measurements;
- `experiments/` - test-only diagnostics.
Before any public push: grep the release files for measured per-printer
numbers (PA, PID, sensor cal, loadcell references, tool offsets, input
shaper, z_adjust). Personal hardware mods never ship either - only empty
hooks (e.g. [gcode_macro _KXL_NOZZLE_CLEAN]) do.

## Calibration options (checked in code 2026-10-04)
| Option | Section | Optional | Saved by |
|---|---|---|---|
| `cal` | `[xl_tool_fsensor Tn]` | yes | CALIBRATE_FILAMENT_SENSOR (to write) |
| `cal_tN` | `[xl_side_sensors]` | yes | CALIBRATE_SIDE_SENSOR (to write) |
| `offset_tN`, `z_adjust_tN`, `z_adjust_single_tN` | `[xl_toolchanger]` | yes (0) | CALIBRATE_TOOLS / Z adjust (to wire to SAVE_CONFIG) |
| `reference_tare_counts`, `counts_per_gram` | `[xl_load_cell_probe Tn]` | yes | Klipper load_cell.py calibration (configfile.set on the section name) |
| `pressure_advance` | `[extruderN]` | seeded 0.025 (released starting value) | PA_TUNE_Tn -> SAVE_TOOL_PA -> SAVE_CONFIG |
| `shaper_type_x/y`, `shaper_freq_x/y` | `[input_shaper]` | seeded stock Prusa XL mzv 35.8 / 35.4 (released, shipped enabled) | SHAPER_CALIBRATE -> SAVE_CONFIG |
| `control`, `pid_Kp/Ki/Kd` | `[extruderN]` | NO (required) | PID_CALIBRATE saves all four; seeded in printer.cfg (rule 3). `control` in the tool include made SAVE_CONFIG refuse ("option 'control' conflicts with included value", 2026-10-04) - it is seeded too |

## Steps (each tested on the printer, one change at a time)
1. Per-tool template carries every per-tool section; `xl_base.cfg` carries the rest;
   no calibration options in either.  Owner's printer.cfg rebuilt from includes +
   seeded values + his calibrations via SAVE_CONFIG.
2. Calibration commands that SAVE_CONFIG (filament, side, offsets/Z adjust, PA).
3. Safe uncalibrated behaviour verified per module.
4. Generator + installer config step on top.
