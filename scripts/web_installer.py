#!/usr/bin/env python3
# KlipperXL-Plus - the web installer: the install runs from a page at the Pi's own
# address, port 8099 (owner 10/10: "RatOS style" - the Pi already knows the
# network from Raspberry Pi Imager, so no hotspot; option B: the page asks the
# password).  Started by scripts/bootstrap_kxlplus.sh, detached.
#
# The page drives the SAME scripts as the terminal install - one installer:
#   Start       -> bootstrap_kxlplus.sh --engine --klipperscreen y|n
#                  (build tools, Klipper update, KlipperScreen, KlipperXL-Plus,
#                   then install_kxlplus.sh: modules, firmware builds, ...)
#   "@@ASK {json}" lines from the scripts become buttons; the answer goes to
#                  their stdin (KXL_WEB=1 - install_kxlplus.sh, xl_usb_stick.py)
#   "@@STEP x", "@@STAGE1_DONE", "@@KLIPPER", "@@FIRMWARE" move the page on
#   Continue    -> install_kxlplus.sh again after the XLBuddy flash
#
# The password: checked with sudo, then turned into a TEMPORARY sudoers rule
# (/etc/sudoers.d/kxlplus-install, NOPASSWD for this user) so the scripts' own
# sudo calls need nothing; never written anywhere else.  The rule is removed
# when the install finishes or stops, and when this program exits.  The page
# itself stops 15 min after the end (or after 4 h if never started).
#
#   python3 web_installer.py --kxl-dir ~/KlipperXL-Plus [--port 8099]
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import argparse, atexit, errno, getpass, http.server, json, os, re, signal
import subprocess, sys, tempfile, threading, time, urllib.request
from urllib.parse import urlparse, parse_qs

SUDOERS = '/etc/sudoers.d/kxlplus-install'
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
IDLE_EXIT = 4 * 3600
DONE_EXIT = 15 * 60
LOG_MAX = 20000


class Installer:
    def __init__(self, kxl_dir):
        self.kxl_dir = kxl_dir
        self.lock = threading.Lock()
        self.log = []
        self.step = 'welcome'   # welcome prepare xlbuddy flash tools done failed
        self.question = None
        self.proc = None
        self.stage1 = None      # 'stick' / 'by-hand': the XLBuddy needs the flash
        self.result = {}
        self.error = None
        self.klipperscreen = False
        self.created = time.time()
        self.finished = None
        self.sudo_granted = False
        self.quit_requested = False

    # ---- state -------------------------------------------------------------
    def add(self, line):
        with self.lock:
            self.log.append(line)
            if len(self.log) > LOG_MAX:
                del self.log[:len(self.log) - LOG_MAX]
    def state(self, since):
        with self.lock:
            since = max(0, min(since, len(self.log)))
            return {'step': self.step, 'question': self.question,
                    'stage1': self.stage1, 'result': self.result,
                    'error': self.error, 'running': self.proc is not None,
                    'klipperscreen': self.klipperscreen,
                    'log': self.log[since:], 'next': len(self.log)}

    # ---- the password -> a temporary sudo rule ----------------------------
    def sudo_grant(self, password):
        user = getpass.getuser()
        r = subprocess.run(['sudo', '-S', '-k', '-p', '', '-v'],
                           input=password + '\n', text=True,
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            return "sudo did not accept that password."
        fd, tmp = tempfile.mkstemp(prefix='kxlplus-sudo-')
        with os.fdopen(fd, 'w') as f:
            # env_keep DEBIAN_FRONTEND: sudo drops it, and apt run by other
            # installers (KlipperScreen's) then filled the log with debconf
            # "unable to initialize frontend" lines (image test 10/10)
            f.write("# KlipperXL-Plus web installer - TEMPORARY, removed when the"
                    " install ends\nDefaults:%s env_keep += \"DEBIAN_FRONTEND\"\n"
                    "%s ALL=(ALL) NOPASSWD: ALL\n" % (user, user))
        try:
            # only the password goes to stdin; visudo + install read nothing
            r = subprocess.run(
                ['sudo', '-S', '-k', '-p', '', 'sh', '-c',
                 'visudo -cqf "$1" && install -m 0440 -o root -g root "$1" "$2"',
                 'sh', tmp, SUDOERS],
                input=password + '\n', text=True, capture_output=True,
                timeout=30)
        finally:
            os.unlink(tmp)
        if r.returncode != 0 or subprocess.run(
                ['sudo', '-n', 'true'], capture_output=True).returncode != 0:
            return ("Could not set up the install permission: %s"
                    % ((r.stderr or r.stdout).strip() or "sudo -n failed",))
        self.sudo_granted = True
        self.add("  Install permission set up (temporary - removed at the end).")
        return None
    def sudo_revoke(self):
        if not self.sudo_granted:
            return
        subprocess.run(['sudo', '-n', 'rm', '-f', SUDOERS], capture_output=True)
        self.sudo_granted = False
        self.add("  Install permission removed - sudo asks for the password"
                 " again as normal.")

    # ---- running the scripts ------------------------------------------------
    def _run(self, argv):
        env = dict(os.environ, KXL_WEB='1', PYTHONUNBUFFERED='1',
                   DEBIAN_FRONTEND='noninteractive')
        self.proc = subprocess.Popen(argv, cwd=os.path.expanduser('~'), env=env,
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        threading.Thread(target=self._reader, args=(self.proc,),
                         daemon=True).start()
    def _marker(self, line):
        kind, _, rest = line[2:].partition(' ')
        with self.lock:
            if kind == 'ASK':
                try:
                    self.question = json.loads(rest)
                except ValueError:
                    self.question = None
            elif kind == 'STEP':
                self.step = rest.strip()
            elif kind == 'STAGE1_DONE':
                self.stage1 = rest.strip() or 'stick'
            elif kind == 'KLIPPER':
                self.result['klipper'] = rest.strip()
            elif kind == 'FIRMWARE':
                self.result['firmware'] = rest.strip()
    def _reader(self, proc):
        for raw in iter(proc.stdout.readline, b''):
            text = ANSI.sub('', raw.decode('utf-8', 'replace')).rstrip('\n')
            # progress lines are redrawn with \r (apt, pip, KlipperScreen's
            # installer): show the last text, and drop lines that were only
            # redraws - image test 10/10: they filled the log with blanks
            parts = [p for p in text.split('\r') if p.strip()]
            if not parts and '\r' in text:
                continue
            line = parts[-1] if parts else ''
            if line.startswith('@@'):
                self._marker(line)
            else:
                self.add(line)
        rc = proc.wait()
        with self.lock:
            self.proc = None
            self.question = None
            if rc == 0 and self.stage1:
                self.step = 'flash'
                return
            if rc == 0 and self.step == 'done':
                self.finished = time.time()
            else:
                self.step = 'failed'
                self.error = ("The install stopped (exit code %d) - the last"
                              " lines below say why." % (rc,))
        self.sudo_revoke()
        if self.step == 'done':
            self.add("  Done. This installer page stops by itself in %d"
                     " minutes." % (DONE_EXIT // 60,))

    # ---- what the page asks for --------------------------------------------
    def start(self, appendix, klipperscreen, password):
        with self.lock:
            if self.proc is not None or self.step not in ('welcome', 'failed'):
                return "The install is already running."
        if appendix is not True:
            return "Tick the appendix box first."
        if not password:
            return "Type your Pi password."
        err = self.sudo_grant(password)
        if err:
            return err
        with self.lock:
            self.klipperscreen = bool(klipperscreen)
            self.step, self.error, self.stage1, self.result = (
                'prepare', None, None, {})
        self.add("=== Started from the installer page: appendix confirmed,"
                 " KlipperScreen %s ===" % ("yes" if klipperscreen else "no",))
        self._run(['bash', os.path.join(self.kxl_dir, 'scripts',
                                        'bootstrap_kxlplus.sh'),
                   '--engine', '--klipperscreen',
                   'y' if klipperscreen else 'n'])
        return None
    def answer(self, qid, value):
        with self.lock:
            q, proc = self.question, self.proc
            if q is None or proc is None or q.get('id') != qid:
                return "That question is no longer open."
            label = next((c[1] for c in q.get('choices', [])
                          if c[0] == value), value)
            self.question = None
        self.add("  > %s" % (label,))
        try:
            proc.stdin.write((value + '\n').encode())
            proc.stdin.flush()
        except OSError as e:
            return "Could not pass the answer on: %s" % (e,)
        return None
    def resume(self, password=None):
        # after the XLBuddy flash (the permission is still in place); after a
        # failure the page goes back to Start, which asks the password again
        with self.lock:
            if self.step != 'flash' or self.proc is not None:
                return "Nothing is waiting for the XLBuddy flash."
            self.stage1 = None
            self.step = 'tools'
        self.add("=== The XLBuddy is flashed - stage 2: the tools ===")
        self._run(['bash', os.path.join(self.kxl_dir, 'scripts',
                                        'install_kxlplus.sh')])
        return None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KlipperXL-Plus installer</title>
<style>
:root { --bg:#16181c; --card:#1f2228; --line:#30343c; --text:#e6e6e6;
        --dim:#9aa0a8; --accent:#fa6831; --ok:#4caf50; --bad:#e5534b; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
main { max-width:860px; margin:0 auto; padding:20px 16px 60px; }
h1 { margin:0 0 4px; font-size:26px; } h1 b { color:var(--accent); }
.sub { color:var(--dim); margin:0 0 20px; }
.steps { display:flex; gap:6px; flex-wrap:wrap; margin:0 0 18px; padding:0;
         list-style:none; }
.steps li { flex:1 1 120px; padding:8px 10px; border-radius:6px;
            background:var(--card); border:1px solid var(--line);
            color:var(--dim); font-size:14px; }
.steps li.now { border-color:var(--accent); color:var(--text); }
.steps li.did { color:var(--ok); }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:18px 20px; margin:0 0 16px; }
.card h2 { margin:0 0 10px; font-size:20px; }
.warn { border-color:var(--accent); }
.warn h2 { color:var(--accent); }
label.row { display:flex; gap:10px; align-items:flex-start; margin:12px 0;
            cursor:pointer; }
label.row input { margin-top:5px; width:18px; height:18px; }
.choice { display:flex; gap:10px; flex-wrap:wrap; margin:8px 0 4px; }
input[type=password] { width:100%; max-width:340px; padding:9px 10px;
       border-radius:6px; border:1px solid var(--line); background:#121418;
       color:var(--text); font-size:16px; }
button { padding:10px 18px; border-radius:6px; border:1px solid var(--line);
         background:#2a2e36; color:var(--text); font-size:16px; cursor:pointer; }
button.go { background:var(--accent); border-color:var(--accent); color:#fff; }
button:disabled { opacity:.45; cursor:not-allowed; }
.seg button.on { border-color:var(--accent); color:var(--accent); }
.err { color:var(--bad); margin-top:10px; min-height:1.2em; }
.now-line { font-family:ui-monospace,Consolas,monospace; font-size:14px;
            color:var(--dim); white-space:pre-wrap; word-break:break-all; }
ol li { margin:4px 0; }
pre#log { background:#0f1114; border:1px solid var(--line); border-radius:6px;
          padding:10px; height:320px; overflow:auto; font-size:13px;
          white-space:pre-wrap; word-break:break-all; margin:8px 0 0; }
.spin { display:inline-block; width:14px; height:14px; border:2px solid var(--dim);
        border-top-color:var(--accent); border-radius:50%;
        animation:s 0.9s linear infinite; vertical-align:-2px; margin-right:8px; }
@keyframes s { to { transform:rotate(360deg); } }
.big { font-size:18px; }
.okc { color:var(--ok); } .badc { color:var(--bad); }
a { color:var(--accent); }
</style></head><body><main>
<h1>Klipper<b>XL-Plus</b> installer</h1>
<p class="sub">Klipper on every tool of your Prusa XL. Keep this page open -
everything happens here.</p>
<ul class="steps" id="steps"></ul>
<div id="view"></div>
<div class="card">
  <details id="det" open><summary>Installer log</summary>
  <pre id="log"></pre></details>
  <div class="now-line" id="last"></div>
</div>
</main>
<script>
const STEPS = [['welcome','1. Appendix'],['prepare','2. Run first: prepare'],
  ['xlbuddy','3. XLBuddy firmware'],['tools','4. Run second: tools'],
  ['done','5. Done']];
let since = 0, st = null, ks = null, busy = false, lastView = '';
const $ = id => document.getElementById(id);
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;',
  '<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
async function post(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':
    'application/json'}, body: JSON.stringify(body || {})});
  return r.json();
}
function stepIndex(s) {
  if (s === 'flash') return 2; if (s === 'failed') return -1;
  return STEPS.findIndex(x => x[0] === s);
}
function drawSteps() {
  const i = stepIndex(st.step);
  $('steps').innerHTML = STEPS.map((x, n) =>
    '<li class="' + (n < i || st.step === 'done' ? 'did' : n === i ? 'now' : '')
    + '">' + x[1] + '</li>').join('');
}
function question() {
  const q = st.question;
  if (!q) return '';
  return '<div class="card warn"><h2>Your answer is needed</h2><p class="big">'
    + esc(q.text) + '</p><div class="choice">' + (q.choices || []).map(c =>
    '<button class="' + (c[0] === 'y' ? 'go' : '') + '" data-a="' + esc(c[0])
    + '">' + esc(c[1]) + '</button>').join('') + '</div><div class="err" id="qerr"></div></div>';
}
function working(title, text) {
  return '<div class="card"><h2><span class="spin"></span>' + title + '</h2><p>'
    + text + '</p></div>';
}
const APPENDIX = `<div class="card warn"><h2>Read this before you start - the appendix</h2>
<p>The Prusa bootloader verifies firmware signatures and will reject unsigned
Klipper firmware unless the <b>appendix</b> (safety seal) on the XLBuddy board is
broken. Breaking it is a <b>physical, irreversible modification to your
mainboard</b> - it cannot be undone, and it is the same requirement as for any
custom firmware on the XL.</p>
<p>Everything else here is reversible: you can flash stock Prusa firmware back
at any time and the printer returns to normal. The appendix is the one step
that does not come back. Decide about it before you begin, not halfway
through.</p>
<p><b>With the appendix intact, none of this works</b> - the bootloader refuses
the KlipperXL-Plus firmware.</p>
<p style="color:var(--dim);font-size:14px">This software is provided "as is"
without warranty of any kind. You are modifying your printer's firmware and
accept all risks of doing so.</p>
<label class="row"><input type="checkbox" id="app"> <span>The appendix on my
XLBuddy is broken, and I accept the above.</span></label></div>
<div class="card"><h2>Before you press Start</h2>
<p>Turn the printer <b>on</b> and leave it connected to the Pi by USB.</p>
<p><b>Is a touchscreen connected to the Pi</b> (for KlipperScreen)?</p>
<div class="choice seg"><button id="ksy">Yes</button><button id="ksn">No</button></div>
<p style="margin-top:16px"><b>Your Pi password</b> - the one you set in Raspberry
Pi Imager. It is used once to install and is not saved.</p>
<input type="password" id="pw" autocomplete="current-password">
<div style="margin-top:16px"><button class="go" id="start" disabled>Start</button></div>
<div class="err" id="serr"></div></div>`;
function viewFor() {
  const s = st.step;
  if (s === 'welcome' || s === 'failed') {
    return (s === 'failed' ? '<div class="card warn"><h2 class="badc">The install stopped</h2><p>'
      + esc(st.error || '') + '</p><p>Fix what the last lines say, then start again'
      + ' below - it picks up where it can.</p></div>' : '') + APPENDIX;
  }
  if (s === 'flash') {
    const hand = st.stage1 === 'by-hand';
    return '<div class="card warn"><h2>Now flash the XLBuddy</h2>'
      + (hand ? '<p>Copy <code>~/kxlplus-build/xlbuddy/klipper.bbf</code> to the'
        + ' root of a FAT32 USB stick (rename any other .bbf on it).</p>'
        : '<p>The firmware is on your USB stick - take it out of the Pi.</p>')
      + '<ol><li>Plug the stick into the <b>printer\'s front USB port</b> and'
      + ' <b>power-cycle</b> the printer.</li><li>When the <b>Prusa logo</b>'
      + ' appears, press the <b>knob 2-3 times</b> - the firmware update menu'
      + ' opens. Confirm <code>klipper.bbf</code>.</li><li>If it asks about the'
      + ' signature, press <b>Ignore</b>.</li><li>The progress bar <b>stops at'
      + ' about 50 %</b> - that is normal.</li><li>Power the printer <b>off</b>,'
      + ' pull the stick out, power it <b>on</b>.</li></ol><p style="color:'
      + 'var(--dim)">The bootloader does not see the stick? Press the printer\'s'
      + ' reset button, then the knob 2-3 times.</p>'
      + '<button class="go" id="cont">The XLBuddy is flashed and the printer'
      + ' is on - continue</button><div class="err" id="cerr"></div></div>';
  }
  if (s === 'done') {
    const r = st.result, ok = r.klipper === 'ready'
      && /every board/.test(r.firmware || '');
    return '<div class="card ' + (ok ? '' : 'warn') + '"><h2 class="'
      + (ok ? 'okc' : 'badc') + '">' + (ok ? 'KlipperXL-Plus is installed'
      : 'Installed - but check this') + '</h2><p class="big">Klipper: <b>'
      + esc(r.klipper || '?') + '</b><br>Firmware: ' + esc(r.firmware || '?')
      + '</p><p><a href="http://' + location.hostname + '/" target="_blank">'
      + 'Open Mainsail</a>, then calibrate: <b>Macros &rarr; CALIBRATION_WIZARDS'
      + '</b>, in this order: loadcell reference, heater PID, filament sensors,'
      + ' Z alignment, dock positions (optional), tool offsets, input shaper,'
      + ' pressure advance.</p>'
      + '<p><b class="badc">Pick or park problems?</b> Run dock positions'
      + ' first - every other calibration picks and parks tools.</p>'
      + '<p style="color:var(--dim)">This page stops by itself in 15 minutes.'
      + '</p></div>';
  }
  const title = s === 'tools' ? 'Run second: the tools' :
                s === 'xlbuddy' ? 'XLBuddy firmware' : 'Run first: preparing';
  const text = s === 'tools' ? 'Finding the tools, flashing each one (backed up'
      + ' and verified), writing the configs and starting Klipper.'
    : s === 'xlbuddy' ? 'Putting the XLBuddy firmware on a USB stick plugged'
      + ' into the Pi.'
    : 'Installing the build tools, updating Klipper' + (st.klipperscreen ?
      ', installing KlipperScreen' : '') + ' and building the firmware for the'
      + ' XLBuddy and the tools. This takes 10-20 minutes - leave this page open.';
  return question() + working(title, text);
}
function bind() {
  const app = $('app');
  if (app) {
    const pw = $('pw'), start = $('start');
    const upd = () => start.disabled = !(app.checked && pw.value && ks !== null);
    app.onchange = upd; pw.oninput = upd;
    const seg = v => { ks = v; $('ksy').classList.toggle('on', v);
                       $('ksn').classList.toggle('on', !v); upd(); };
    $('ksy').onclick = () => seg(true); $('ksn').onclick = () => seg(false);
    if (ks !== null) seg(ks);
    start.onclick = async () => {
      start.disabled = true; $('serr').textContent = 'Checking the password...';
      const r = await post('/api/start', {appendix: app.checked,
        klipperscreen: ks, password: pw.value});
      pw.value = '';
      $('serr').textContent = r.error || ''; if (r.error) upd();
      lastView = ''; tick();
    };
  }
  document.querySelectorAll('[data-a]').forEach(b => b.onclick = async () => {
    document.querySelectorAll('[data-a]').forEach(x => x.disabled = true);
    const r = await post('/api/answer', {id: st.question.id, value: b.dataset.a});
    if (r.error && $('qerr')) $('qerr').textContent = r.error;
    lastView = ''; tick();
  });
  const cont = $('cont');
  if (cont) cont.onclick = async () => {
    cont.disabled = true; const r = await post('/api/continue');
    if (r.error) $('cerr').textContent = r.error; lastView = ''; tick();
  };
}
async function tick() {
  if (busy) return; busy = true;
  try {
    st = await (await fetch('/api/state?since=' + since)).json();
    if (st.log.length) {
      const pre = $('log'), atEnd = pre.scrollTop + pre.clientHeight
        >= pre.scrollHeight - 30;
      pre.textContent += st.log.join('\n') + '\n';
      if (pre.textContent.length > 400000)
        pre.textContent = pre.textContent.slice(-300000);
      if (atEnd) pre.scrollTop = pre.scrollHeight;
      const lines = st.log.filter(l => l.trim());
      if (lines.length) $('last').textContent = lines[lines.length - 1].trim();
    }
    since = st.next;
    drawSteps();
    const key = st.step + '|' + JSON.stringify(st.question) + '|' + st.stage1
      + '|' + JSON.stringify(st.result) + '|' + st.error;
    if (key !== lastView) {
      const app = $('app'), pw = $('pw');
      const keep = app ? {a: app.checked, p: pw.value} : null;
      $('view').innerHTML = viewFor(); bind(); lastView = key;
      if (keep && $('app')) { $('app').checked = keep.a; $('pw').value = keep.p;
        $('pw').dispatchEvent(new Event('input')); }
    }
  } catch (e) {
    $('last').textContent = 'The installer page is not answering (it stops by'
      + ' itself 15 minutes after the install, or the Pi is restarting).';
  }
  busy = false;
}
setInterval(tick, 1000); tick();
</script></body></html>
"""


def make_handler(inst):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass
        def _send(self, code, body, ctype='application/json'):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == '/':
                self._send(200, PAGE, 'text/html')
            elif u.path == '/api/state':
                since = int(parse_qs(u.query).get('since', ['0'])[0] or 0)
                self._send(200, json.dumps(inst.state(since)))
            else:
                self._send(404, '{"error": "not found"}')
        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(n) or b'{}')
            except ValueError:
                body = {}
            path = urlparse(self.path).path
            if path == '/api/start':
                err = inst.start(body.get('appendix'), body.get('klipperscreen'),
                                 body.get('password') or '')
            elif path == '/api/answer':
                err = inst.answer(str(body.get('id')), str(body.get('value', '')))
            elif path == '/api/continue':
                err = inst.resume()
            elif path == '/api/quit':
                # a newer page (started from Mainsail) takes the port over -
                # only from this Pi itself, and never during an install
                with inst.lock:
                    busy = inst.proc is not None
                if self.client_address[0] not in ('127.0.0.1', '::1'):
                    err = "only from the Pi itself"
                elif busy:
                    err = "an install is running"
                else:
                    err = None
                    inst.quit_requested = True
            else:
                return self._send(404, '{"error": "not found"}')
            self._send(200, json.dumps({'ok': not err, 'error': err}))
    return Handler


def listen(port, inst):
    """The server, or None (an install is running in an older page - leave
    it), or False (the port belongs to something else).  Owner 10/10: a page
    started from Mainsail while an older, idle one still lingered (15 min
    after an install) failed with "Address already in use"."""
    for attempt in range(2):
        try:
            return http.server.ThreadingHTTPServer(('', port),
                                                   make_handler(inst))
        except OSError as e:
            if e.errno != errno.EADDRINUSE or attempt:
                print("cannot listen on port %d: %s" % (port, e))
                return False
        base = 'http://127.0.0.1:%d' % (port,)
        try:
            old = json.load(urllib.request.urlopen(base + '/api/state',
                                                   timeout=3))
            assert 'step' in old
        except Exception:
            print("port %d is used by another program" % (port,))
            return False
        if old.get('running'):
            print("an install is running in the installer page already -"
                  " open port %d" % (port,))
            return None
        req = urllib.request.Request(base + '/api/quit', b'{}',
                                     {'Content-Type': 'application/json'})
        json.load(urllib.request.urlopen(req, timeout=3))
        print("an older, idle installer page was on port %d - taking over"
              % (port,))
        for i in range(30):              # its main loop checks every 5 s
            time.sleep(0.5)
            try:
                urllib.request.urlopen(base + '/api/state', timeout=1)
            except Exception:
                break
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kxl-dir', required=True)
    ap.add_argument('--port', type=int, default=8099)
    a = ap.parse_args()
    inst = Installer(os.path.abspath(os.path.expanduser(a.kxl_dir)))
    # never leave the temporary sudo rule behind
    atexit.register(inst.sudo_revoke)
    def on_signal(signum, frame):
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    srv = listen(a.port, inst)
    if srv is None:
        return 0
    if srv is False:
        return 1
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("KlipperXL-Plus installer page on port %d" % (a.port,))
    sys.stdout.flush()
    while True:
        time.sleep(5)
        with inst.lock:
            started = inst.step != 'welcome'
            running = inst.proc is not None
            finished = inst.finished
        if finished and time.time() - finished > DONE_EXIT:
            break
        if not started and time.time() - inst.created > IDLE_EXIT:
            break
        if not running and inst.step in ('failed', 'flash') \
                and time.time() - inst.created > IDLE_EXIT * 2:
            break
        if inst.quit_requested:
            print("a newer installer page took over port %d" % (a.port,))
            break
    srv.shutdown()
    srv.server_close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
