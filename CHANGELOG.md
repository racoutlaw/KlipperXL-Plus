# Changelog

## Updates since v1.0.0 - 2026-10-15 (from Mainsail's update list)

**The modular bed in Mainsail and KlipperScreen.** The bed is now a normal
heated bed for Klipper's screens: a **Heater Bed** row with a target box in
Mainsail and in its presets, the bed on KlipperScreen with its number pad,
and `B:` in `M105`. The bed's own stock firmware still does the heating. The
separate "Modular Bed" temperature row is gone - it showed the same.

**Bed zones.** On the bedlet grid camera: tap bedlets, set a temperature and
press **Heat selected** - the chosen bedlets heat and their neighbours are
warmed part-way, as Prusa does around a print area; **Heat all** and **Off**
too, and each bedlet shows its own target. Console:
`SET_BED_ZONES ZONES=1,2,5 S=60` or `ZONES=ALL`. Not while printing: a
print's own area takes over, and the whole bed comes back after it.

**Start-up clock check.** Each tool's clock estimate now settles before the
next tool boots. The occasional "MCU 'dwarf_t3' configured for 56Mhz but
running at 55Mhz" warning at start-up came from that first estimate - the
tools' crystals were always right (56.000 MHz); none showed in the test
restarts after the change. Start-up takes a few seconds longer.

**Bedlet grid page** always loads fresh after an update (browsers re-check
it, and the camera's address is now `/bedlet_grid.html?v=2`); any spelling
of its address works.

**After this update** Mainsail asks you to finish it with the installer page
(it adds the bed's `[heater_bed]` section and the new grid page): power menu
(top right) → **Service Control** → **KlipperXL-Plus-installer** → start, then
open `http://<your Pi>:8099`.

## v1.0.0 - 2026-10-13

The first public release of KlipperXL-Plus: everything
[KlipperXL](https://github.com/racoutlaw/KlipperXL) does, with Klipper
firmware on the tools themselves. See the [README](README.md) for the full
list; the highlights:

**Klipper on every tool.** Each Dwarf runs Klipper firmware (its own heater,
extruder, loadcell, sensors, fan and lights); the XLBuddy's Klipper firmware
is the bridge to them on the printer's RS485 bus; the modular bed keeps its
stock Prusa firmware.

**Install from one SD card.** A Raspberry Pi Imager image (MainsailOS +
KlipperXL-Plus) boots into an installer page in the browser: it builds the
firmware on the Pi, guides the XLBuddy flash with a USB stick, flashes every
tool over the bus (backup first, read back and checked) and writes the
configs for the tools it found. Updates come from Mainsail's update list; a
firmware check at every start says plainly when a board needs new firmware.
The installer also adds the bedlet grid (the bed's 16 zone temperatures) as
a camera in Mainsail.

**Guided calibration wizards** (Macros → CALIBRATION_WIZARDS): loadcell
reference, heater PID, filament sensors, Z alignment, dock positions, tool
offsets, input shaper, pressure advance.

**Like stock Prusa firmware:**
- filament: load / unload / autoload at the front of the bed, autoload into
  the gears, the end of print pulls the filament out of the melt zone and the
  next print gives it back on the tool's first extrusion, cold unload, stock
  `M600` and `M591`
- tool changer: follows a tool taken off or put on by hand, refuses to pick a
  tool that is not in its dock, tool status LEDs in stock's colours, homing
  lines up the tool locks (right edge bump), an X/Y home leaves an unknown Z
  un-homed, pick and park use each dock's own calibrated position
- tool offsets on the pin with stock G425's checks and stock's loadcell touch
  filter
- motor drivers: stock currents, microsteps, chopper and StallGuard pair

**Mainsail:** a filament sensor row for each tool's side and tool sensor;
KlipperScreen Load / Unload, and its Cooldown turns the modular bed off.

Test images before this release: v0.1.0 and v0.1.1 (2026-10-10, not public).
