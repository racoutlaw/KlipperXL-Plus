# KlipperXL-Plus - automatic per-tool Pressure Advance: loadcell recorder +
# analyser hand-off
#
# Released KlipperXL "XLPATuner Road B" (@1b681b3: dwarf_pa_tuner.py,
# config/pa_tuner.cfg, config/pa_menu.cfg, docs/PRESSURE_ADVANCE.md), with
# ONE change: the pressure trace comes from the picked tool's Klipper load cell
# (xl_load_cell - the selected tool streams 320 sps over the RS485 bridge)
# instead of the XLBuddy's MODBUS loadcell stream, which KlipperXL-Plus does not
# have.  Everything the analyser sees is as released: the same CSV (time from
# the first sample; grams = (raw counts - head tare) x 0.0192 from the RAW
# counts, so Klipper's own tare does not enter; "# param NAME=VALUE" header),
# the same files, the same analyser (pa_analysis/ = CNC Kitchen PrusaPATuner in
# its own venv, run detached so it never blocks Klipper), the same report and
# the same save path (SAVE_TOOL_PA, xl_toolchanger: [extruderN]
# pressure_advance).  Also as released: the extrusion guard of the sweeping
# tool's extruder is raised to 50 mm^2 for the recording only (the sweep moves
# are 6 / 10 / 30 mm^2; max_extrude_cross_section 4.0 stays for printing).
#
#   [xl_pa_tuner]
#   #analysis_python: ~/pa_analysis_env/bin/python
#   #analysis_script: ~/pa_analysis/pa_analyze.py
#
# Commands (released names): START_PA_RECORDING, STOP_PA_RECORDING [params
# written into the CSV header], ANALYZE_PA [CSV=] [TOOL=] [SAVE=1]
# [SAVECONFIG=1] [FILAMENT=], PA_RECORD_TEST [SECONDS=2] (capture only, no
# motion: rate, gaps, noise).  The sweep is kxlplus/xl_pa_tuner.cfg.
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json, logging, os, shutil, subprocess, time

SCALE = 0.0192          # g per raw count (Prusa loadcell scale, released SCALE)
RAW_LIMIT = 1 << 23     # 24-bit ADC: anything outside is not a reading
LATEST = '/tmp/pa_recording.csv'
RESULT = '/tmp/pa_result.json'
BIG_GAP = 0.100         # s - a real hole in the trace (released STOP report)
WRITE_SLICE = 2000      # samples per slice of the recording write (the reactor
                        # runs between slices - see cmd_STOP_PA_RECORDING)


class XLPATuner:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        home = os.path.expanduser('~')
        self.analysis_python = config.get(
            'analysis_python', os.path.join(home, 'pa_analysis_env/bin/python'))
        self.analysis_script = config.get(
            'analysis_script', os.path.join(home, 'pa_analysis/pa_analyze.py'))
        # released: a recording that is never stopped (error, M112, cancelled
        # sweep) must not run for ever; a full fine sweep is ~23 min
        self.max_record = config.getfloat('max_record_seconds', 1800., above=0.)
        self.sweep_cross_section = config.getfloat('sweep_cross_section', 50.,
                                                   above=0.)
        self.recording = False
        self.gen = 0                 # recording number (stale feeds drop out)
        self.rec_tool = None
        self.rec_start = 0.
        self.samples = []            # (print_time, raw counts)
        self.batches = 0
        self.bad = 0
        self.first_counters = None   # sensor (errors, overflows) at the start
        self.last_counters = None
        self.stream_held = False
        self.guard_ext = None
        self.saved_ratio = None
        self.stop_timer = self.reactor.register_timer(self._auto_stop)
        self.an_proc = None
        self.an_timer = self.reactor.register_timer(self._an_poll)
        self.an_tool = -1
        self.an_save = False
        self.an_saveconfig = False
        self.an_label = ''
        # the last analysis, for the pressure advance wizard (xl_wizards):
        # {'tool', 'k', 'res'} or {'tool', 'k': None, 'error'}
        self.last = None
        for cmd, desc in (
                ('START_PA_RECORDING', "Begin buffering the picked tool's"
                 " loadcell in the background"),
                ('STOP_PA_RECORDING', "End the PA recording and write the CSV"),
                ('ANALYZE_PA', "Analyse the last PA recording and report K"
                 " (SAVE=1 stores it)"),
                ('PA_RECORD_TEST', "Capture the picked tool's loadcell for a"
                 " few seconds (no motion) and report rate and noise")):
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                        desc=desc)
    # ---- the picked tool's load cell -------------------------------------
    def _picked(self, gcmd):
        tool = self.printer.lookup_object('xl_toolchanger').active_tool
        if tool is None or tool < 0:
            raise gcmd.error("No tool picked - pick T0-T4 first (only the"
                             " picked tool's loadcell streams)")
        return tool
    def _select(self):
        return self.printer.lookup_object('xl_load_cell_select')
    def _cell(self, gcmd, tool):
        sel = self._select()
        name = 'T%d' % (tool,)
        if sel.selected != name or name not in sel.cells:
            raise gcmd.error("T%d's loadcell is not the selected one" % (tool,))
        return sel.cells[name]
    def _on_batch(self, msg, gen):
        # rows: [print_time, grams, raw counts, tare counts] (load_cell.py)
        if not self.recording or gen != self.gen:
            return False                 # Klipper drops this client
        for row in msg.get('data') or ():
            counts = row[2]
            if counts is None or counts > RAW_LIMIT or counts < -RAW_LIMIT:
                self.bad += 1
                continue
            self.samples.append((row[0], counts))
        self.batches += 1
        counters = (msg.get('errors') or 0, msg.get('overflows') or 0)
        if self.first_counters is None:
            self.first_counters = counters
        self.last_counters = counters
        return True
    def _begin(self, gcmd, tool, guard):
        cell = self._cell(gcmd, tool)
        self.samples = []
        self.batches = self.bad = 0
        self.first_counters = self.last_counters = None
        self.rec_tool = tool
        self.gen += 1
        self.recording = True
        cell.add_client(lambda msg, gen=self.gen: self._on_batch(msg, gen))
        self._select().stream_begin()
        self.stream_held = True
        self.guard_ext = self.saved_ratio = None
        if guard:
            ext = self.printer.lookup_object('toolhead').get_extruder()
            if hasattr(ext, 'max_extrude_ratio'):
                self.guard_ext, self.saved_ratio = ext, ext.max_extrude_ratio
                ext.max_extrude_ratio = (self.sweep_cross_section
                                         / ext.filament_area)
                logging.info("xl_pa_tuner: extrude guard %.1f mm^2 for the"
                             " sweep (was %.3f mm^2)", self.sweep_cross_section,
                             self.saved_ratio * ext.filament_area)
        self.rec_start = self.reactor.monotonic()
        self.reactor.update_timer(self.stop_timer,
                                  self.rec_start + self.max_record)
    def _end(self):
        """Idempotent teardown - success, error or abort."""
        self.recording = False
        self.reactor.update_timer(self.stop_timer, self.reactor.NEVER)
        if self.guard_ext is not None:
            self.guard_ext.max_extrude_ratio = self.saved_ratio
            logging.info("xl_pa_tuner: extrude guard restored")
            self.guard_ext = self.saved_ratio = None
        if self.stream_held:
            self.stream_held = False
            sel = self._select()
            # a tool change resets the stream count (xl_load_cell select)
            if sel.selected == 'T%d' % (self.rec_tool,):
                sel.stream_end()
    def _auto_stop(self, eventtime):
        logging.warning("xl_pa_tuner: recording exceeded %.0f s, stopping",
                        self.max_record)
        self._end()
        return self.reactor.NEVER
    def _stats(self):
        ts = [s[0] for s in self.samples]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        span = ts[-1] - ts[0] if len(ts) > 1 else 0.
        rate = (len(ts) - 1) / span if span > 0 else 0.
        errs = ovfl = 0
        if self.first_counters is not None:
            errs = self.last_counters[0] - self.first_counters[0]
            ovfl = self.last_counters[1] - self.first_counters[1]
        return gaps, rate, errs, ovfl
    # ---- commands ----------------------------------------------------------
    def cmd_START_PA_RECORDING(self, gcmd):
        if self.recording:
            raise gcmd.error("START_PA_RECORDING: already recording")
        tool = self._picked(gcmd)
        self._begin(gcmd, tool, guard=True)
        gcmd.respond_info("PA recording started (T%d, Klipper load cell"
                          " stream)" % (tool,))
    def cmd_STOP_PA_RECORDING(self, gcmd):
        if not self.recording:
            raise gcmd.error("STOP_PA_RECORDING: not recording")
        elapsed = self.reactor.monotonic() - self.rec_start
        tool = self.rec_tool
        self._end()
        n = len(self.samples)
        if not n:
            gcmd.respond_info("STOP_PA_RECORDING: no samples captured in"
                              " %.2fs" % (elapsed,))
            return
        samples = self.samples
        # tare off the head of the recording (the quiet baseline dwell)
        n_tare = max(1, min(n // 20, 100))
        tare = float(sum(s[1] for s in samples[:n_tare])) / n_tare
        t0 = samples[0][0]
        # every sweep keeps its own file; /tmp/pa_recording.csv = the latest
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = "/tmp/pa_recording_T%d_%s.csv" % (tool, stamp)
        # Written in slices with Klipper's main loop let in between, the
        # report's figures gathered on the way.  In one go (~122k lines plus
        # three more passes over them) it held the reactor 0.51 / 0.57 s and
        # the hot tool's Dwarf got a timed command late: "MCU 'dwarf_t4'
        # shutdown: Timer too close" right after the sweep (owner 10/11, twice;
        # ANALYZE_PA alone and SET_PRESSURE_ADVANCE alone did not do it).
        g_min = g_max = None
        worst = 0.
        big = 0
        prev = t0
        try:
            with open(path, "w") as f:
                f.write("# tool=T%d dwarf=%d samples=%d elapsed=%.3f tare=%.1f"
                        " scale=%.4f\n" % (tool, tool + 1, n, elapsed, tare,
                                           SCALE))
                for k in gcmd.get_command_parameters().items():
                    f.write("# param %s=%s\n" % k)
                f.write("time_s,grams\n")
                for a in range(0, n, WRITE_SLICE):
                    rows = []
                    for t, raw in samples[a:a + WRITE_SLICE]:
                        g = (raw - tare) * SCALE
                        rows.append("%.6f,%.4f\n" % (t - t0, g))
                        if g_min is None or g < g_min:
                            g_min = g
                        if g_max is None or g > g_max:
                            g_max = g
                        gap = t - prev
                        if gap > worst:
                            worst = gap
                        if gap > BIG_GAP:
                            big += 1
                        prev = t
                    f.write("".join(rows))
                    self.reactor.pause(self.reactor.monotonic() + 0.001)
            shutil.copyfile(path, LATEST)
        except Exception as e:
            gcmd.respond_info("STOP_PA_RECORDING: WRITE FAILED: %s" % (e,))
            return
        span = samples[-1][0] - t0
        rate = (n - 1) / span if span > 0 else 0.
        errs = ovfl = 0
        if self.first_counters is not None:
            errs = self.last_counters[0] - self.first_counters[0]
            ovfl = self.last_counters[1] - self.first_counters[1]
        gcmd.respond_info(
            "PA recording stopped: %d samples in %.2fs (%.1f Hz)\n"
            "  tared grams: min=%.2f max=%.2f\n"
            "  gaps: worst=%.0fms  (>%.0fms: %d of %d)\n"
            "  batches=%d rejected=%d sensor errors=%d overflows=%d\n"
            "  written to %s"
            % (n, elapsed, rate, g_min, g_max, worst * 1000., BIG_GAP * 1000.,
               big, n - 1, self.batches, self.bad, errs, ovfl, path))
    def cmd_PA_RECORD_TEST(self, gcmd):
        if self.recording:
            raise gcmd.error("PA_RECORD_TEST: a recording is running")
        seconds = gcmd.get_float('SECONDS', 2., above=0., maxval=60.)
        tool = self._picked(gcmd)
        self._begin(gcmd, tool, guard=False)
        try:
            self.reactor.pause(self.reactor.monotonic() + seconds)
        finally:
            self._end()
        n = len(self.samples)
        if n < 2:
            gcmd.respond_info("PA_RECORD_TEST: %d samples in %.1fs (T%d) -"
                              " the loadcell is not streaming"
                              % (n, seconds, tool))
            return
        raws = [s[1] for s in self.samples]
        gaps, rate, errs, ovfl = self._stats()
        sg = sorted(gaps)
        n_tare = max(1, min(n // 5, 100))
        tare = float(sum(raws[:n_tare])) / n_tare
        grams = [(r - tare) * SCALE for r in raws]
        mean = sum(grams) / n
        sd = (sum((g - mean) ** 2 for g in grams) / n) ** 0.5
        gcmd.respond_info(
            "PA_RECORD_TEST: %d samples in %.1fs (T%d)\n"
            "  rate %.1f Hz; sample spacing min=%.1f median=%.1f max=%.1f ms"
            " (>%.0fms: %d)\n"
            "  raw min=%d max=%d; tared grams min=%.3f max=%.3f noise sd=%.4f g\n"
            "  batches=%d rejected=%d sensor errors=%d overflows=%d"
            % (n, seconds, tool, rate, sg[0] * 1000., sg[len(sg) // 2] * 1000.,
               sg[-1] * 1000., BIG_GAP * 1000.,
               sum(1 for g in gaps if g > BIG_GAP), min(raws), max(raws),
               min(grams), max(grams), sd, self.batches, self.bad, errs, ovfl))
    # ---- analysis (released ANALYZE_PA) -----------------------------------
    # Fitting 100k+ samples takes tens of seconds: the analyser runs as a
    # detached process in its own venv and is polled from a timer, so Klipper
    # never blocks.  SAVECONFIG=1 must therefore be issued from here once the
    # result is in - a SAVE_CONFIG line in a macro would run before it.
    def cmd_ANALYZE_PA(self, gcmd):
        if self.an_proc is not None:
            raise gcmd.error("ANALYZE_PA: an analysis is already running")
        csv = gcmd.get('CSV', LATEST)
        if not os.path.exists(csv):
            raise gcmd.error("ANALYZE_PA: no recording at %s" % (csv,))
        self.an_save = bool(gcmd.get_int('SAVE', 0))
        self.an_saveconfig = bool(gcmd.get_int('SAVECONFIG', 0))
        tool = gcmd.get_int('TOOL', -1)
        if tool < 0:
            tool = self.printer.lookup_object('xl_toolchanger').active_tool
        self.an_tool = tool
        self.an_label = gcmd.get('FILAMENT', '')
        self.last = None
        try:
            self.an_proc = subprocess.Popen(
                [self.analysis_python, self.analysis_script, csv],
                cwd=os.path.dirname(self.analysis_script) or '/tmp',
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            self.an_proc = None
            raise gcmd.error("ANALYZE_PA: could not start the analyser (%s)"
                             " - is ~/pa_analysis_env installed?" % (e,))
        gcmd.respond_info("Analysing %s in the background (T%d)%s ..."
                          % (csv, tool, " and will SAVE" if self.an_save
                             else ""))
        self.reactor.update_timer(self.an_timer,
                                  self.reactor.monotonic() + 1.)
    def _an_poll(self, eventtime):
        proc = self.an_proc
        if proc is None:
            return self.reactor.NEVER
        if proc.poll() is None:
            return eventtime + 1.
        try:
            out = proc.stdout.read() or b''
        except Exception:
            out = b''
        rc = proc.returncode
        self.an_proc = None
        if rc != 0:
            self.last = {'tool': self.an_tool, 'k': None,
                         'error': "the analyser failed (rc=%d)" % (rc,)}
            tail = out.decode('utf-8', 'replace').strip().splitlines()[-6:]
            self.gcode.respond_info("ANALYZE_PA FAILED (rc=%d):\n  %s"
                                    % (rc, "\n  ".join(tail)))
            return self.reactor.NEVER
        try:
            with open(RESULT) as fh:
                res = json.load(fh)
        except Exception as e:
            self.last = {'tool': self.an_tool, 'k': None,
                         'error': "could not read the result (%s)" % (e,)}
            self.gcode.respond_info("ANALYZE_PA: could not read result: %s"
                                    % (e,))
            return self.reactor.NEVER
        self._report(res)
        return self.reactor.NEVER
    def _report(self, res):
        # bd_k_opt is the headline; every fit is shown with its r^2 so a bad
        # fit is visible (released: phase_fit once gave 0.0652 at r2=0.008)
        k = res.get('bd_k_opt')
        self.last = {'tool': self.an_tool, 'k': k, 'res': res}
        lines = ["PA analysis:",
                 "  bd_k_opt            %s   <-- headline" % (k,)]
        for fname in ('integral_fit', 'bd_signed_rise_fit',
                      'bd_signed_fall_fit', 'integral_legacy_fit', 'phase_fit'):
            fit = res.get(fname)
            if not isinstance(fit, dict):
                continue
            r2 = fit.get('r_squared')
            flag = ""
            if isinstance(r2, float):
                flag = "  <-- strong" if r2 >= 0.9 else (
                    "  <-- IGNORE, no fit" if r2 < 0.2 else "")
            lines.append("  %-19s k=%s  r2=%s%s"
                         % (fname, fit.get('k_opt'), r2, flag))
        seg_n = res.get('bd_k_from_segments_n')
        if seg_n:
            lines.append("  segments            %s used, per-segment k=%s"
                         " (MAD %s)" % (seg_n, res.get('bd_k_from_segments'),
                                        res.get('bd_k_from_segments_mad')))
        if 'sample_rate_hz' in res:
            lines.append("  sample rate         %s Hz" % (res['sample_rate_hz'],))
        self.gcode.respond_info("\n".join(lines))
        if k is None:
            self.gcode.respond_info(
                "  no K_opt -- usually too few cycles per K (needs >=4"
                " INCLUDED segments; use CYCLES=6 or more)")
            return
        if not self.an_save:
            self.gcode.respond_info("  not saved. Re-run with SAVE=1, or:"
                                    " SAVE_TOOL_PA TOOL=%d K=%.4f"
                                    % (self.an_tool, k))
            return
        if self.an_tool is None or self.an_tool < 0:
            self.gcode.respond_info("  SAVE requested but no tool known - not"
                                    " saved")
            return
        script = "SAVE_TOOL_PA TOOL=%d K=%.4f" % (self.an_tool, k)
        if self.an_label:
            script += " FILAMENT=%s" % (self.an_label,)
        if self.an_saveconfig:
            script += "\nSAVE_CONFIG"
        tool, saveconfig = self.an_tool, self.an_saveconfig
        # from a timer: queue it like any command (takes the g-code mutex)
        self.reactor.register_callback(
            lambda e: self._save(script, tool, k, saveconfig))
    def _save(self, script, tool, k, saveconfig):
        try:
            self.gcode.run_script(script)
        except Exception as e:
            self.gcode.respond_info("ANALYZE_PA: saving failed: %s" % (e,))
            return
        if not saveconfig:
            # live now, lost on the next restart unless saved (released box)
            self.gcode.respond_info(
                "\n"
                "  ============================================================\n"
                "    T%d PA = %.4f is ACTIVE NOW but NOT YET SAVED.\n"
                "    It will be LOST on the next restart.\n"
                "    Run  SAVE_CONFIG  to keep it (Klipper will restart).\n"
                "  ============================================================"
                % (tool, k))
    def get_status(self, eventtime):
        return {'recording': self.recording,
                'analysing': self.an_proc is not None}

def load_config(config):
    return XLPATuner(config)
