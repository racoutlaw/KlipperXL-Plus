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
of its address works. Its camera is taller now (3:4): in Mainsail's camera
panel on a computer screen the whole page shows without scrolling. A camera
still at v1.0.0's 1:1 is changed; any other shape you chose is kept.

**Parked tools get their tool-change retract back.** In a multi-tool print
the slicer pulls a tool's filament back a little when it parks it (2 mm in
our OrcaSlicer tests) and pushes it back when it picks that tool again - but
not on a tool's first pick in the next print, so each parked tool started
that print short. Now, as stock Prusa firmware does, every tool's extruder
moves are counted, how far each tool is pulled back is remembered at the end
of a print or a cancel, and it is given back on that tool's first extrusion
of the next print.

**Probe waits for the head to settle.** Before every loadcell tap (Z home,
each mesh point, `PROBE`) the head now stands still 0.5 s and the loadcell is
zeroed over 0.15 s, as the original KlipperXL did. Klipper zeroed it the
moment the travel move ended, with the head still shaking: in our test the
first tap at a point read on average 0.021 mm high (up to 0.044 mm), with
the wait 0.007 mm. A 7x6 mesh takes about 24 s longer.

**Installer log open from the start.** The installer page's log window
(now called **Installer log**) is open when the page loads, so you can see
the installer working from the first step.

**Start-up clock check, last tool too.** The same warning could still show
for the last tool to boot ("MCU 'dwarf_t4' configured for 56Mhz but running
at 55Mhz"). Right after a tool connects, Klipper measures its clock from a
few quick samples; one of them sometimes waited behind another tool's
traffic on the shared bus - the more tools already running, the more often
(last 19 start-ups: the first tool never, the last tool 12 times). The other
tools now leave the bus to the connecting tool for that half second. None
showed in the test restarts after the change; start-up takes the same time.

**Small fixes from a code review.**
- `M140 B<n> S<temp>` (one bedlet) now refuses a target above the bed's
  `max_temp` and refuses while the bed isn't running, the same checks the
  whole-bed `M140` already had.
- `SAVE_TOOL_Z_OFFSET TOOL=<n>` without `Z=` refuses when that tool isn't
  the picked one: it read the picked tool's offset and saved it for the
  other tool. Give `Z=` to set a parked tool.
- A tool that doesn't report parked after a park gets its offset and
  loadcell back - it is still on the carriage, and a later `T<n>` ran it
  with neither.
- `XL_PROBE_Z RETRIES=0` is refused (the minimum is 1); with 0 the probe
  always failed.

**Load asks which filament.** As stock Prusa does, a load or autoload stops
once the gears have the filament and asks its type in a pop-up (Mainsail and
KlipperScreen): PLA 215, PETG 230, ASA 260, PC 275, PVB 215, ABS 255,
HIPS 220, PP 240, FLEX 240, PA 285 C - stock's temperatures - or Cancel,
which takes it back out of the gears. It heats to that, then loads and
purges; a tool already heating keeps its temperature and is not asked. Each
tool remembers its type and its unload heats for it; the Unload button on a
tool whose type is not known asks the same question before anything moves.
Before, everything loaded and unloaded at 215 C unless you typed a
temperature - ABS went in at 215. The Load / Unload buttons have no TEMP box
any more; `LOAD_FILAMENT` / `UNLOAD_FILAMENT TOOL=n TEMP=t` in the console
still take one. FLEX stays in the nozzle after loading (stock: no pull-back
for FLEX).

**Load / Unload buttons no longer fall back to T0.** With no TOOL and no
tool picked they refuse and ask for TOOL. Before, they used T0: a 4 typed
into the TEMP box picked T0 and started heating it.

**After this update** Mainsail asks you to finish it with the installer page
(it adds the bed's `[heater_bed]` section, the new grid page, the longer
loadcell zeroing in each tool's file and the new Load / Unload buttons): power menu
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
