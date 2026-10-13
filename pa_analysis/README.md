# pa_analysis - the Pressure Advance analyser

The off-printer half of KlipperXL+'s automatic per-tool Pressure Advance
(released KlipperXL @1b681b3, unchanged).  It runs as a separate process in its
own virtual environment, never inside Klipper.

```
pa_analysis/
  pa_analyze.py         KlipperXL adapter: CSV in, /tmp/pa_result.json out   GPL-3.0
  requirements.txt      numpy + scipy
  prusa_pa_tuner/       the analysis mathematics, CNC Kitchen PrusaPATuner   AGPL-3.0
    LICENSE, NOTICE     licence and attribution - read NOTICE
```

`prusa_pa_tuner/` is Stefan Hermann's (CNC Kitchen)
[PrusaPATuner](https://github.com/CNCKitchen/PrusaPATuner), vendored unmodified
so KlipperXL+ stays buildable if the upstream repository disappears.

## Why its own venv
- scipy must never be able to disturb klippy-env, which runs the printer;
- the AGPL code runs as a detached subprocess and is never imported into Klipper.

## Install (the KlipperXL+ installer does this)
```bash
cp -r ~/KlipperXL-plus/pa_analysis ~/pa_analysis
python3 -m venv ~/pa_analysis_env
~/pa_analysis_env/bin/pip install -r ~/pa_analysis/requirements.txt
```
`[xl_pa_tuner]` finds both by default (`analysis_python`, `analysis_script`).

## By hand
Every sweep leaves a timestamped CSV (`/tmp/pa_recording_T<n>_<stamp>.csv`):
```bash
~/pa_analysis_env/bin/python ~/pa_analysis/pa_analyze.py /tmp/pa_recording_T1_20261006-171500.csv
```
It prints a summary and writes `/tmp/pa_result.json`.  The CSV header carries
the sweep parameters as `# param NAME=VALUE`, so the analysis plan is rebuilt
from what the machine actually ran.
