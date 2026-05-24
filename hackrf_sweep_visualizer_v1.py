#!/usr/bin/env python3
"""
HackRF SDR Master Console — Standalone Edition
Created by sohmee & Google AI Studio Build
Requirements: pip3 install flask flask-sock numpy (optional but highly recommended)
              sudo apt install hackrf lsof psmisc
Run local server: python3 hackrf_sweep_visualizer.py
Then navigate to http://localhost:8085 in your web browser.
"""
import subprocess, json, time, os, signal, threading, queue, math, struct
from flask import Flask, render_template_string
from flask_sock import Sock

app  = Flask(__name__)
sock = Sock(app)
MY_PID = os.getpid()

rf_proc   = None
rf_lock   = threading.Lock()
rf_mode   = "sweep" 
rf_gen    = 0

stdout_q  = queue.Queue(maxsize=5000)
stderr_q  = queue.Queue(maxsize=1000)

class RFSimulator:
    def __init__(self):
        self.active_carriers = [
            # FM Radio Broadcast Peaks (87.5 - 108 MHz)
            {"freq": 88.5, "width": 0.3, "power": -45, "label": "BBC Radio 2", "modulation_speed": 1.5},
            {"freq": 91.3, "width": 0.25, "power": -52, "label": "Classic FM", "modulation_speed": 0.8},
            {"freq": 95.8, "width": 0.3, "power": -38, "label": "Capital FM (High Power)", "modulation_speed": 2.2},
            {"freq": 98.1, "width": 0.25, "power": -60, "label": "BBC Radio 1", "modulation_speed": 1.1},
            {"freq": 101.4, "width": 0.2, "power": -68, "label": "Heart Radio", "modulation_speed": 1.9},
            {"freq": 105.8, "width": 0.3, "power": -42, "label": "Absolute Radio", "modulation_speed": 2.5},

            # Aviation VHF AM Peaks (118 - 137 MHz)
            {"freq": 120.8, "width": 0.05, "power": -72, "label": "Tower AM Control", "modulation_speed": 0.2},
            {"freq": 124.55, "width": 0.04, "power": -65, "label": "Approach Control AM", "modulation_speed": 0.1},
            {"freq": 128.2, "width": 0.05, "power": -80, "label": "ATIS Weather Broadcast", "modulation_speed": 0.4},

            # Amateur VHF / 2m Band (144 - 148 MHz)
            {"freq": 144.3, "width": 0.015, "power": -82, "label": "Ham CW Beacon", "modulation_speed": 3.0},
            {"freq": 145.8, "width": 0.025, "power": -74, "label": "ISS Downlink (FM Voice)", "modulation_speed": 0.7},

            # Marine VHF (156 - 162 MHz)
            {"freq": 156.8, "width": 0.03, "power": -70, "label": "Marine Channel 16 Safety", "modulation_speed": 1.2},

            # Cellular GSM (935 MHz, 1090 MHz)
            {"freq": 935.2, "width": 0.4, "power": -48, "label": "GSM-900 Cell Tower BCCH", "modulation_speed": 2.0},
            {"freq": 1090.0, "width": 0.1, "power": -40, "label": "ADS-B Mode-S Transponder", "modulation_speed": 5.0}
        ]

    def generate_sweep_data(self, start, end, lna=28, vga=44, amp=0, binwidth=100000):
        lines = []
        sweep_range = end - start
        hz_start = start * 1e6
        hz_end = end * 1e6

        gain_factor = (lna - 28) * 0.4 + (vga - 44) * 0.2 + (11 if amp else 0)
        noise_center = -102 + (6 if amp else 0) + (lna / 4.0)

        chunk_count = int(math.ceil(sweep_range / 20.0))
        for c in range(chunk_count):
            chunk_start_hz = hz_start + c * 20 * 1e6
            chunk_end_hz = min(hz_end, chunk_start_hz + 20 * 1e6)
            if chunk_start_hz >= hz_end:
                break

            chunk_range = chunk_end_hz - chunk_start_hz
            num_bins = max(8, int(round(chunk_range / binwidth)))
            bin_hz = chunk_range / num_bins

            bins_array = []
            now_ts = time.time()
            date_str = time.strftime("%Y-%m-%d", time.localtime(now_ts))
            time_str = time.strftime("%H:%M:%S", time.localtime(now_ts)) + f".{int((now_ts % 1) * 1000000):06d}"

            for b in range(num_bins):
                bin_center_hz = chunk_start_hz + (b + 0.5) * bin_hz
                bin_center_mhz = bin_center_hz / 1e6

                import random
                bin_val = noise_center + math.sin(bin_center_mhz / 5.0) * 2.0 + random.uniform(-2.5, 2.5)

                for carrier in self.active_carriers:
                    mhz_dist = abs(bin_center_mhz - carrier["freq"])
                    if mhz_dist < carrier["width"] * 2.0:
                        mod_amp = math.sin(now_ts * carrier["modulation_speed"]) * 3.0 if carrier["modulation_speed"] else 0
                        dev_factor = carrier["width"] / 2.0
                        height = (carrier["power"] + gain_factor - noise_center) + mod_amp
                        if height > 0:
                            peak_strength = height * math.exp(-((mhz_dist / dev_factor) ** 2))
                            if peak_strength > 0:
                                bin_val += peak_strength

                bins_array.append(f"{bin_val:.2f}")

            csv_line = f"{date_str}, {time_str}, {int(chunk_start_hz)}, {int(chunk_end_hz)}, {int(binwidth)}, {num_bins}, {', '.join(bins_array)}"
            lines.append(csv_line)
        return lines

def _drain_stdout(proc, gen):
    try:
        for line in proc.stdout:
            line = line.rstrip('\n')
            if not line: continue
            with rf_lock:
                cur = rf_gen
            if gen != cur:
                break
            try: stdout_q.put_nowait(line)
            except queue.Full: pass
    except Exception: pass

def _drain_stderr(proc, gen):
    try:
        for line in proc.stderr:
            line = line.rstrip()
            if not line: continue
            with rf_lock:
                cur = rf_gen
            if gen != cur: break
            if "sweeps/second" not in line and "sweeps completed" not in line:
                print(f"[hackrf] {line}")
            try: stderr_q.put_nowait(line)
            except queue.Full: pass
    except Exception: pass

IS_ROOT = False
try:
    IS_ROOT = (os.geteuid() == 0)
except AttributeError:
    pass

def kill_hackrf_users():
    if os.name == 'nt':
        try: subprocess.run(["taskkill", "/F", "/IM", "hackrf_sweep.exe"], capture_output=True, timeout=1.0)
        except Exception: pass
        return
    cmd_prefix = ["sudo"] if IS_ROOT else []
    try:
        subprocess.run(cmd_prefix + ["killall", "-9", "hackrf_sweep", "hackrf_transfer"], capture_output=True, timeout=1.0)
    except Exception:
         pass
    try:
        r = subprocess.run(cmd_prefix + ["lsof", "-t", "/dev/hackrf0"], capture_output=True, text=True, timeout=1.0)
        if r and r.stdout:
            for ps in r.stdout.strip().splitlines():
                try:
                    pid = int(ps.strip())
                    if pid != MY_PID: os.kill(pid, signal.SIGKILL)
                except Exception: pass
    except Exception:
        pass
    try:
        subprocess.run(cmd_prefix + ["fuser", "-k", "/dev/hackrf0"], capture_output=True, timeout=1.0)
    except Exception:
        pass
    time.sleep(0.1)

def free_port(port):
    if os.name == 'nt':
        return
    cmd_prefix = ["sudo"] if IS_ROOT else []
    try:
        r = subprocess.run(cmd_prefix + ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, timeout=1.0)
        if r and r.stdout:
            for ps in r.stdout.strip().splitlines():
                try:
                    pid = int(ps.strip())
                    if pid != MY_PID: os.kill(pid, signal.SIGKILL)
                except Exception: pass
    except Exception:
        pass
    try:
        subprocess.run(cmd_prefix + ["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=1.0)
    except Exception:
        pass
    time.sleep(0.1)

def stop_rf():
    global rf_proc, rf_gen
    with rf_lock:
        proc = rf_proc
        rf_proc = None
        rf_gen += 1          
    if proc:
        try: proc.kill(); proc.wait(timeout=1.0)
        except Exception: pass
    
    # Empty stale queues
    for q in (stdout_q, stderr_q):
        try:
            while True: q.get_nowait()
        except queue.Empty: pass

def start_sweep(start, end, lna=28, vga=44, amp=0, binwidth=100000):
    global rf_proc, rf_gen, rf_mode
    start    = max(1,    min(5980, int(start)))
    end      = max(21,   min(6000, int(end)))
    lna      = max(0,    min(40,   int(lna)))
    vga      = max(0,    min(62,   int(vga)))
    amp      = 1 if int(amp) else 0
    binwidth = max(10000, min(1000000, int(binwidth)))

    stop_rf()
    kill_hackrf_users()
    rf_mode = "sweep"

    cmd = ["hackrf_sweep", 
           "-f", f"{start}:{end}", 
           "-l", str(lna), "-g", str(vga), 
           "-w", str(binwidth)]
    if amp: 
        cmd += ["-a", "1"]
    
    print(f"[sweep] launching hardware: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        with rf_lock:
            gen = rf_gen
            rf_proc = proc
        threading.Thread(target=_drain_stdout, args=(proc, gen), daemon=True).start()
        threading.Thread(target=_drain_stderr, args=(proc, gen), daemon=True).start()
    except Exception as e:
        print(f"[error] hackrf_sweep start failed: {e}")
        proc = None
        with rf_lock:
            rf_proc = None
    return proc, start, end

@sock.route('/ws')
def ws_route(ws):
    cur_start, cur_end = 88, 108
    cur_lna, cur_vga, cur_amp, cur_binwidth = 28, 44, 0, 100000 # Default LNA / VGA set to 70%!
    proc, cur_start, cur_end = start_sweep(cur_start, cur_end, lna=cur_lna, vga=cur_vga)

    def send_status(level, msg):
        try: ws.send(json.dumps({"type":"status","level":level,"msg":msg}))
        except Exception: pass

    def send_range(s, e):
        try: ws.send(json.dumps({"type":"range","start":s,"end":e}))
        except Exception: pass

    last_stderr_flush = time.time()
    last_data_time    = time.time()
    
    if proc is None:
        send_status("warn", "SDR Hardware is not detected on USB or failed to start. Falling back to clean live spectrum simulation.")
        last_data_time = 0
    else:
        send_status("info", f"hackrf_sweep visualizer active: Scannable bounds {cur_start}–{cur_end} MHz")
    send_range(cur_start, cur_end)
    
    stderr_line_count = 0 
    is_simulating     = False
    sim_instance      = RFSimulator()

    try:
        while True:
            try:
                msg = ws.receive(timeout=0.01)
            except Exception:
                msg = None

            if msg:
                try:
                    d = json.loads(msg)
                    cmd = d.get("cmd")
                    if cmd == "setSweep":
                        s = max(1,   min(5980, int(float(d.get("start", 88)))))
                        e = max(21,  min(6000, int(float(d.get("end",  108)))))
                        cur_lna = int(d.get("lna", 28))
                        cur_vga = int(d.get("vga", 44))
                        cur_amp = int(d.get("amp", 0))
                        cur_binwidth = int(d.get("binwidth", 100000))
                        proc, cur_start, cur_end = start_sweep(s, e,
                            lna=cur_lna, vga=cur_vga,
                            amp=cur_amp, binwidth=cur_binwidth)
                        send_status("info", f"Confirmed Sweep Range Config: {cur_start}–{cur_end} MHz")
                        send_range(cur_start, cur_end)
                        last_data_time = time.time() if proc else 0
                        is_simulating = False
                except Exception as e2:
                    send_status("error", f"Command Parse Error: {e2}")

            now = time.time()
            if now - last_stderr_flush > 0.4:
                last_stderr_flush = now
                msgs = []
                try:
                    while True: msgs.append(stderr_q.get_nowait())
                except queue.Empty: pass
                for ln in msgs:
                    if "sweeps/second" in ln or "sweeps completed" in ln:
                        stderr_line_count += 1
                        if stderr_line_count % 35 != 0:
                            continue
                    lvl = "error" if any(w in ln.lower() for w in ["error","fail","unable","no device","board"]) else "warn"
                    send_status(lvl, f"hackrf: {ln}")

            sent = 0
            try:
                while sent < 120:
                    line = stdout_q.get_nowait()
                    try: ws.send(line + '\n')
                    except Exception: return
                    sent += 1
                    last_data_time = time.time()
                    is_simulating = False
            except queue.Empty: pass

            if sent == 0 and (time.time() - last_data_time) > 2.0:
                if not is_simulating:
                    is_simulating = True
                    send_status("info", "Hardware standby: Live simulation system started.")
                
                sim_lines = sim_instance.generate_sweep_data(
                    cur_start, cur_end,
                    lna=cur_lna, vga=cur_vga, amp=cur_amp, binwidth=cur_binwidth
                )
                for line in sim_lines:
                    try: ws.send(line + '\n')
                    except Exception: return
                time.sleep(0.08)  # prevent tight cpu spinning on active mock dispatch
            else:
                time.sleep(0.005)
    finally:
        print("[server] WebSocket closed. Terminating active scan...")
        stop_rf()

# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE FRONTEND HTML (Optimized with high fidelity visual assets)
# ─────────────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>HackRF SDR Console — Premium Sweep Visualizer</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#05070a;--panel:#0a0f1d;--b2:#17223b;
  --acc:#00ff88;--a2:#00d4ff;--dim:#718da9;--text:#d8f0d8;--danger:#ff4455;--warn:#f59e0b;
}
body{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;
     font-size:14px;display:flex;flex-direction:column;height:100vh;overflow:hidden;
     user-select:none}

/* ── TOP BAR ── */
#topbar{display:flex;align-items:center;gap:16px;background:var(--panel);
        border-bottom:2px solid var(--b2);flex-shrink:0;height:115px;padding:0 20px;overflow-x:auto}
#topbar::-webkit-scrollbar{height:4px}
#topbar::-webkit-scrollbar-thumb{background:var(--b2)}

#brand{display:flex;flex-direction:column;gap:4px;flex-shrink:0}
#brand-main{display:flex;align-items:center;gap:8px}
#sdot{width:12px;height:12px;border-radius:50%;background:#ef4444;box-shadow:0 0 8px #ef4444}
#sdot.ok{background:var(--acc);box-shadow:0 0 10px var(--acc);animation:ping 1.5s infinite alternate}
#brand-text{font-family:'Orbitron',sans-serif;font-size:18px;font-weight:900;
            color:var(--acc);letter-spacing:1px;text-shadow:0 0 10px rgba(0,255,136,0.35)}

@keyframes ping {
  0% { transform: scale(0.92); opacity: 0.8; }
  100% { transform: scale(1.15); opacity: 1; }
}

.tsep{width:2px;background:var(--b2);align-self:stretch;margin:10px 0;flex-shrink:0}

/* Frequency Digit Dials - Massive Premium Version */
#dials-wrapper{display:flex;align-items:center;gap:18px}
.dial-box{display:flex;flex-direction:column;align-items:center;background:#030509;border:2px solid var(--b2);padding:6px 14px;border-radius:12px;box-shadow:0 6px 20px rgba(0,0,0,0.5)}
.dial-box-title{font-size:9px;font-family:'Orbitron',sans-serif;font-weight:900;color:var(--dim);letter-spacing:1px;margin-bottom:4px;text-transform:uppercase}
.dial-row{display:flex;gap:6px}
.dial-col{display:flex;flex-direction:column;align-items:center}
.dial-btn{width:24px;height:20px;background:#0b101c;border:1.5px solid #1c2a42;color:#94a3b8;font-size:11px;font-weight:900;border-radius:4px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all 0.1s}
.dial-btn:hover{border-color:var(--acc);color:var(--acc);background:#131c2d}
.dial-val{font-family:'Share Tech Mono',monospace;font-size:38px;font-weight:900;width:24px;text-align:center;line-height:1}

/* Keyboard / Typed inputs wrapper */
#keyboard-wrapper{display:none;align-items:center;gap:12px}
.input-field{display:flex;flex-direction:column;gap:2px;background:#030509;border:2px solid var(--b2);padding:6px 12px;border-radius:10px}
.input-field label{font-size:9px;color:var(--dim);font-weight:bold;text-transform:uppercase;font-family:'Orbitron',sans-serif}
.freq-inp{width:110px;height:30px;background:transparent;border:none;color:var(--acc);outline:none;font-family:'Orbitron',sans-serif;font-size:18px;font-weight:900;text-align:center}

#toggle-input-btn{height:54px;width:75px;background:#05070a;border:2px solid var(--b2);color:var(--dim);font-family:'Orbitron',sans-serif;font-size:10px;font-weight:900;letter-spacing:1px;cursor:pointer;border-radius:10px;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;transition:all 0.15s}
#toggle-input-btn.act{border-color:purple;color:orchid;background:rgba(128,0,128,0.1);box-shadow:0 0 10px rgba(128,0,128,0.25)}
#toggle-input-btn:hover{color:#fff;border-color:var(--dim)}

/* Top sliders & action buttons */
#pause-sweep-btn{height:54px;padding:0 14px;background:transparent;border:2px solid var(--b2);color:var(--dim);font-family:'Orbitron',sans-serif;font-size:10px;font-weight:900;border-radius:10px;cursor:pointer;transition:all 0.1s;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px}
#pause-sweep-btn.act{border-color:var(--warn);color:var(--warn);background:rgba(245,158,11,0.12);box-shadow:0 0 12px rgba(245,158,11,0.25)}
#pause-sweep-btn:hover:not(.act){border-color:#fff;color:#fff}

#force-sweep-btn{height:54px;padding:0 14px;background:transparent;border:2px solid var(--acc);color:var(--acc);font-family:'Orbitron',sans-serif;font-size:10px;font-weight:900;border-radius:10px;cursor:pointer;transition:all 0.1s;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px}
#force-sweep-btn:hover{background:var(--acc);color:#000;box-shadow:0 0 14px rgba(0,255,136,0.35)}

#factory-default-btn{height:54px;padding:0 14px;background:transparent;border:2px solid var(--danger);color:var(--danger);font-family:'Orbitron',sans-serif;font-size:9px;font-weight:900;border-radius:10px;cursor:pointer;transition:all 0.15s;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;box-shadow:0 0 8px rgba(255,68,85,0.15)}
#factory-default-btn:hover{border-color:#fff;color:#fff;background:rgba(255,68,85,0.15);box-shadow:0 0 15px rgba(255,68,85,0.35)}

.ts-col{display:flex;flex-direction:column;background:#030509;border:2px solid var(--b2);border-radius:10px;padding:6px 12px;height:54px;justify-content:center}
.ts-header{display:flex;justify-content:space-between;font-size:9px;color:var(--dim);font-weight:black;letter-spacing:1px;margin-bottom:4px;font-family:'Orbitron',sans-serif}
.ts-header .val{color:var(--a2);font-weight:bold}
.ts-row{display:flex;align-items:center;gap:8px}
.ts-row input[type=range]{width:80px;accent-color:var(--a2);cursor:pointer;height:5px}

/* Preamp AMP button style */
#preamp-btn{height:54px;padding:0 12px;background:transparent;border:2px solid var(--b2);color:var(--dim);font-family:'Orbitron',sans-serif;font-size:9px;font-weight:900;border-radius:10px;cursor:pointer;transition:all 0.15s;display:flex;justify-content:center;align-items:center}
#preamp-btn.act{border-color:var(--warn);color:var(--warn);background:rgba(245,158,11,0.1);box-shadow:0 0 8px rgba(245,158,11,0.2)}

/* ── QUICK BANDS SECTION ── */
#quick-bands-container {
  display: flex;
  flex-direction: column;
  background: #080d1a;
  border-bottom: 2px solid var(--b2);
  flex-shrink: 0;
  padding: 4px 0;
}
.quick-bands-row {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 34px;
  padding: 0 16px;
  overflow-x: auto;
  white-space: nowrap;
}
.quick-bands-row::-webkit-scrollbar { height: 2px; }
.quick-bands-row::-webkit-scrollbar-thumb { background: var(--b2); }
.qtitle {
  font-family: 'Orbitron', sans-serif;
  font-size: 9px;
  font-weight: 900;
  color: var(--dim);
  letter-spacing: 1.5px;
  margin-right: 6px;
}
.qpill {
  padding: 4px 12px;
  border-radius: 12px;
  border: 1.5px solid var(--b2);
  background: #030509;
  color: #94a3b8;
  font-family: 'Share Tech Mono', monospace;
  font-size: 11px;
  cursor: pointer;
  transition: all 0.1s ease;
  white-space: nowrap;
}
.qpill:hover { border-color: var(--acc); color: #fff; background: rgba(0, 255, 136, 0.05); }
.qpill.act { border-color: var(--acc); color: var(--acc); background: rgba(0, 255, 136, 0.12); }


/* ── BODY ROW ── */
#body-row{display:flex;flex:1;overflow:hidden;min-height:0}

/* Panels */
#left-panel, #right-panel{width:220px;flex-shrink:0;background:var(--panel);
              display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden}
#left-panel{border-right:2px solid var(--b2)}
#right-panel{border-left:2px solid var(--b2)}

/* scrollbars */
#left-panel::-webkit-scrollbar, #right-panel::-webkit-scrollbar{width:4px}
#left-panel::-webkit-scrollbar-thumb, #right-panel::-webkit-scrollbar-thumb{background:var(--b2)}

.ps{padding:12px;border-bottom:1px solid var(--b2)}
.pst{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:900;
     color:var(--dim);letter-spacing:1.5px;margin-bottom:8px;text-transform:uppercase}

.btn-opt{width:100%;height:32px;margin-bottom:6px;background:transparent;
         border:1.5px solid var(--b2);color:var(--dim);font-family:'Orbitron',sans-serif;
         font-size:10px;font-weight:900;letter-spacing:1px;cursor:pointer;border-radius:6px;transition:all 0.15s;
         display:flex;align-items:center;justify-content:center;gap:4px}
.btn-opt:hover{border-color:#fff;color:#fff}
.btn-opt.act{border-color:var(--acc);color:var(--acc);background:rgba(0,255,136,0.08)}
.btn-opt.cyan{border-color:var(--a2);color:var(--a2);background:rgba(0,212,255,0.08)}

.psel{width:100%;height:32px;background:#030509;border:1.5px solid var(--b2);
      color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:12px;
      border-radius:6px;outline:none;cursor:pointer;padding:0 6px;transition:border-color 0.15s}
.psel:focus{border-color:var(--a2);color:#fff}

/* Premium Sidebars Sliders Group */
.sidebar-slider-box {
  background: #040813;
  border: 1.5px solid var(--b2);
  border-radius: 8px;
  padding: 8px;
  margin-bottom: 8px;
}
.sidebar-slider-header {
  display: flex;
  justify-content: space-between;
  font-size: 8px;
  font-family: 'Orbitron', sans-serif;
  color: var(--dim);
  font-weight: bold;
  margin-bottom: 4px;
}
.sidebar-slider-input {
  width: 100%;
  accent-color: var(--acc);
  cursor: pointer;
  height: 4px;
}

/* Display Center Section */
#display{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;background:#030408}
#freq-ruler{height:30px;background:#05070f;border-bottom:1px solid var(--b2);flex-shrink:0}
#freq-ruler canvas{display:block;cursor:ew-resize}

#spectrum-wrap{height:190px;flex-shrink:0;position:relative;
               background:#020305;border-bottom:1px solid var(--b2)}
#spectrum-canvas{display:block;width:100%;height:100%}

#db-axis{position:absolute;left:4px;top:4px;bottom:4px;width:55px;
         display:flex;flex-direction:column;justify-content:space-between;
         padding:2px;color:rgba(113,141,169,0.7);font-size:9px;font-family:monospace;
         pointer-events:none;z-index:2;text-shadow:0 1px 2px #000}

#waterfall-wrap{flex:1;position:relative;overflow:hidden;cursor:crosshair}
#waterfall{display:block;position:absolute;left:0;top:0;width:100%;height:100%}

#marker-tip{position:absolute;background:#060f1e;border:1.5px solid var(--a2);
            color:#fff;font-size:11px;padding:4px 8px;border-radius:4px;
            pointer-events:none;display:none;z-index:20;white-space:nowrap;box-shadow:0 2px 10px rgba(0,0,0,0.5)}

/* Interactive Live coordinates overlay hover tooltip */
#live-tooltip {
  position: absolute;
  background: rgba(3, 7, 12, 0.9);
  border: 1.5px solid var(--acc);
  color: #fff;
  padding: 8px 12px;
  border-radius: 8px;
  font-family: 'Share Tech Mono', monospace;
  font-size: 12px;
  pointer-events: none;
  display: none;
  z-index: 9999;
  box-shadow: 0 4px 15px rgba(0, 0, 0, 0.75), 0 0 10px rgba(0, 255, 136, 0.25);
}
#lt-freq { font-family: 'Orbitron', sans-serif; font-weight: 900; color: var(--acc); font-size: 13px; }
#lt-db { color: var(--a2); font-weight: bold; margin-top: 2px; }
#lt-peak { color: var(--warn); font-size: 10px; margin-top: 4px; font-weight: bold; }

/* Peak readout */
#peak-readout{font-family:'Orbitron',sans-serif;font-size:19px;font-weight:900;color:var(--acc);text-shadow:0 0 6px rgba(0,255,136,0.25);margin-top:2px}
#peak-freq{font-size:11px;color:var(--dim);margin-top:1px}

/* Pin logs list style */
#marker-list{display:flex;flex-direction:column;gap:5px;font-size:11px}
.marker-item{display:flex;justify-content:space-between;align-items:center;background:#030509;border:1px solid var(--b2);padding:4px 8px;border-radius:4px}
.marker-lbl{color:var(--a2);font-weight:bold}
.marker-del{cursor:pointer;color:var(--danger);font-weight:bold;font-size:11px;padding:0 2px}
.marker-del:hover{color:#ff7788}

/* Help list */
.help-key{color:#38bdf8;font-weight:bold}

/* ── INFO BAR ── */
#infobar{display:flex;align-items:center;background:var(--panel);
         border-top:2px solid var(--b2);flex-shrink:0;height:32px;padding:0 12px;overflow:hidden;font-size:11px}
.ib{padding-right:16px;margin-right:16px;border-right:1px solid rgba(23,34,59,0.5);display:flex;align-items:center;gap:4px}
.ib .ik{color:var(--dim);font-weight:bold;text-transform:uppercase}
.ib .iv{color:var(--a2);font-weight:bold}
#kbd-hint{margin-left:auto;color:#475569}

/* ── CONSOLE ── */
#console-wrap{background:#020306;border-top:2.5px solid var(--b2);flex-shrink:0;display:flex;flex-direction:column;height:100px;transition:height 0.15s}
#console-wrap.collapsed{height:20px}
#console-header{display:flex;align-items:center;gap:8px;padding:3px 12px;background:#060811;border-bottom:1px solid var(--b2);cursor:pointer;flex-shrink:0}
#console-header span{font-size:10px;font-family:'Orbitron',sans-serif;color:var(--dim);font-weight:bold;letter-spacing:1px}
#ctoggle{margin-left:auto;font-size:9px;color:var(--dim)}
#console-log{flex:1;overflow-y:auto;padding:3px 12px;font-size:11px;line-height:1.5;font-family:'Share Tech Mono',monospace}
#console-wrap.collapsed #console-log{display:none}
.log-info{color:#00ff88}.log-warn{color:#ffaa22}.log-error{color:#ef4444}

/* Help Modal */
.modal{display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.85);align-items:center;justify-content:center}
.modal.open{display:flex}
.mbox{background:#080b14;border:2px solid var(--b2);border-radius:10px;width:500px;max-width:96vw;padding:20px 24px;box-shadow:0 8px 32px rgba(0,0,0,0.85);font-size:12px;line-height:1.6}
.mbox h2{font-family:'Orbitron',sans-serif;color:var(--acc);font-size:14px;margin-bottom:12px;letter-spacing:1.5px}
.mbox h3{color:var(--a2);font-size:10px;letter-spacing:1px;margin:12px 0 4px;text-transform:uppercase;font-family:'Orbitron',sans-serif;border-bottom:1px solid var(--b2);padding-bottom:2px}
.mcl{float:right;background:transparent;border:1.5px solid var(--b2);color:var(--dim);padding:3px 8px;font-size:10px;border-radius:4px;cursor:pointer;font-family:'Orbitron',sans-serif;font-weight:bold}
.mcl:hover{border-color:var(--danger);color:#fff;background:rgba(239,68,68,0.1)}
.help-row{display:flex;justify-content:space-between;margin-bottom:3px;font-family:monospace}
</style>
</head>
<body>

<div id="topbar">
  <div id="brand">
    <div id="brand-main">
      <div id="sdot"></div>
      <span id="brand-text">⬡ HACKRF SDR</span>
    </div>
    <div style="font-size:9px;color:var(--dim);letter-spacing:1px;font-weight:bold;margin-top:2px;font-family:'Orbitron',sans-serif">MASTER MONITOR</div>
  </div>
  
  <div class="tsep"></div>

  <!-- STUNNING FREQUENCY CONTROLS (MASSIVE DIALS TYPE) -->
  <div id="dials-wrapper">
    <div class="dial-box" id="start-dial-container"></div>
    <div class="dial-box" id="end-dial-container"></div>
  </div>

  <div id="keyboard-wrapper">
    <div class="input-field">
      <label>Start Freq</label>
      <input type="number" class="freq-inp" id="start" value="88" min="1" max="5980">
    </div>
    <div class="input-field">
      <label>End Freq</label>
      <input type="number" class="freq-inp" id="end" value="108" min="6" max="6000">
    </div>
  </div>

  <button id="toggle-input-btn" onclick="toggleInputMode()" title="Switch between dial tickers and direct keyboard number writing">
    ⚙️ TYPE
  </button>

  <div class="tsep"></div>

  <!-- Sweep, Pause, & Re-added Factory Default buttons -->
  <button id="pause-sweep-btn" onclick="togglePause()" title="Temporarily freeze real-time sweep spectrogram streams">
    ⏸️ PAUSE
  </button>
  <button id="force-sweep-btn" onclick="sendSweepConfig()" title="Force instant parameters synchronization">
    ⚡ SWEEP
  </button>
  <button id="factory-default-btn" onclick="restoreFactoryDefault()" title="Restore whole original default parameters and frequencies">
    🔄 DEFAULT
  </button>

  <div class="tsep"></div>

  <!-- Real time hardware selectors -->
  <div class="ts-col">
    <div class="ts-header">
      <span>LNA GAIN</span>
      <span class="val" id="lna-val">28dB</span>
    </div>
    <div class="ts-row">
      <input type="range" id="lna" min="0" max="40" step="8" value="28" oninput="onGainSlider('lna',this.value)">
    </div>
  </div>

  <div class="ts-col">
    <div class="ts-header">
      <span>VGA GAIN</span>
      <span class="val" id="vga-val">44dB</span>
    </div>
    <div class="ts-row">
      <input type="range" id="vga" min="0" max="62" step="2" value="44" oninput="onGainSlider('vga',this.value)">
    </div>
  </div>

  <button id="preamp-btn" onclick="toggleAmp()" title="Enable or disable HackRF physical frontend low-noise pre-amplifier">
    AMP: OFF
  </button>

  <div class="tsep"></div>

  <button id="img-grab-btn" onclick="exportFramePNG()" title="Capture and save high-resolution spectrogram and waterfall visual charts to local disk" style="height:54px;padding:0 14px;background:transparent;border:2px solid #a855f7;color:#c084fc;font-family:'Orbitron',sans-serif;font-size:10px;font-weight:900;border-radius:10px;cursor:pointer;transition:all 0.15s;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px">
    📸 IMG GRAB
  </button>

  <div class="tsep"></div>

  <button id="toggle-bands-btn" onclick="toggleQuickBands()" title="Collapse or expand the 30 quick presets rows to optimize vertical screen layout space" style="height:54px;padding:0 14px;background:transparent;border:2px solid #f59e0b;color:#f59e0b;font-family:'Orbitron',sans-serif;font-size:10px;font-weight:900;border-radius:10px;cursor:pointer;transition:all 0.15s;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px">
    👁️ BANDS PANEL
  </button>

  <div class="tsep"></div>

  <button id="help-trigger-btn" onclick="openModal('help-modal')" title="How to run standalone, system requirements, troubleshooting FAQs and info specs" style="height:54px;padding:0 14px;background:transparent;border:2px solid #10b981;color:#10b981;font-family:'Orbitron',sans-serif;font-size:10px;font-weight:900;border-radius:10px;cursor:pointer;transition:all 0.15s;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px">
    ❓ HELP & SPECS
  </button>
</div>

<!-- HORIZONTAL PRESET CONTAINER (THREE ROWS) -->
<div id="quick-bands-container">
  <div class="quick-bands-row" id="quick-bands-row-1"></div>
  <div class="quick-bands-row" id="quick-bands-row-2"></div>
  <div class="quick-bands-row" id="quick-bands-row-3"></div>
</div>

<div id="body-row">
  <!-- LEFT SIDEBAR PANEL -->
  <div id="left-panel">
    <div class="ps">
      <div class="pst">Color Map</div>
      <select class="psel" id="visuals-palette-selector" onchange="setScheme(this.value)">
        <option value="classic">Classic Classic</option>
        <option value="viridis" selected>Viridis Spectrum</option>
        <option value="nightvision">Night Vision</option>
        <option value="hot">Thermal Hot</option>
        <option value="grayscale">Grayscale Mapping</option>
        <option value="aurora">Aurora Northern</option>
        <option value="cyberpunk">Cyberpunk Neon</option>
      </select>
    </div>

    <!-- RE-ADDED PREMIUM SLIDERS (MIN DB, MAX DB & SCROLL SPEED) -->
    <div class="ps">
      <div class="pst">Trace & Display Tweaks</div>
      
      <!-- Min DB Display bound slider -->
      <div class="sidebar-slider-box">
        <div class="sidebar-slider-header">
          <span>MIN DB FLOOR</span>
          <span id="mindb-lbl" style="color:var(--danger)">-110 dBm</span>
        </div>
        <input class="sidebar-slider-input" type="range" id="mindb-slider" min="-130" max="-45" step="5" value="-110" oninput="onMinDbSlider(this.value)">
      </div>

      <!-- Max DB Display bound slider -->
      <div class="sidebar-slider-box">
        <div class="sidebar-slider-header">
          <span>MAX DB CEILING</span>
          <span id="maxdb-lbl" style="color:var(--acc)">-20 dBm</span>
        </div>
        <input class="sidebar-slider-input" type="range" id="maxdb-slider" min="-60" max="10" step="5" value="-20" oninput="onMaxDbSlider(this.value)">
      </div>

      <!-- Waterfall accumulation scrolling rate speed slider -->
      <div class="sidebar-slider-box">
        <div class="sidebar-slider-header">
          <span>Waterfall Speed</span>
          <span id="wfspeed-lbl" style="color:var(--a2)">4x (Normal)</span>
        </div>
        <input class="sidebar-slider-input" type="range" id="wfspeed-slider" min="1" max="15" step="1" value="4" oninput="onWfSpeedSlider(this.value)">
      </div>
    </div>

    <div class="ps">
      <div class="pst">Traces Filters</div>
      <button class="btn-opt" id="trace-avg-btn" onclick="toggleAvg()">AVG: OFF</button>
      <button class="btn-opt" id="trace-peak-btn" onclick="togglePeak()">PEAK: OFF</button>
    </div>

    <div class="ps">
      <div class="pst">Calibration</div>
      <button class="btn-opt cyan" onclick="autoCalibrateFloor()" title="Calculate auto noise threshold floor level">
        ⚡ AUTO FLOOR
      </button>
    </div>

    <div class="ps">
      <div class="pst">Resolution</div>
      <select class="psel" id="bw-sel" onchange="onBinwidthSelector(this.value)">
        <option value="100000" selected>100 kHz (Optimal)</option>
        <option value="250000">250 kHz (Fast)</option>
        <option value="50000">50 kHz (Detail)</option>
        <option value="10000">10 kHz (Insane)</option>
      </select>
    </div>


  </div>

  <!-- CENTER PLOTS CONTAINER -->
  <div id="display">
    <div id="freq-ruler">
      <canvas id="ruler-canvas"></canvas>
    </div>

    <div id="spectrum-wrap">
      <div id="db-axis">
        <span>-20 dBm</span>
        <span>-50 dBm</span>
        <span>-80 dBm</span>
        <span>-110 dBm</span>
      </div>
      <canvas id="spectrum-canvas"></canvas>
    </div>

    <div id="waterfall-wrap">
      <canvas id="waterfall"></canvas>
      <div id="marker-tip"></div>
    </div>
  </div>

  <!-- RIGHT SIDEBAR PANEL -->
  <div id="right-panel">
    <div class="ps">
      <div class="pst">Live Telemetry</div>
      <div style="font-size:10px;color:var(--dim)">Max Power</div>
      <div id="peak-readout">---</div>
      <div id="peak-freq">---</div>
    </div>

    <div class="ps" style="flex:1">
      <div class="pst">Dropped Pins</div>
      <div id="marker-list"></div>
      <button class="btn-opt" id="clear-pins-btn" onclick="clearMarkers()" style="margin-top:10px;height:24px;border-color:var(--danger);color:var(--danger)">
        🗑️ Clear Pins
      </button>
    </div>

    <div class="ps">
      <button class="btn-opt" onclick="openModal('help-modal')" style="border-color:var(--dim);color:var(--dim)">
        ❓ SHORTCUTS HELP
      </button>
    </div>
  </div>
</div>

<!-- FLOATING CO-ORDINATES MOUSE OVER HOVER TOOLTIP OVERLAY -->
<div id="live-tooltip">
  <div id="lt-freq">-- MHz</div>
  <div id="lt-db">-- dBm</div>
  <div id="lt-peak" style="display:none">★ Peak label</div>
</div>

<!-- INFO BAR -->
<div id="infobar">
  <div class="ib">
    <span class="ik">Status:</span>
    <span class="iv" id="stxt">disconnected</span>
  </div>
  <div class="ib">
    <span class="ik">Sweep:</span>
    <span class="iv" id="info-rate">0</span> <span style="color:var(--dim)">S/s</span>
  </div>
  <div class="ib">
    <span class="ik">Scannable:</span>
    <span class="iv" id="info-span">0</span> <span style="color:var(--dim)">MHz</span>
  </div>
  <div class="ib">
    <span class="ik">Points:</span>
    <span class="iv" id="info-bins">0</span>
  </div>
  <span id="kbd-hint">Hover spectrogram/waterfall to track frequencies | Drag Ruler to Pan bounds</span>
</div>

<!-- CONSOLE DRAWER -->
<div id="console-wrap">
  <div id="console-header" onclick="toggleConsole()">
    <span>📟 TERMINAL EXPORTS LOGS</span>
    <span id="ctoggle">▼ COLLAPSE PANEL</span>
  </div>
  <div id="console-log"></div>
</div>

<!-- SYSTEM MANUAL & SHORTCUTS HELP MODAL -->
<div class="modal" id="help-modal" onclick="modalBackdrop(event,this)">
  <div class="mbox" style="width: 680px; max-width: 96vw; max-height: 85vh; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--b2) #080b14;">
    <button class="mcl" onclick="closeModal('help-modal')">CLOSE MANUAL</button>
    <h2 style="font-family:'Orbitron',sans-serif;color:var(--acc);font-size:15px;border-bottom:2px solid var(--b2);padding-bottom:6px">📡 HACKRF SDR SPECTRUM GENERAL USER MANUAL</h2>
    
    <h3>🚀 What is Everything and How it Works</h3>
    <ul style="padding-left:14px;color:rgba(216,240,216,0.85);margin-top:4px;list-style-type:square;space-y:2px">
      <li><b>Spectrum Grid Plot:</b> Real-time RF signal density (FFT trace). Displays signal spikes, carrier cycles, and current amplitude levels.</li>
      <li><b>Waterfall Spectrogram:</b> Moving history plot showing frequency power levels over time. Slide the <b>Waterfall Speed</b> slider to rate limit or speed up the rate.</li>
      <li><b>Interactive Frequency Ruler:</b> Click and drag the gray/blue frequency timeline at the top to pan and scan the active window.</li>
      <li><b>Dropped Markers:</b> Click on the spectrum or waterfall plots to pin target channels. Tracks peak values and labels.</li>
      <li><b>Auto Floor Limits:</b> Clicking this automatically aligns the minimum and maximum dB scaling offsets with surrounding ambient signals.</li>
      <li><b>Filter Options:</b> <i>Trace Averaging</i> filters noise out of traces, while <i>Peak Hold</i> keeps the absolute record maximum as an orange ghost trace.</li>
      <li><b>30 Quick Bands:</b> Allows immediate single-click jumping between 30 sorted physical bands (from HF up to 5GHz Wi-Fi lines), with a hide panel toggle.</li>
    </ul>

    <h3>📟 Interactive Keyboard Shortcuts</h3>
    <div class="help-row"><span class="help-key">Spacebar</span> <span>Pause / Resume real-time SDR hardware sweep stream</span></div>
    <div class="help-row"><span class="help-key">A</span> <span>Auto calibrate visual graph decibel (dB) limits</span></div>
    <div class="help-row"><span class="help-key">R / S</span> <span>Toggle Trace averages filter / Peak hold modes</span></div>
    <div class="help-row"><span class="help-key">M</span> <span>Completely clear all dropped pinpoint markings</span></div>
    
    <h3>⚙️ Physical Hardware Requirements</h3>
    <p style="color:var(--dim);margin-top:4px">
      To operate offline, you need a physical <b>Great Scott Gadgets HackRF One</b> SDR transceiver connected over USB, along with compatible antennas tuned to your target sweep frequencies.
    </p>

    <h3>🛠️ Stands & Core Dependencies</h3>
    <p style="color:var(--dim);margin-top:4px">
      The server runs on <b>Python 3</b>. It relies on system tools: <b>hackrf_sweep</b> (under CLI packages) for high-speed hardware control, <b>lsof</b> to balance socket endpoints, <b>psmisc</b> for background sweep process pruning, and <b>sox</b> for raw audio. Python libraries needed are <b>Flask</b> and <b>flask-sock</b>.
    </p>

    <h3>🖥️ Installation Guide Per Operating System</h3>
    
    <div style="background:#030509;border:1px solid var(--b2);padding:10px;border-radius:6px;margin-top:10px">
      <strong style="color:var(--acc);font-size:10px;font-family:'Orbitron',sans-serif">🐧 LINUX (Ubuntu, Debian, Kali, Raspberry Pi OS)</strong>
      <p style="color:var(--dim);margin-top:4px;font-size:11px">
        1. Install binary toolchains: <code style="color:#38bdf8">sudo apt update && sudo apt install -y hackrf python3-pip lsof psmisc sox</code><br>
        2. Install web libraries: <code style="color:#38bdf8">pip3 install flask flask-sock</code><br>
        3. Plug your HackRF One via USB, and verify discovery: <code style="color:#38bdf8">hackrf_info</code><br>
        4. Execute visualizer with physical device privileges: <code style="color:#00ff88">sudo python3 hackrf_sweep_visualizer.py</code>
      </p>
    </div>

    <div style="background:#030509;border:1px solid var(--b2);padding:10px;border-radius:6px;margin-top:10px">
      <strong style="color:#00d4ff;font-size:10px;font-family:'Orbitron',sans-serif font-weight:bold">🍏 MAC (macOS - Intel or Apple Silicon)</strong>
      <p style="color:var(--dim);margin-top:4px;font-size:11px">
        1. Open Mac Terminal, and ensure <b>Homebrew</b> is installed.<br>
        2. Install requirements: <code style="color:#38bdf8">brew install hackrf sox python</code><br>
        3. Pip install dependencies: <code style="color:#38bdf8">pip3 install flask flask-sock</code><br>
        4. Connect hardware, verify: <code style="color:#38bdf8">hackrf_info</code><br>
        5. Execute visualizer script: <code style="color:#00ff88">sudo python3 hackrf_sweep_visualizer.py</code>
      </p>
    </div>

    <div style="background:#030509;border:1px solid var(--b2);padding:10px;border-radius:6px;margin-top:10px">
      <strong style="color:#c084fc;font-size:10px;font-family:'Orbitron',sans-serif font-weight:bold">🪟 WINDOWS WSL (Subsystem for Linux)</strong>
      <p style="color:var(--dim);margin-top:4px;font-size:11px">
        1. Open PowerShell under Administrator privileges on Windows. Install USBIPD: <code style="color:#38bdf8">winget install usbipd</code><br>
        2. Identify HackRF Bus ID: <code style="color:#38bdf8">usbipd list</code>, bind with: <code style="color:#38bdf8">usbipd bind --busid &lt;bus-id&gt;</code><br>
        3. Mount card directly to WSL Linux virtual machine: <code style="color:#38bdf8">usbipd attach --wsl --busid &lt;bus-id&gt;</code><br>
        4. Inside WSL terminal (such as Ubuntu), install Linux dependencies & start visualizer: <code style="color:#00ff88">sudo apt update && sudo apt install -y hackrf python3-pip lsof psmisc && pip3 install flask flask-sock && sudo python3 hackrf_sweep_visualizer.py</code>
      </p>
    </div>

    <div style="background:#030509;border:1px solid var(--b2);padding:10px;border-radius:6px;margin-top:10px">
      <strong style="color:#f59e0b;font-size:10px;font-family:'Orbitron',sans-serif;font-weight:bold">🪟 WINDOWS (Native Python Environment)</strong>
      <p style="color:var(--dim);margin-top:4px;font-size:11px">
        1. Install official Python 3 on Windows (Tick the 'Add Python to PATH' setting during installation).<br>
        2. Download the Windows compiled HackRF host library binary archive from official Great Scott Gadgets resource releases. Add <code>hackrf_sweep.exe</code> to your Windows Environmental user environment variable PATH.<br>
        3. Execute pip modules: <code style="color:#38bdf8">pip install flask flask-sock</code> in Command Prompt.<br>
        4. Start visualizer dashboard: <code style="color:#00ff88">python hackrf_sweep_visualizer.py</code>
      </p>
    </div>

  </div>
</div>

<script>
// Spectrum States - Default LNA / VGA set to 70%!
let freqStart=88, freqEnd=108;
let lnaGain=28, vgaGain=44, ampActive=false, curBinwidth=1e5;
let sweepActive = true, isPaused=false, peakHold=false, useAvg=false;
let directInputMode=false;

let markers=[];

let ws=null;
let lineBuf="";
let sweepFreqLow=null, sweepFreqHigh=null;
let expectedStartHz=null;
let sweepBins=[];
let latestBins=null;

function resetSweepBuffers() {
  sweepFreqLow = null;
  sweepFreqHigh = null;
  sweepBins = [];
  expectedStartHz = null;
}

// Auto Floor variables
let dbMin=-110, dbMax=-20;
let wfSpeed=4; // Waterfall speed default rate limit

// Grid Drawing & Buffers
const rulerCanvas=document.getElementById('ruler-canvas');
const rulerCtx=rulerCanvas.getContext('2d');
const specCanvas=document.getElementById('spectrum-canvas');
const specCtx=specCanvas.getContext('2d');
const wfCanvas=document.getElementById('waterfall');
const wfCtx=wfCanvas.getContext('2d');

let peakBuf=null, avgBuf=null;
let wfAccumBuf=null, wfAccumCount=0;
let wfY=0;
let resizeTimer=null;

let rateCount=0, lastRateTs=Date.now();
const LOG_LIMIT=75;

// Color LUT Configurations
const SCHEMES={
  classic: [[0.0, [0, 0, 0]], [0.2, [0, 0, 120]], [0.5, [0, 190, 255]], [0.75, [255, 230, 0]], [1.0, [255, 60, 0]]],
  viridis: [[0.0, [68, 1, 84]], [0.25, [59, 82, 139]], [0.5, [33, 145, 140]], [0.75, [94, 201, 98]], [1.0, [253, 231, 37]]],
  nightvision: [[0.0, [3, 10, 3]], [0.3, [10, 48, 10]], [0.7, [24, 160, 70]], [1.0, [170, 240, 190]]],
  hot: [[0.0, [5, 5, 8]], [0.3, [130, 20, 20]], [0.6, [240, 110, 20]], [0.85, [230, 175, 8]], [1.0, [254, 254, 220]]],
  grayscale: [[0.0, [0,0,0]], [1.0, [255,255,255]]],
  aurora: [[0.0, [5, 2, 15]], [0.4, [10, 160, 120]], [0.7, [180, 20, 210]], [1.0, [240, 220, 100]]],
  cyberpunk: [[0.0, [2, 2, 8]], [0.3, [240, 0, 110]], [0.6, [0, 215, 255]], [0.9, [140, 0, 255]], [1.0, [255, 255, 255]]]
};

let LUT=buildLUT('viridis');

// QUICK BANDS PRESET ROW SYSTEM (42 ORDERED BANDS ACROSS 3 ROWS)
const PRESET_BANDS_1 = [
  { name: '📻 Ham 160m (HF)', s: 1, e: 2 },
  { name: '📡 Ham 80m (HF)', s: 3, e: 4 },
  { name: '📻 Ham 60m (HF)', s: 5, e: 6 },
  { name: '📡 Ham 40m (HF)', s: 7, e: 8 },
  { name: '📻 Ham 30m (HF)', s: 10, e: 11 },
  { name: '📡 Ham 20m (HF)', s: 14, e: 15 },
  { name: '📻 Ham 17m (HF)', s: 18, e: 19 },
  { name: '📡 Ham 15m (HF)', s: 21, e: 22 },
  { name: '📻 Ham 12m (HF)', s: 24, e: 25 },
  { name: '📻 CB Radio Band', s: 26, e: 28 },
  { name: '📡 Ham 10m (HF)', s: 28, e: 30 },
  { name: '📻 Ham 6m (VHF)', s: 50, e: 54 },
  { name: '📻 FM Broadcast', s: 88, e: 108 },
  { name: '✈ Aviation VHF', s: 118, e: 137 }
];

const PRESET_BANDS_2 = [
  { name: '🌤 NOAA Satellites', s: 136, e: 138 },
  { name: '📡 Ham VHF (2m)', s: 144, e: 148 },
  { name: '⚓ Marine VHF Voice', s: 156, e: 162 },
  { name: '📻 Land Mobile VHF', s: 162, e: 174 },
  { name: '📡 Ham 1.25m Band', s: 220, e: 225 },
  { name: '🚗 Key Fobs 315M', s: 312, e: 317 },
  { name: '🔒 Emergency TETRA', s: 380, e: 400 },
  { name: '🛰️ Weather Balloon', s: 400, e: 406 },
  { name: '📡 Ham UHF (70cm)', s: 430, e: 440 },
  { name: '🚗 Key Fobs 433M', s: 433, e: 435 },
  { name: '📻 PMR446 Handheld', s: 446, e: 447 },
  { name: '📻 Walkie FRS/GMRS', s: 462, e: 468 },
  { name: '📡 IoT LoRa/Sigfox', s: 868, e: 869 },
  { name: '📶 US ISM 915MHz', s: 902, e: 928 }
];

const PRESET_BANDS_3 = [
  { name: '📶 GSM Cell-Tower', s: 925, e: 945 },
  { name: '✈ ADS-B Aircraft', s: 1080, e: 1100 },
  { name: '🛰 GPS L5 Frequency', s: 1164, e: 1189 },
  { name: '🛰 GPS L2 Frequency', s: 1215, e: 1240 },
  { name: '📡 Ham 23cm Band', s: 1240, e: 1300 },
  { name: '🛰 GPS L1 Frequency', s: 1560, e: 1580 },
  { name: '🛰️ Iridium Sat', s: 1616, e: 1626 },
  { name: '📶 ISM 2.4G Band', s: 2400, e: 2500 },
  { name: '📶 Wi-Fi 2.4G Sweep', s: 2400, e: 2480 },
  { name: '📡 Amateur 13cm', s: 2390, e: 2450 },
  { name: '📶 Wi-Fi 5G Uni-1', s: 5150, e: 5250 },
  { name: '📶 Wi-Fi 5G Channels', s: 5160, e: 5360 },
  { name: '📡 Amateur 5cm', s: 5650, e: 5925 },
  { name: '🌍 Entire SDR Scope', s: 1, e: 5981 }
];

function renderQuickBands() {
  const row1 = document.getElementById('quick-bands-row-1');
  const row2 = document.getElementById('quick-bands-row-2');
  const row3 = document.getElementById('quick-bands-row-3');
  if(!row1 || !row2 || !row3) return;

  row1.innerHTML = `<span class="qtitle">⚡ ROWS 1 (HF):</span>`;
  PRESET_BANDS_1.forEach(b => {
    const isAct = Math.abs(freqStart - b.s) <= 2 && Math.abs(freqEnd - b.e) <= 2;
    const btn = document.createElement('button');
    btn.className = `qpill ${isAct ? 'act' : ''}`;
    btn.textContent = b.name;
    btn.onclick = () => {
      changeFreq(b.s, b.e);
    };
    row1.appendChild(btn);
  });

  row2.innerHTML = `<span class="qtitle">⚡ ROWS 2 (VHF):</span>`;
  PRESET_BANDS_2.forEach(b => {
    const isAct = Math.abs(freqStart - b.s) <= 2 && Math.abs(freqEnd - b.e) <= 2;
    const btn = document.createElement('button');
    btn.className = `qpill ${isAct ? 'act' : ''}`;
    btn.textContent = b.name;
    btn.onclick = () => {
      changeFreq(b.s, b.e);
    };
    row2.appendChild(btn);
  });

  row3.innerHTML = `<span class="qtitle">⚡ ROWS 3 (UHF/SHF):</span>`;
  PRESET_BANDS_3.forEach(b => {
    const isAct = Math.abs(freqStart - b.s) <= 2 && Math.abs(freqEnd - b.e) <= 2;
    const btn = document.createElement('button');
    btn.className = `qpill ${isAct ? 'act' : ''}`;
    btn.textContent = b.name;
    btn.onclick = () => {
      changeFreq(b.s, b.e);
    };
    row3.appendChild(btn);
  });
}

function buildLUT(name){
  const stops=SCHEMES[name]||SCHEMES.classic;
  const lut=new Uint8ClampedArray(256*3);
  for(let i=0;i<256;i++){
    const t=i/255;
    let lo=stops[0], hi=stops[stops.length-1];
    for(let s=0;s<stops.length-1;s++){
      if(t>=stops[s][0] && t<=stops[s+1][0]){lo=stops[s];hi=stops[s+1];break}
    }
    const f= (hi[0]-lo[0])===0 ? 0 : (t-lo[0])/(hi[0]-lo[0]);
    lut[i*3]  =Math.round(lo[1][0]+f*(hi[1][0]-lo[1][0]));
    lut[i*3+1]=Math.round(lo[1][1]+f*(hi[1][1]-lo[1][1]));
    lut[i*3+2]=Math.round(lo[1][2]+f*(hi[1][2]-lo[1][2]));
  }
  return lut;
}
function setScheme(name){ 
  LUT=buildLUT(name); 
}

// Smart visual frequency safe guard
function changeFreq(newStart, newEnd) {
  newStart = Math.max(1, Math.min(5980, Math.round(newStart)));
  newEnd = Math.max(6, Math.min(6000, Math.round(newEnd)));
  const minSpan = 5;

  if (newStart >= newEnd - minSpan) {
    if (newStart !== freqStart) { // start changed
      newEnd = newStart + minSpan;
      if (newEnd > 6000) {
        newEnd = 6000;
        newStart = newEnd - minSpan;
      }
    } else { // end changed
      newStart = newEnd - minSpan;
      if (newStart < 1) {
        newStart = 1;
        newEnd = newStart + minSpan;
      }
    }
  }
  
  freqStart = newStart;
  freqEnd = newEnd;
  
  resetSweepBuffers();
  syncDialsToInputs();
  sendSweepConfig();
  drawRuler();
  renderQuickBands();
}

// UI Input Helpers
function toggleInputMode(){
  directInputMode=!directInputMode;
  const dw=document.getElementById('dials-wrapper');
  const kw=document.getElementById('keyboard-wrapper');
  const btn=document.getElementById('toggle-input-btn');
  if(directInputMode){
    dw.style.display='none';
    kw.style.display='flex';
    btn.innerHTML='⚙️ DIALS';
    btn.classList.add('act');
  } else {
    dw.style.display='flex';
    kw.style.display='none';
    btn.innerHTML='✏️ TYPE';
    btn.classList.remove('act');
  }
}

function adjustDigitAt(type, idx, direction){
  const weights = [1000, 100, 10, 1];
  const weight = weights[idx];
  const offset = direction === 'up' ? weight : -weight;
  
  if(type === 'start'){
    changeFreq(freqStart + offset, freqEnd);
  } else {
    changeFreq(freqStart, freqEnd + offset);
  }
}

function syncDialsToInputs(){
  document.getElementById('start').value = freqStart;
  document.getElementById('end').value = freqEnd;
  renderDials();
}

function renderDials(){
  renderDigitSelector('start-dial-container', freqStart, 'start', '#00ff88');
  renderDigitSelector('end-dial-container', freqEnd, 'end', '#00d4ff');
}

function renderDigitSelector(containerId, value, type, color){
  const padded = Math.floor(value).toString().padStart(4, '0');
  const digits = padded.split('');
  const container = document.getElementById(containerId);
  if (!container) return;
  
  let html = `<div class="dial-box-title">${type === 'start' ? 'START FREQ' : 'END FREQ'}</div>`;
  html += '<div class="dial-row">';
  for (let i = 0; i < 4; i++) {
    html += `
      <div class="dial-col">
        <button class="dial-btn" onclick="adjustDigitAt('${type}', ${i}, 'up')">+</button>
        <span class="dial-val" style="color: ${color}; text-shadow: 0 0 8px ${color}45">${digits[i]}</span>
        <button class="dial-btn" onclick="adjustDigitAt('${type}', ${i}, 'down')">-</button>
      </div>
    `;
  }
  html += '</div>';
  container.innerHTML = html;
}

// Controls handlers
function togglePause(){
  isPaused=!isPaused;
  const btn=document.getElementById('pause-sweep-btn');
  btn.classList.toggle('act', isPaused);
  logMsg('info', isPaused ? 'Sweep spectrum feeds paused.' : 'Spectrum feeds active.');
}

function toggleAvg(){
  useAvg=!useAvg;
  const btn=document.getElementById('trace-avg-btn');
  btn.classList.toggle('act', useAvg);
  btn.textContent=useAvg ? 'AVG: ON' : 'AVG: OFF';
}

function togglePeak(){
  peakHold=!peakHold;
  const btn=document.getElementById('trace-peak-btn');
  btn.classList.toggle('act', peakHold);
  btn.textContent=peakHold ? 'PEAK: ON' : 'PEAK: OFF';
}

function toggleAmp(){
  ampActive=!ampActive;
  const btn=document.getElementById('preamp-btn');
  btn.classList.toggle('act', ampActive);
  btn.textContent=ampActive ? 'AMP: ON' : 'AMP: OFF';
  sendSweepConfig();
}

function onGainSlider(id, v){
  document.getElementById(id+'-val').textContent=v+'dB';
  if(id==='lna') lnaGain=parseInt(v);
  if(id==='vga') vgaGain=parseInt(v);
  sendSweepConfig();
}

// Side sliders adjustments
function onMinDbSlider(val) {
  dbMin = parseInt(val);
  document.getElementById('mindb-lbl').textContent = dbMin + ' dBm';
  updateDbAxis();
}

function onMaxDbSlider(val) {
  dbMax = parseInt(val);
  document.getElementById('maxdb-lbl').textContent = dbMax + ' dBm';
  updateDbAxis();
}

function onWfSpeedSlider(val) {
  wfSpeed = parseInt(val);
  document.getElementById('wfspeed-lbl').textContent = wfSpeed === 1 ? '1x (Peak Speed)' : wfSpeed + 'x slower';
}

function onBinwidthSelector(v){
  curBinwidth=parseInt(v);
  logMsg('info', `Sweep step binwidth updated to ${v/1000} kHz`);
  sendSweepConfig();
}

function restoreFactoryDefault() {
  freqStart = 88;
  freqEnd = 108;
  lnaGain = 28; // set default LNA to 70% (28 out of 40 dBm)
  vgaGain = 44; // set default VGA to 70% (44 out of 62 dBm)
  ampActive = false;
  curBinwidth = 100000;
  dbMin = -110;
  dbMax = -20;
  wfSpeed = 4;
  isPaused = false;
  peakHold = false;
  useAvg = false;
  markers = [];

  // reset side controls interface
  document.getElementById('bw-sel').value = "100000";
  document.getElementById('visuals-palette-selector').value = "viridis";
  setScheme("viridis");

  // reset sliders values
  document.getElementById('lna').value = 28;
  document.getElementById('lna-val').textContent = '28dB';
  document.getElementById('vga').value = 44;
  document.getElementById('vga-val').textContent = '44dB';

  document.getElementById('mindb-slider').value = -110;
  document.getElementById('mindb-lbl').textContent = '-110 dBm';
  document.getElementById('maxdb-slider').value = -20;
  document.getElementById('maxdb-lbl').textContent = '-20 dBm';
  document.getElementById('wfspeed-slider').value = 4;
  document.getElementById('wfspeed-lbl').textContent = '4x slower';

  // preamp
  document.getElementById('preamp-btn').classList.remove('act');
  document.getElementById('preamp-btn').textContent = 'AMP: OFF';

  // display filters
  document.getElementById('trace-avg-btn').classList.remove('act');
  document.getElementById('trace-avg-btn').textContent = 'AVG: OFF';
  document.getElementById('trace-peak-btn').classList.remove('act');
  document.getElementById('trace-peak-btn').textContent = 'PEAK: OFF';

  if(isPaused) {
    togglePause();
  }

  changeFreq(88, 108);
  clearMarkers();
  logMsg('info', 'Factory settings & default limits successfully restored.');
}

function autoCalibrateFloor(){
  if(!latestBins || !latestBins.length){
    logMsg('warn', 'Wait for SDR traffic before calibration!');
    return;
  }
  let sum=0;
  for(let i=0;i<latestBins.length;i++) sum+=latestBins[i];
  const avg=sum/latestBins.length;
  dbMin=Math.round(avg-12);
  dbMax=Math.round(avg+45);
  // clamping
  if(dbMin < -130) dbMin=-130;
  if(dbMax > 20) dbMax=20;
  
  // Update sliders to fit calibration
  document.getElementById('mindb-slider').value = dbMin;
  document.getElementById('mindb-lbl').textContent = dbMin+' dBm';
  document.getElementById('maxdb-slider').value = dbMax;
  document.getElementById('maxdb-lbl').textContent = dbMax+' dBm';
  
  updateDbAxis();
  logMsg('info', `SDR floor calibrated. Range: ${dbMin} to ${dbMax} dBm`);
}

function updateDbAxis(){
  const span = dbMax - dbMin;
  const d3 = document.getElementById('db-axis');
  if (d3) {
    d3.children[0].textContent=dbMax+' dBm';
    d3.children[1].textContent=Math.round(dbMin+span*0.66)+' dBm';
    d3.children[2].textContent=Math.round(dbMin+span*0.33)+' dBm';
    d3.children[3].textContent=dbMin+' dBm';
  }
}

// WebSocket Connector
function connect(){
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws');
  ws.onopen=()=>{
    document.getElementById('sdot').className='ok';
    document.getElementById('stxt').textContent='online';
    logMsg('info','WebSocket premium diagnostic spectrum links online.');
    sendSweepConfig();
  };
  ws.onclose=()=>{
    document.getElementById('sdot').className='';
    document.getElementById('stxt').textContent='reconnecting...';
    setTimeout(connect,2500);
  };
  
  ws.onmessage=(ev)=>{
    if(isPaused) return;
    const raw=ev.data;
    if(raw.charCodeAt(0)===123){ // JSON header
       try{
        const d=JSON.parse(raw);
        if(d.type==='status') logMsg(d.level, d.msg);
        else if(d.type==='range'){
          freqStart=d.start; freqEnd=d.end;
          syncDialsToInputs();
          resize();
        }
      }catch(e){}
      return;
    }

    if(!sweepActive) return;
    lineBuf+=raw;
    const lines=lineBuf.split('\n');
    lineBuf=lines.pop();

    for(const line of lines){
      const t=line.trim(); if(!t) continue;
      const parts=t.split(','); if(parts.length<7) continue;
      const hzLow =parseFloat(parts[2]);
      const hzHigh=parseFloat(parts[3]);
      const bins  =parts.slice(6).map(Number).filter(v=>isFinite(v));
      if(!bins.length||isNaN(hzLow)) continue;

      if(expectedStartHz===null) expectedStartHz=hzLow;
      const isNewSweep=sweepFreqLow!==null && (hzLow < sweepFreqLow || hzLow === sweepFreqLow);

      if(isNewSweep){
        if(sweepBins.length>=4){
          latestBins=new Float32Array(sweepBins);
          drawSpectrum(latestBins);
          drawWaterfallLine(latestBins);
          updateSignalMonitor(latestBins);
          document.getElementById('info-bins').textContent=latestBins.length;
          document.getElementById('info-span').textContent=freqEnd-freqStart;
          rateCount++;
          const now=Date.now();
          if(now-lastRateTs>=1000){
            document.getElementById('info-rate').textContent=rateCount;
            rateCount=0; lastRateTs=now;
          }
        }
        sweepBins=[]; sweepFreqLow=hzLow; sweepFreqHigh=hzHigh;
      } else {
        if(sweepFreqLow===null) sweepFreqLow=hzLow;
        sweepFreqHigh=hzHigh;
      }

      const reqLow=freqStart*1e6;
      const reqHigh=freqEnd*1e6;
      const binHz=(hzHigh-hzLow)/bins.length;
      for(let i=0;i<bins.length;i++){
        const bf=hzLow+(i+0.5)*binHz;
        if(bf>=reqLow&&bf<=reqHigh) sweepBins.push(bins[i]);
      }
    }
  };
}

function sendSweepConfig(){
  resetSweepBuffers();
  if(!ws||ws.readyState!==1) return;
  ws.send(JSON.stringify({
    cmd:'setSweep',
    start:freqStart,
    end:freqEnd,
    lna:lnaGain,
    vga:vgaGain,
    amp:ampActive?1:0,
    binwidth:curBinwidth
  }));
}

// Terminology export logs
function logMsg(lvl, txt){
  const box=document.getElementById('console-log');
  if(!box) return;
  const item=document.createElement('div');
  const now=new Date().toLocaleTimeString();
  item.className='log-'+lvl;
  item.textContent=`[${now}] sdr: ${txt}`;
  box.appendChild(item);
  box.scrollTop=box.scrollHeight;
  while(box.children.length > LOG_LIMIT) box.removeChild(box.firstChild);
}

function toggleConsole(){
  const wrap=document.getElementById('console-wrap');
  wrap.classList.toggle('collapsed');
  const isNowCollapsed = wrap.classList.contains('collapsed');
  document.getElementById('ctoggle').textContent = isNowCollapsed ? '▲ EXPAND PANEL' : '▼ COLLAPSE PANEL';
}

function toggleQuickBands() {
  const container = document.getElementById('quick-bands-container');
  const btn = document.getElementById('toggle-bands-btn');
  if (container.style.display === 'none') {
    container.style.display = 'flex';
    btn.style.borderColor = '#f59e0b';
    btn.style.color = '#f59e0b';
  } else {
    container.style.display = 'none';
    btn.style.borderColor = '#475569';
    btn.style.color = '#475569';
  }
}

// Drawing & Plots
function resize(){
  const W=document.getElementById('display').clientWidth||600;
  const h=document.getElementById('waterfall-wrap').clientHeight||300;
  wfCanvas.width=W; wfCanvas.height=h;
  specCanvas.width=W; specCanvas.height=190;
  rulerCanvas.width=W; rulerCanvas.height=30;
  peakBuf=new Float32Array(W).fill(-300);
  avgBuf =new Float32Array(W).fill(-300);
  wfAccumBuf=new Float32Array(W).fill(-120); wfAccumCount=0;
  wfY=0;
  wfCtx.fillStyle='#020306'; wfCtx.fillRect(0,0,W,h);
  resetSweepBuffers();
  drawRuler(); updateDbAxis();
  renderQuickBands();
}
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(resize,100)});
setTimeout(resize,100);

function drawRuler(){
  const W=rulerCanvas.width, H=30, span=freqEnd-freqStart;
  if(span<=0)return;
  rulerCtx.fillStyle='#05070f'; rulerCtx.fillRect(0,0,W,H);
  rulerCtx.strokeStyle='#1e293b'; rulerCtx.lineWidth=1;
  rulerCtx.strokeRect(0,0,W,H);
  
  rulerCtx.fillStyle='#64748b'; rulerCtx.font='10px monospace';
  rulerCtx.textAlign='center';
  
  const step=span>1200?500:span>500?100:span>100?50:span>20?10:span>5?2:1;
  const firstMark=Math.ceil(freqStart/step)*step;
  for(let f=firstMark; f<=freqEnd; f+=step){
    const x=((f-freqStart)/span)*W;
    rulerCtx.strokeStyle='#334155'; rulerCtx.lineWidth=1.5;
    rulerCtx.beginPath(); rulerCtx.moveTo(x, 18); rulerCtx.lineTo(x, 30); rulerCtx.stroke();
    rulerCtx.fillText(f+'M', x, 12);
  }
}

function drawSpectrum(bins){
  const W=specCanvas.width, H=specCanvas.height, size=bins.length;
  specCtx.fillStyle='#020305'; specCtx.fillRect(0,0,W,H);
  
  // Grid Lines
  specCtx.strokeStyle='#0f172a'; specCtx.lineWidth=1;
  for(let y=0.25;y<1.0;y+=0.25){
    specCtx.beginPath();specCtx.moveTo(0,y*H);specCtx.lineTo(W,y*H);specCtx.stroke();
  }
  const span=freqEnd-freqStart;
  const step=span>200?50:span>50?10:5;
  const firstMark=Math.ceil(freqStart/step)*step;
  for(let f=firstMark; f<=freqEnd; f+=step){
    const x=((f-freqStart)/span)*W;
    specCtx.beginPath();specCtx.moveTo(x,0);specCtx.lineTo(x,H);specCtx.stroke();
  }

  const tempArr=new Float32Array(W).fill(-200);
  for(let x=0;x<W;x++){
    const si=x/W*size, lo=Math.floor(si), hi=Math.min(lo+1,size-1);
    const db=bins[lo]+(bins[hi]-bins[lo])*(si-lo);
    tempArr[x]=db;
    if(db>peakBuf[x]) peakBuf[x]=db;
    if(avgBuf[x]<=-200) avgBuf[x]=db;
    else avgBuf[x]=0.92*avgBuf[x]+0.08*db;
  }

  // Draw Averaged
  const drawLine=(buf,color,fill)=>{
    specCtx.beginPath();
    for(let x=0;x<W;x++){
      const y=(1-(buf[x]-dbMin)/(dbMax-dbMin))*H;
      if(x===0)specCtx.moveTo(x,y); else specCtx.lineTo(x,y);
    }
    if(fill){
      specCtx.lineTo(W,H); specCtx.lineTo(0,H); specCtx.closePath();
      const grad=specCtx.createLinearGradient(0,0,0,H);
      grad.addColorStop(0,'rgba(0,255,136,0.15)');grad.addColorStop(1,'rgba(0,0,0,0)');
      specCtx.fillStyle=grad; specCtx.fill();
    } else {
      specCtx.strokeStyle=color; specCtx.lineWidth=1.5; specCtx.stroke();
    }
  };

  if(peakHold) drawLine(peakBuf,'rgba(239, 68, 68, 0.4)',false);
  drawLine(useAvg?avgBuf:tempArr,'#00ff88',true);
  drawLine(useAvg?avgBuf:tempArr,'#00ff88',false);

  // Markers
  for(let i=0;i<markers.length;i++){
    const mx=((markers[i]-freqStart)/span)*W;
    if(mx>=0&&mx<=W){
      specCtx.strokeStyle='rgba(239, 68, 68, 0.8)'; specCtx.lineWidth=1;
      specCtx.setLineDash([3,3]);
      specCtx.beginPath(); specCtx.moveTo(mx,0); specCtx.lineTo(mx,H); specCtx.stroke();
      specCtx.setLineDash([]);
      specCtx.fillStyle='#ef4444';
      specCtx.fillText(`${markers[i].toFixed(1)}M`, mx+4, 15);
    }
  }
}

function drawWaterfallLine(bins){
  const W=wfCanvas.width, H=wfCanvas.height, size=bins.length;
  if(!wfAccumBuf||wfAccumBuf.length!==W) wfAccumBuf=new Float32Array(W);
  for(let x=0;x<W;x++){
    const si=x/W*size, lo=Math.floor(si), hi=Math.min(lo+1,size-1);
    wfAccumBuf[x]+=bins[lo]+(bins[hi]-bins[lo])*(si-lo);
  }
  wfAccumCount++;
  if(wfAccumCount < wfSpeed) return; 

  const img=wfCtx.createImageData(W, 1);
  const out=img.data;
  for(let x=0;x<W;x++){
    const db=wfAccumBuf[x]/wfAccumCount;
    dbToColor(db, dbMin, dbMax, out, x*4);
  }
  wfAccumBuf.fill(0); wfAccumCount=0;

  wfCtx.drawImage(wfCanvas, 0, 0, W, H-1, 0, 1, W, H-1);
  wfCtx.putImageData(img, 0, 0);
}

function dbToColor(db, mn, mx, out, off){
  const normalized=Math.max(0, Math.min(1, (db-mn)/(mx-mn)));
  const idx=Math.round(normalized*255);
  out[off]=LUT[idx*3]; out[off+1]=LUT[idx*3+1]; out[off+2]=LUT[idx*3+2]; out[off+3]=255;
}

function updateSignalMonitor(bins){
  let peakY=-400, peakX=0;
  for(let i=0;i<bins.length;i++){
    if(bins[i]>peakY){peakY=bins[i]; peakX=i;}
  }
  const peakHz=freqStart*1e6 + (peakX/bins.length)*(freqEnd-freqStart)*1e6;
  document.getElementById('peak-readout').textContent=peakY.toFixed(1)+' dBm';
  document.getElementById('peak-freq').textContent=(peakHz/1e6).toFixed(3)+' MHz';
}

// Marker Pins dropped system
function addMarker(freq){
  if(markers.length>=10) markers.shift();
  markers.push(freq);
  syncMarkersList();
}
function removeMarker(idx){
  markers.splice(idx,1);
  syncMarkersList();
}
function clearMarkers(){
  markers=[];
  syncMarkersList();
}
function syncMarkersList(){
  const box=document.getElementById('marker-list');
  box.innerHTML='';
  if(!markers.length){
    box.innerHTML='<div style="font-size:10px;color:var(--dim);text-align:center;padding:12px 0">No pins dropped.</div>';
    return;
  }
  markers.forEach((m,i)=>{
    const el=document.createElement('div');
    el.className='marker-item';
    el.innerHTML=`<span class="marker-lbl">📍 ${m.toFixed(2)} MHz</span><span class="marker-del" onclick="removeMarker(${i})">✕</span>`;
    box.appendChild(el);
  });
}

function exportFramePNG(){
  const dumpCanvas=document.createElement('canvas');
  dumpCanvas.width=specCanvas.width;
  dumpCanvas.height=specCanvas.height + wfCanvas.height;
  const tc=dumpCanvas.getContext('2d');
  tc.drawImage(specCanvas, 0, 0);
  tc.drawImage(wfCanvas, 0, specCanvas.height);
  
  const link=document.createElement('a');
  link.download=`SDR-Spectrum-${freqStart}-${freqEnd}MHz-${Date.now()}.png`;
  link.href=dumpCanvas.toDataURL();
  link.click();
  logMsg('info', 'SDR high-res chart grab saved to Local Downloads.');
}

// Ruler Mouse Interaction Drag-to-Pan
let isRulerDragging=false, rulerStartX=0, rulerOriginalStart=0;
rulerCanvas.addEventListener('mousedown',e=>{
  if(e.button===0){
    isRulerDragging=true;
    rulerStartX=e.clientX;
    rulerOriginalStart=freqStart;
  }
});
window.addEventListener('mousemove',e=>{
  if(isRulerDragging){
    const dx=e.clientX - rulerStartX;
    const span=freqEnd-freqStart;
    const mhzPerPx=span/rulerCanvas.width;
    const shift=Math.round(dx*mhzPerPx);
    changeFreq(rulerOriginalStart - shift, rulerOriginalStart - shift + span);
  }
});
window.addEventListener('mouseup',()=>{
  if(isRulerDragging){
    isRulerDragging=false;
    sendSweepConfig();
  }
});

// Click spec or waterfall to drop marker pin
function handleDisplayClick(e, elem){
  const rect=elem.getBoundingClientRect();
  const frac=(e.clientX-rect.left)/elem.width;
  const f=freqStart+frac*(freqEnd-freqStart);
  addMarker(f);
  logMsg('info', `Dropped PIN marker at: ${f.toFixed(3)} MHz`);
}
specCanvas.addEventListener('click',e=>handleDisplayClick(e,specCanvas));
wfCanvas.addEventListener('click',e=>handleDisplayClick(e,wfCanvas));

// Live coordinated overlay hovers mouse tracks
function onCanvasMouseMove(e, elem) {
  const rect = elem.getBoundingClientRect();
  const frac = (e.clientX - rect.left) / rect.width;
  if(frac < 0 || frac > 1) return;

  const span = freqEnd - freqStart;
  const hoverFreq = freqStart + frac * span;

  let dbVal = null;
  if (latestBins && latestBins.length) {
    const binIdx = Math.floor(frac * latestBins.length);
    if(binIdx >= 0 && binIdx < latestBins.length) {
      dbVal = latestBins[binIdx];
    }
  }

  const tooltip = document.getElementById('live-tooltip');
  tooltip.style.display = 'block';
  tooltip.style.left = (e.clientX + 16) + 'px';
  tooltip.style.top = (e.clientY + 12) + 'px';
  document.getElementById('lt-freq').textContent = hoverFreq.toFixed(3) + ' MHz';
  document.getElementById('lt-db').textContent = (dbVal !== null ? dbVal.toFixed(1) : '--') + ' dBm';

  // Check if nahe to database signal peaks
  let labelHit = null;
  const signalLib = [
    { freq: 88.5, label: "BBC Radio 2" },
    { freq: 91.3, label: "Classic FM" },
    { freq: 95.8, label: "Capital FM" },
    { freq: 98.1, label: "BBC Radio 1" },
    { freq: 101.4, label: "Heart Radio" },
    { freq: 105.8, label: "Absolute Radio" },
    { freq: 120.8, label: "VHF Air Traffic Control" },
    { freq: 124.55, label: "VHF Approach AM" },
    { freq: 128.25, label: "ATIS Airport Weather" },
    { freq: 144.3, label: "CW VHF Beacon" },
    { freq: 145.8, label: "ISS Amateur Voice Link" },
    { freq: 156.8, label: "Marine Channel 16" },
    { freq: 935.2, label: "GSM-900 Cell Station" },
    { freq: 1090.0, label: "ADS-B Mode-S Transponder" }
  ];

  for(const item of signalLib) {
    if (Math.abs(hoverFreq - item.freq) < span * 0.012) {
      labelHit = item.label;
      break;
    }
  }

  const lblEl = document.getElementById('lt-peak');
  if(labelHit) {
    lblEl.textContent = '★ ' + labelHit;
    lblEl.style.display = 'block';
  } else {
    lblEl.style.display = 'none';
  }
}

function onCanvasMouseLeave() {
  document.getElementById('live-tooltip').style.display = 'none';
}

specCanvas.addEventListener('mousemove', e => onCanvasMouseMove(e, specCanvas));
specCanvas.addEventListener('mouseleave', onCanvasMouseLeave);
wfCanvas.addEventListener('mousemove', e => onCanvasMouseMove(e, wfCanvas));
wfCanvas.addEventListener('mouseleave', onCanvasMouseLeave);


// Keyboard Shortcuts Integration
window.addEventListener('keydown',e=>{
  if(document.activeElement.tagName==='INPUT') return;
  const k=e.key.toLowerCase();
  if(e.code==='Space'||k===' '){
    e.preventDefault(); togglePause();
  } else if(k==='a'){
    autoCalibrateFloor();
  } else if(k==='r'){
    toggleAvg();
  } else if(k==='s'){
    togglePeak();
  } else if(k==='m'){
    clearMarkers();
  }
});

// Modals
function openModal(id){document.getElementById(id).classList.add('open')}
function closeModal(id){document.getElementById(id).classList.remove('open')}
function modalBackdrop(e,el){if(e.target===el)el.classList.remove('open')}

// Init
document.getElementById('start').addEventListener('change',()=>{
  const val = parseInt(document.getElementById('start').value);
  changeFreq(val, freqEnd);
});
document.getElementById('end').addEventListener('change',()=>{
  const val = parseInt(document.getElementById('end').value);
  changeFreq(freqStart, val);
});

syncDialsToInputs();
syncMarkersList();
renderQuickBands();
connect();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    print(f"[server] Standalone Web Receiver active. Running PID {MY_PID}")
    free_port(8085)
    kill_hackrf_users()
    app.run(host="0.0.0.0", port=8085, threaded=True)

