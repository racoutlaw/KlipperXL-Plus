# KlipperXL-Plus - OrcaSlicer setup

KlipperXL-Plus runs Klipper on the XLBuddy **and** on every tool, so the slicer must
send Klipper-style G-code, not Prusa firmware G-code. Stock OrcaSlicer's Prusa XL
profiles are written for Prusa firmware (`gcode_flavor: marlin2`) and send commands
Klipper does not have (`M862.x`, `M555`, `M217`, `P0`, `G29`, `M601`, ...).

**Easiest:** start from the released KlipperXL profile (`orcaslicer/KlipperXL.json`,
imported on top of OrcaSlicer's *Generic Tool Changer*): every box below already
matches. If you start from a stock Prusa XL profile instead, change each box as
listed here.

## Printer settings

| Setting (Printer > Basic information / Extruder) | Value |
|---|---|
| G-code flavor | **Klipper** |
| Use relative E distances | **on** (START_PRINT also sets `M83` itself) |
| Z offset | **0** - first-layer Z is tuned per tool in Klipper (babystep + Save) |
| Pressure advance | **leave it out of the slicer** - each tool keeps its own in Klipper (`SAVE_TOOL_PA`) |

## Machine G-code - every box

| Box | Stock Orca Prusa XL sends | Do this | KlipperXL-Plus content |
|---|---|---|---|
| **Machine start G-code** | ~11 500 chars of Prusa G-code: `M862.3/.5/.6/.1`, `M115 U..`, `M555`, `M217`, heat + `G28 XY` / `G28 Z` / `G29`, purge lines | **Delete everything**, paste the one line on the right | see [Start G-code](#start-g-code) |
| **Machine end G-code** | `G4`, Z moves, `P0 S1` (park), `M104 Tn S0`, `M140 S0`, `M107`, `M221 S100`, `M84`, `M77` | **Delete everything** | `END_PRINT` |
| **Printing by object G-code** | (empty) | optional | `;BETWEEN_OBJECTS` / `G92 E0` (released profile) |
| **Before layer change G-code** | `;BEFORE_LAYER_CHANGE`, `G92 E0.0`, `;[layer_z]` | **keep, add one line** | `;BEFORE_LAYER_CHANGE`<br>`;[layer_z]`<br>`G92 E0`<br>`_ON_LAYER_CHANGE LAYER={layer_num + 1}` |
| **Layer change G-code** (after) | `;AFTER_LAYER_CHANGE`, `;[layer_z]` | keep as is | `;AFTER_LAYER_CHANGE`<br>`;[layer_z]` |
| **Timelapse G-code** | (empty) | only if you use moonraker-timelapse | `TIMELAPSE_TAKE_FRAME` (needs `[include timelapse.cfg]` in your printer.cfg) |
| **Change filament G-code** (tool change) | Prusa script with `P0 S1 L2 D0`, `M109 Sn Tn`, `Tn S1 L0 D0` | **Delete everything** - leave the box **empty** | *(empty: Orca sends `T<n>` itself; KlipperXL-Plus's `T<n>` parks, picks, applies the tool's offset, PA and print fan)* |
| **Change extrusion role G-code** | (empty) | leave empty | *(empty)* |
| **Pause G-code** | `M601` | **replace** | `PAUSE` |
| **Template custom G-code** | (empty) | leave empty | *(empty)* |

### Start G-code

One line (released KlipperXL profile, unchanged):

```
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single] TOOL=[initial_extruder] TOTAL_LAYER_COUNT={total_layer_count} X0={first_layer_print_min[0]} Y0={first_layer_print_min[1]} X1={first_layer_print_max[0]} Y1={first_layer_print_max[1]}
```

What START_PRINT does with it: heats the bed for the print area only
(`X0/Y0/X1/Y1`), homes XY, picks T0 and heats it to the probing temperature
(150 C), homes Z with T0's loadcell, meshes only the print area (4-12 points per
axis, ~30 mm apart), picks the print tool (`TOOL`), heats it to `EXTRUDER_TEMP`,
gives back any melt-zone retract from the last print, and draws a prime line.

Optional parameter: `TOOLS=<n>` = how many tools the job uses. With `TOOLS=1`
the job homes Z with its own printing tool instead of T0 (no tool offset involved).
Without it, the multi-tool path is used (T0 homes Z), which works for every job.

## What KlipperXL-Plus handles that the slicer sends during a print

| Command | KlipperXL-Plus |
|---|---|
| `T0`-`T4` | park + pick, tool offset, the tool's own pressure advance, print fan follows the tool. During a print, a tool **without a calibrated offset is refused** (it would print misaligned), except a single-tool job's own homing tool |
| `M104 Tn` / `M109 Tn` | heat tool n (each tool is its own Klipper extruder) |
| `M140` / `M190` | modular bed (adaptive area from START_PRINT) |
| `M106 S..` / `M107` | the print fan **of the picked tool**; fractional `S127.5` is fine; a fan problem never stops a print |
| `SET_PRINT_STATS_INFO`, `EXCLUDE_OBJECT_*`, `M73`, `M204`, `M220`/`M221` | Klipper built-ins |
| `PAUSE` / `RESUME` / `CANCEL_PRINT` | Mainsail's macros; pause = the released/stock moves (retract 1 mm, lift 20 mm, park front left X3 Y10; the bed mesh stays on), RESUME puts 1.5 mm back; after 30 min paused the nozzles turn off and RESUME re-heats them; cancel runs the released cancel steps (`_KXL_CANCEL_PRINT`: melt-zone retract, lift, park the tool, motors off) |
| filament runout | the picked tool's side sensor empty on 2 readings while printing (tool at 170 °C or more) → `PAUSE`; `M591 S0` / `M591 S1` turns it off / on (remembered) |

## Troubleshooting

- **"Unknown command" for `M862.3`, `M555`, `M217`, `P0`, `G29`, `M601`** - the start,
  end, tool-change or pause box still has stock Prusa G-code. Replace it as above.
- **Mesh probes the whole bed** - the start line is missing `X0/Y0/X1/Y1`.
- **"Tn has no calibrated tool offset - refusing to print with it"** - calibrate the
  tool offsets first; KlipperXL-Plus will not print a multi-tool job misaligned.
