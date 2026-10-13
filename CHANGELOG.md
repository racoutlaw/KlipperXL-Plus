# Changelog

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
