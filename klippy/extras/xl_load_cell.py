# KlipperXL-Plus - Prusa XL Dwarf loadcells: only the selected tool streams
#
# Klipper's LoadCell attaches its sensor permanently at klippy:ready
# (load_cell.py _handle_do_ready), so every configured HX717 streams for
# ever.  Five Dwarfs streaming 320 sps overload the RS485 bridge (printer
# 2026-10-03, test C3: "Unable to obtain 'sensor_bulk_status' response",
# streams stopped).  Stock Prusa runs ONLY the selected Dwarf's loadcell
# and turns it off while the accelerometer runs (src/puppies/Dwarf.cpp
# 437-485).  This subclass keeps everything of Klipper's LoadCell
# (calibration, tare, LOAD_CELL_* commands, clients) and changes one thing:
# the sensor is attached only while its tool is selected.  Deselected, the
# sensor client returns False, Klipper's BatchBulkHelper drops it and stops
# the HX717 on that Dwarf (bulk_sensor.py _proc_batch / _stop).
#
#   [xl_load_cell T0]          # same options as [load_cell]
#   sensor_type: hx717
#   dout_pin: dwarf_t0:PA3
#   sclk_pin: dwarf_t0:PA0
#
#   XL_LOAD_CELL_SELECT TOOL=T0     (or TOOL=NONE) - exactly one streams
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import load_cell


class XLLoadCell(load_cell.LoadCell):
    def __init__(self, config, sensor):
        load_cell.LoadCell.__init__(self, config, sensor)
        self._active = False
        self._attached = False
    # Klipper attaches the sensor here for good; attach only when selected
    def _handle_do_ready(self, eventtime):
        self.add_client(self._track_force)
        if self._active:
            self._attach()
        if self.is_calibrated():
            self.printer.send_event("load_cell:calibrate", self)
        if self.is_tared():
            self.printer.send_event("load_cell:tare", self)
    def _attach(self):
        # Check Klipper's own client list: BatchBulkHelper clears it when it
        # stops on an error (bulk_sensor.py _proc_batch -> _stop), so a
        # local flag alone would go stale.
        clients = getattr(getattr(self.sensor, 'batch_bulk', None),
                          'client_cbs', None)
        if clients is not None:
            self._attached = self._sensor_data_event in clients
        if not self._attached:
            self._attached = True
            self.sensor.add_client(self._sensor_data_event)
    def _sensor_data_event(self, msg):
        if not self._active:
            # BatchBulkHelper removes a client that returns False; with no
            # client left it stops the sensor on the Dwarf
            self._attached = False
            return False
        return load_cell.LoadCell._sensor_data_event(self, msg)
    def set_active(self, active):
        self._active = active
        if active:
            self._attach()
    def is_active(self):
        return self._active


class XLLoadCellSelect:
    # KlipperXL-Plus 2026-10-04 (owner): the selected tool's loadcell streams
    # for as long as it is selected, as stock Prusa does (Dwarf.cpp:440-443
    # enables it on select; loadcell.cpp:256 feeds every sample to the
    # stuck-filament detector).  The 10/03 rule "stream only while probing"
    # answered heater shutdowns whose real cause was the RS485 bridge
    # leaving a Dwarf unpolled for 250 ms (fixed: serial_bridge
    # stream_poll_interval and rate-aware re-poll, FACTS 11-12).
    # select() chooses the probe tool and starts its held stream;
    # stream_begin()/stream_end() are counted, so probes nest on top of it.
    CONTINUOUS = True
    def __init__(self, printer):
        self.printer = printer
        self.cells = {}
        self.selected = None
        self.stream_count = 0
        gcode = printer.lookup_object('gcode')
        gcode.register_command("XL_LOAD_CELL_SELECT",
                               self.cmd_XL_LOAD_CELL_SELECT,
                               desc="Choose which tool's loadcell is the probe")
        gcode.register_command("XL_LOAD_CELL_STREAM",
                               self.cmd_XL_LOAD_CELL_STREAM,
                               desc="Manually stream the selected loadcell")
    def add(self, cell):
        self.cells[cell.name.upper()] = cell
    def _bridge_streaming(self, cell):
        # a streaming Dwarf must be polled often enough that its transmit
        # buffer cannot fill (serial_bridge stream_poll_interval)
        bridge = self.printer.lookup_object('serial_bridge', None)
        if bridge is not None:
            bridge.set_stream_poll(None if cell is None
                                   else cell.get_sensor().get_mcu().get_name())
    def select(self, name):
        name = (name or 'NONE').upper()
        if name != 'NONE' and name not in self.cells:
            raise self.printer.command_error("Unknown loadcell '%s'" % (name,))
        # a tool change also ends any streaming left over from an error
        self.stream_count = 0
        for cell in self.cells.values():
            cell.set_active(False)
        self._bridge_streaming(None)
        self.selected = None if name == 'NONE' else name
        if self.CONTINUOUS and self.selected is not None:
            self.stream_begin()          # held while the tool is selected
        logging.info("xl_load_cell: selected %s", name)
    def stream_begin(self):
        self.stream_count += 1
        if self.stream_count == 1 and self.selected is not None:
            self._bridge_streaming(self.cells[self.selected])
            self.cells[self.selected].set_active(True)
    def stream_end(self):
        self.stream_count = max(0, self.stream_count - 1)
        if not self.stream_count:
            for cell in self.cells.values():
                cell.set_active(False)
            self._bridge_streaming(None)
    def cmd_XL_LOAD_CELL_SELECT(self, gcmd):
        self.select(gcmd.get('TOOL'))
        gcmd.respond_info("Loadcell (probe) tool: %s" % (self.selected or "none"))
    def cmd_XL_LOAD_CELL_STREAM(self, gcmd):
        # manual stream switch for LOAD_CELL_DIAGNOSTIC / calibration
        if gcmd.get_int('ENABLE', 1):
            self.stream_begin()
        else:
            self.stream_count = 0
            for cell in self.cells.values():
                cell.set_active(False)
            self._bridge_streaming(None)
        gcmd.respond_info("Loadcell %s streaming: %s" % (
            self.selected or "none", "on" if self.stream_count else "off"))
    def get_status(self, eventtime):
        return {'selected': self.selected,
                'streaming': bool(self.stream_count),
                'stream_count': self.stream_count}


def load_config_prefix(config):
    printer = config.get_printer()
    sel = printer.lookup_object('xl_load_cell_select', None)
    if sel is None:
        sel = XLLoadCellSelect(printer)
        printer.add_object('xl_load_cell_select', sel)
    sensors = {}
    sensors.update(load_cell.hx71x.HX71X_SENSOR_TYPES)
    sensor_class = config.getchoice('sensor_type', sensors)
    cell = XLLoadCell(config, sensor_class(config))
    sel.add(cell)
    return cell
