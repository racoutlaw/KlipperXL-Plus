<p align="center">
  <img src="docs/images/KlipperXL-Plus-logo.png" alt="KlipperXL-Plus" width="600">
</p>

# KlipperXL-Plus

**Klipper on every tool of the Prusa XL.** The XLBuddy board runs Klipper as
the bridge to the tools, **every tool (1-5) runs Klipper itself**, and the
modular bed keeps its stock Prusa firmware. It installs from a ready-made SD
card image, with a step-by-step installer page in your browser - **no
terminal, no commands to type**: setup, firmware flashing and calibration are
all guided, and updates come through Mainsail.

KlipperXL-Plus is the next step after
[KlipperXL](https://github.com/racoutlaw/KlipperXL): **everything KlipperXL
does, plus** Klipper firmware on the tools themselves, a one-card install,
guided calibration wizards, and the tool changer and filament handling done
the way stock Prusa firmware does them.

> ⚠️ **Read this before you start - the appendix.** The Prusa bootloader
> verifies firmware signatures and will reject unsigned Klipper firmware
> unless the **appendix** (safety seal) on the XLBuddy is broken. Breaking it
> is a **physical, irreversible modification to your mainboard** - it cannot
> be undone, and it is the same requirement as for any custom firmware on the
> XL. Everything else here is reversible: you can flash stock Prusa firmware
> back at any time and the printer returns to normal. The appendix is the one
> step that does not come back.

---

## Everything KlipperXL does

- **1 to 5 tools** - tool changes with Prusa's own dock moves (pick, park,
  wiggle retry), `T0`-`T4` from the slicer
- **Tool offset calibration** on Prusa's calibration pin, with stock G425's
  checks (a bad touch is re-probed or refused, never saved)
- **Dock position calibration** - each dock's real position measured and saved
- **Multi-color printing** with a wipe tower, and **spool join**: after a
  runout, carry on with another tool loaded with the same filament
- **Loadcell Z homing and bed mesh** with every tool's own loadcell; adaptive
  mesh (only the print area); a single-tool print homes Z with the tool that
  prints
- **Modular bed**: 16 zones, heats only under the print with Prusa's gradient
  on the zones around it; `BED_STATUS`, `SET_BED_AREA`; the **bedlet grid**
  panel - the 16 zone temperatures live in Mainsail
- **Automatic per-tool pressure advance**, measured with each tool's loadcell
  (CNC Kitchen's method) - about 6 minutes per tool, no test prints
- **Input shaper**, **Z alignment** of the two Z motors (`PRUSA_Z_LEVEL`),
  **per-tool Z fine-tune** with Mainsail's own Z offset buttons
- **Filament**: autoload, side + tool sensors, runout pause, Load / Unload
  buttons (`FILAMENT_LOAD` / `FILAMENT_UNLOAD`)
- **Print fan follows the tool**; every tool heats on its own (`M104 T<n>`)
- **Nozzle lights** (`NOZZLE_LEDS`), **side LED strip** status effects
  (optional, with the klipper-led_effect plugin), the **tool buttons**
  (up = retract, down = extrude)
- **Mid-print guards**: calibrations and tests refuse to run during a print
- An **OrcaSlicer** setup guide

## New in KlipperXL-Plus

### Klipper on every tool
KlipperXL kept stock Prusa firmware on the tools and talked to them over
MODBUS. In KlipperXL-Plus **each Dwarf runs Klipper firmware** - every tool is
its own Klipper board with its heater, extruder, loadcell, sensors, fan and
lights - and the XLBuddy's Klipper firmware is the bridge to them on the
printer's RS485 bus. Each tool is a normal Klipper extruder (`[extruderN]`),
so Klipper's own PID, pressure advance and heater handling work per tool.

### Install from one SD card - no command line
- **Raspberry Pi Imager** writes the KlipperXL-Plus image (MainsailOS +
  KlipperXL-Plus), with your Wi-Fi and user set in Imager
- the Pi boots into the **installer page** (`http://<your Pi>:8099`): it
  builds the firmware on the Pi from current Klipper, takes you through the
  XLBuddy flash with a USB stick, then **flashes every tool over the bus**
  (each tool's firmware backed up first, every flash read back and checked)
  and writes the configs for exactly the tools it found
- **updates from Mainsail's update list**; at every start Klipper checks that
  each board runs the firmware this version needs and says so in plain words
  if not
- already on MainsailOS? One line puts the installer page on the Pi

### Guided calibration wizards
**Macros → CALIBRATION_WIZARDS** in Mainsail, Fluidd or KlipperScreen: step
by step dialogs, each saving into your own `printer.cfg`:

| Wizard | What it does |
|---|---|
| Loadcell reference | every tool's loadcell zero, nozzle hot and free |
| Heater PID | every tool, each one starting cold |
| Filament sensors | each tool's side sensor and tool sensor, as stock |
| Z alignment | the two Z motors against the top frame, then home all |
| Dock positions | one dock or all - released KlipperXL's dock calibration as a guided dialog |
| Tool offsets | all tools on the calibration pin against T0 |
| Input shaper | X then Y with T0's accelerometer; you see the result and choose to save |
| Pressure advance | every tool, filament loaded; Save, a finer sweep, or Discard |

⚠ **Pick or park problems? Run Dock positions first** - every other wizard
picks and parks tools.

### Filament handling like stock Prusa
- load, unload and autoload go to the **front of the bed** first, where the
  purge can be cleaned off
- **end of print**: the filament is pulled back out of the melt zone, and
  given back on that tool's **first extrusion** of the next print (as stock's
  planner does) - no dry first perimeters, no ooze while it sits
- a hot unload ends as stock does: *"pull the filament out of the
  extruder"*; a filament already out of the nozzle unloads cold
- stock **`M600`** filament change and **`M591`** runout on / off
- **Mainsail's filament sensor rows**: every tool's side sensor and tool
  sensor, *Detected* / *Empty*

### Tool changer like stock Prusa
- **follows a tool you take off or put on by hand** (the dock sensors) - and
  outside a print the next command knows which tool is really on the carriage
- **refuses to pick a tool that is not in its dock** - nothing moves
- **tool status LEDs** in stock's colours: picked, docked, off its dock
- homing **bumps the right edge to line up the tool locks** (stock G28); an
  X/Y-only home leaves Z un-homed when its height is unknown, as stock
- pick and park use **each dock's own calibrated X and Y**

### Accuracy
- **pin touches use stock's loadcell filter** (a 0.8-12.8 Hz band-pass) - the
  motion noise that tripped early touches is filtered out
- **motor drivers set exactly as stock**: currents, microsteps, chopper, and
  stock's StallGuard pair for X/Y homing
- the bed height is **kept across a Klipper restart** (RESTART, SAVE_CONFIG):
  the next X/Y home uses the real height instead of dropping the bed 10 mm -
  the wizards restart Klipper after every tool, so the bed no longer walks
  down tool by tool

### More
- the **bedlet grid is set up as a camera** ("Modular Bed") by the installer
- the tool buttons are watched by each tool itself - holding one never holds
  up the bus
- KlipperScreen: Load / Unload buttons, and its Cooldown turns the modular bed
  off too

## Requirements

| | |
|---|---|
| Printer | Prusa XL (XLBuddy), 1 to 5 tools |
| Appendix | **broken** on the XLBuddy - permanent (see the warning above) |
| Host | Raspberry Pi 4 or 5 (64-bit), USB to the XLBuddy - tested on a Pi 4 |
| SD card | 16 GB or more |
| Flashing | a FAT32 USB stick (the XLBuddy firmware goes onto the printer from it) |
| Calibration | Prusa's calibration pin and a brass brush (tool offsets) |

## Install

1. Break the appendix (see above).
2. Flash the KlipperXL-Plus image with **Raspberry Pi Imager**, with your
   Wi-Fi, user and password set in Imager.
3. Boot the Pi, open `http://<your Pi>:8099`, and follow the installer page.
4. Calibrate: **Macros → CALIBRATION_WIZARDS**.

Step by step, with pictures: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Key commands

| Command | What it does |
|---|---|
| `T0` - `T4` | change tool: park, pick, the tool's offset, PA and print fan |
| `PICK_TOOL T=` / `PARK_TOOL` | pick / park a tool (homes X/Y first if needed) |
| `G28` | home all - X/Y, then Z with T0's loadcell |
| `CALIBRATION_WIZARDS` | the guided calibrations |
| `CALIBRATE_TOOL_OFFSETS` | tool offsets on the calibration pin |
| `DOCK_CAL [DOCK=n]` | dock position calibration; `DOCK_POSITIONS` lists them |
| `PRUSA_Z_LEVEL` | Z alignment (loud - the bed goes up into the frame) |
| `PA_TUNE_T0` - `PA_TUNE_T4` | measure one tool's pressure advance, end to end |
| `GET_TOOL_PA` / `SET_TOOL_PA TOOL= K=` / `SAVE_TOOL_PA TOOL= K=` | per-tool pressure advance |
| `GET_TOOL_Z_OFFSETS` / `SET_TOOL_Z_OFFSET` / `SAVE_TOOL_Z_OFFSET` | per-tool Z fine-tune |
| `FILAMENT_LOAD TOOL= TEMP=` / `FILAMENT_UNLOAD TOOL= TEMP=` | Prusa's M701 / M702 |
| `FILAMENT_SENSORS` | every tool's sensors |
| `M600` / `M591 S0`, `M591 S1` | filament change / runout off, on |
| `SET_AUTOLOAD` | autoload on / off |
| `SPOOL_JOIN TO=` / `SPOOL_JOIN_STATUS` / `SPOOL_JOIN_RESET` | carry on with another tool after a runout |
| `BED_STATUS` | the 16 bed zones |
| `SET_BED_AREA X0= Y0= X1= Y1=` / `CLEAR_BED_AREA` | heat only an area of the bed / the whole bed |
| `NOZZLE_LEDS STATE=ON\|OFF\|ALWAYS [TOOL=]` | nozzle lights |
| `TURN_OFF_HEATERS` | every heater off - the modular bed too |
| `QUERY_DOCK` / `QUERY_SIDE_SENSORS` / `QUERY_TOOL_FSENSOR` | raw sensor readings |

## Per-tool Z fine-tune

First-layer height is tuned **per tool** in Klipper, on top of the tool
offsets from the pin:

1. Start a print with the tool and babystep with Mainsail's **Z offset**
   buttons until the first layer is right.
2. Press **Save** in Mainsail's Z offset panel - it saves to the tool that is
   printing.
3. **SAVE_CONFIG** writes it to `printer.cfg`.

A multi-tool print and a single-tool print each have their own value per
tool (`z_adjust_tN` / `z_adjust_single_tN` in `[xl_toolchanger]`), because
Z is homed with T0 in one and with the printing tool in the other. Range
-0.3 to +0.5 mm. **Leave the Z offset at 0 in the slicer.**

## Per-tool pressure advance

Every tool keeps **its own** pressure advance - the same spool can measure
quite differently from one tool to the next. Load filament, run the
**Pressure advance** wizard (or `PA_TUNE_T1`), about 6 minutes per tool: it
heats, extrudes into free air while the loadcell records the back pressure,
analyses it and shows K. Saved as `pressure_advance` in that tool's
`[extruderN]`. **Leave pressure advance out of the slicer** - Klipper applies
the tool's own value. Re-run it when you change material.

## Slicer

OrcaSlicer: **[docs/SLICER_SETUP.md](docs/SLICER_SETUP.md)** - one
`START_PRINT` line for the start G-code, an empty tool change box (KlipperXL-Plus's
`T<n>` does the park and pick), Z offset 0 and no pressure advance in the
slicer.

## Updating

KlipperXL-Plus is in **Mainsail's update list** next to Klipper and Mainsail.
An update that also changes configs or board firmware is finished by the
installer page - Mainsail says so, and the page is started from Mainsail's
power menu → Service Control → KlipperXL-Plus-installer. It never overwrites
your `printer.cfg` or your calibrations. Details:
[docs/INSTALL.md](docs/INSTALL.md#7-updating).

## Going back to stock

**Prusa's factory bootloaders stay untouched** - the XLBuddy's
(KlipperXL-Plus is installed after it, at 0x08020200) and every tool's (each
tool is flashed through its own Prusa bootloader). So going back is one file:
download the current XL firmware from Prusa
([help.prusa3d.com/downloads/xl](https://help.prusa3d.com/downloads/xl)), put
the `.bbf` on a FAT32 USB stick and flash it like the XLBuddy step of the
install. The stock firmware re-flashes the tools with Prusa's own firmware by
itself, and the printer is back to normal - only the appendix stays broken.

## Safety notes

- Calibrations and tests **refuse to run during a print** (a paused print
  too): the wizards, `CALIBRATE_INPUT_SHAPER`, `TEST_RESONANCES_X` / `_Y`,
  `CALIBRATE_PA` / `CALIBRATE_PA_FINE` and the `PA_TUNE_Tn` that use them,
  `DOCK_CAL` / `DOCK_CALIBRATE`, `PRUSA_Z_LEVEL` / `STALLGUARD_Z_ALIGN`, and
  `PARK_TOOL`. If you add your own macro that homes, probes or changes tools,
  start it with `_ASSERT_NOT_PRINTING WHAT=<YOUR_MACRO>`.
- **Z+ = bed down (safe), Z- = bed up.**
- Tool offsets and dock positions are per printer - calibrate your own.
- KlipperXL-Plus cannot resume a print after a power loss - put the printer
  and the Pi on a UPS.

## Documentation

| | |
|---|---|
| [Installation](docs/INSTALL.md) | step by step, with pictures |
| [Calibration wizards](docs/CALIBRATION_WIZARDS.md) | every wizard, and what it is based on |
| [Slicer setup](docs/SLICER_SETUP.md) | OrcaSlicer |
| [Config layout](docs/RELEASE_CONFIG.md) | what the installer owns and what is yours |
| [Changelog](CHANGELOG.md) | what changed in each version |

## Credits

Built on [Klipper](https://github.com/Klipper3d/klipper),
[Moonraker](https://github.com/Arksine/moonraker),
[Mainsail](https://github.com/mainsail-crew/mainsail) and
[MainsailOS](https://github.com/mainsail-crew/MainsailOS); KlipperScreen is
installed when you choose it, and the LED effects use
[klipper-led_effect](https://github.com/julianschill/klipper-led_effect).
Printer behaviour and constants follow Prusa Research's
[Prusa-Firmware-Buddy](https://github.com/prusa3d/Prusa-Firmware-Buddy). The
pressure advance analysis is CNC Kitchen's
[PrusaPATuner](https://github.com/CNCKitchen/PrusaPATuner) (Stefan Hermann,
AGPL-3.0, in [pa_analysis/](pa_analysis/), run as a separate program).
Successor to [KlipperXL](https://github.com/racoutlaw/KlipperXL). Logo and
icon by MakerViking.

KlipperXL-Plus is an independent project, not affiliated with or endorsed by
Prusa Research.

## License

Copyright (C) 2026 Richard Crook

GNU General Public License v3 or later - see [LICENSE](LICENSE). The pressure
advance analyser in `pa_analysis/prusa_pa_tuner/` keeps its own license
(AGPL-3.0 or later).

## Disclaimer

THIS SOFTWARE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. YOU ARE
MODIFYING YOUR PRINTER'S FIRMWARE AND ACCEPT ALL RISKS OF DOING SO. THE
AUTHOR IS NOT RESPONSIBLE FOR ANY DAMAGE TO YOUR PRINTER, PROPERTY OR PERSON.
THIS PROJECT REPLACES STOCK FIRMWARE AND MAY VOID YOUR WARRANTY. USE AT YOUR
OWN RISK.
