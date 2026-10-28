"""
stage_gui.py - Zintegrowany interfejs GUI dla sterowania stolikami oraz pomiarów pirometrem.
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import pyro_scenarios as ps
import stage_control as sc
from pyrometer_classes import PyroServer

POLL_MS = 300
DEFAULT_STEP_MM = {"nano": "0.01", "max": "0.1"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("System Sterowania Stolikami i Pirometrem")
        root.geometry("1000x500")

        self.stages: sc.Stages | None = None
        self.pyro_server = PyroServer(address="192.168.2.112", port=9125)
        self.pyro_reader = ps.PyroReader(self.pyro_server)

        self.busy = {a: False for a in sc.AXES}
        self.threads = []
        self.events = queue.Queue()
        self.rows = {}
        self.scale_vars = {}
        self.status_var = tk.StringVar()
        self.stop_scenario_event = threading.Event()

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_connect()
        self.root.after(POLL_MS, self._tick)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True, pady=(0, 10))

        # TAB 1: Sterowanie Stolikami
        tab_stages = ttk.Frame(notebook, padding=5)
        notebook.add(tab_stages, text=" Sterowanie Stolikami ")

        for key in sc.CONTROLLERS:
            frame = ttk.LabelFrame(tab_stages, text=sc.CONTROLLER_LABELS.get(key, key), padding=8)
            frame.pack(fill="x", pady=(0, 8))

            self.scale_vars[key] = tk.StringVar(value="Łączenie...")
            ttk.Label(frame, textvariable=self.scale_vars[key], foreground="gray").grid(
                row=0, column=0, columnspan=12, sticky="w", pady=(0, 4))

            heads = ["Oś", "Pozycja", "Stan", "Ruch Absolutny (mm)", "", "Ruch Addytywny (Δ mm)", "", "", "Wysyłanie", "Bazowanie", "Stop"]
            for c, h in enumerate(heads):
                ttk.Label(frame, text=h, font=("TkDefaultFont", 9, "bold")).grid(row=1, column=c, padx=3)

            r = 2
            for axis, (k, _) in sc.AXES.items():
                if k == key:
                    self._build_row(frame, axis, key, r)
                    r += 1

        # TAB 2: Pirometr & Pomiar Stacjonarny
        tab_pyro = ttk.Frame(notebook, padding=10)
        notebook.add(tab_pyro, text=" Pirometr & Pomiar Stacjonarny ")
        self._build_pyro_tab(tab_pyro)

        # TAB 3: Poszukiwanie Ogniska / Skan Krokowy
        tab_scan = ttk.Frame(notebook, padding=10)
        notebook.add(tab_scan, text=" Poszukiwanie Ogniska (Skan Krokowy) ")
        self._build_scan_tab(tab_scan)

        # Dolna sekcja statusowa
        bottom = ttk.Frame(main)
        bottom.pack(fill="x")
        bottom.columnconfigure(1, weight=1)

        self.stop_all_btn = tk.Button(bottom, text="STOP ALL", bg="#c62828", fg="white",
                                      activebackground="#8e0000", activeforeground="white",
                                      font=("TkDefaultFont", 11, "bold"), width=12,
                                      command=self.stop_all, state="disabled")
        self.stop_all_btn.grid(row=0, column=0, padx=(0, 10))

        self.status_label = ttk.Label(bottom, textvariable=self.status_var, anchor="w", font=("Consolas", 10))
        self.status_label.grid(row=0, column=1, sticky="ew")

        self.connect_btn = ttk.Button(bottom, text="Ponów Połączenie", command=self.start_connect, state="disabled")
        self.connect_btn.grid(row=0, column=2, padx=(10, 0))

    def _build_row(self, frame, axis, key, r):
        lo, hi = sc.LIMITS_MM[axis]
        row = {
            "pos": tk.StringVar(value="--"),
            "state": tk.StringVar(value="--"),
            "target": tk.StringVar(value=f"{lo:g}"),
            "step": tk.StringVar(value=DEFAULT_STEP_MM.get(key, "0.1")),
        }

        ttk.Label(frame, text=f"{axis}\n({lo:g}..{hi:g} mm)").grid(row=r, column=0, padx=3, sticky="w")
        ttk.Label(frame, textvariable=row["pos"], width=13, font=("Consolas", 11)).grid(row=r, column=1, padx=3)
        ttk.Label(frame, textvariable=row["state"], width=16).grid(row=r, column=2, padx=3)

        target_entry = ttk.Entry(frame, textvariable=row["target"], width=8)
        target_entry.grid(row=r, column=3, padx=2)
        target_entry.bind("<Return>", lambda e, a=axis: self.go_abs(a))
        btn_abs = ttk.Button(frame, text="Idź do (Abs)", width=11, command=lambda a=axis: self.go_abs(a))
        btn_abs.grid(row=r, column=4, padx=2)

        step_entry = ttk.Entry(frame, textvariable=row["step"], width=8)
        step_entry.grid(row=r, column=5, padx=2)
        btn_minus = ttk.Button(frame, text="- krok", width=7, command=lambda a=axis: self.jog(a, -1))
        btn_minus.grid(row=r, column=6, padx=1)
        btn_plus = ttk.Button(frame, text="+ krok", width=7, command=lambda a=axis: self.jog(a, +1))
        btn_plus.grid(row=r, column=7, padx=1)

        btn_zero_pos = ttk.Button(frame, text="Idź do 0", width=8, command=lambda a=axis: self.go_to_zero(a))
        btn_zero_pos.grid(row=r, column=8, padx=3)

        btn_home = ttk.Button(frame, text="Home", width=7, command=lambda a=axis: self.home(a))
        btn_home.grid(row=r, column=9, padx=3)

        btn_stop = ttk.Button(frame, text="Stop", width=6, command=lambda a=axis: self.stop(a))
        btn_stop.grid(row=r, column=10, padx=3)

        row["action_buttons"] = [btn_abs, btn_minus, btn_plus, btn_zero_pos, btn_home]
        row["stop_button"] = btn_stop
        self.rows[axis] = row
        self._update_buttons(axis)

    def _build_pyro_tab(self, parent):
        cfg_frame = ttk.LabelFrame(parent, text=" Połączenie z Pirometrem ", padding=10)
        cfg_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(cfg_frame, text="Adres IP:").grid(row=0, column=0, padx=5, sticky="w")
        self.pyro_ip_var = tk.StringVar(value="192.168.2.112")
        ttk.Entry(cfg_frame, textvariable=self.pyro_ip_var, width=15).grid(row=0, column=1, padx=5)

        ttk.Label(cfg_frame, text="Port:").grid(row=0, column=2, padx=5, sticky="w")
        self.pyro_port_var = tk.StringVar(value="9125")
        ttk.Entry(cfg_frame, textvariable=self.pyro_port_var, width=8).grid(row=0, column=3, padx=5)

        ttk.Button(cfg_frame, text="Połącz z Pirometrem", command=self.connect_pyro).grid(row=0, column=4, padx=10)
        self.pyro_status_var = tk.StringVar(value="Niepołączony")
        ttk.Label(cfg_frame, textvariable=self.pyro_status_var, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=5, padx=5)

        mode_frame = ttk.LabelFrame(parent, text=" Tryb Pracy Pirometru ", padding=10)
        mode_frame.pack(fill="x", pady=(0, 8))

        self.pyro_mode_var = tk.StringVar(value="Tryb: --")
        ttk.Label(mode_frame, textvariable=self.pyro_mode_var, font=("Consolas", 11, "bold")).grid(row=0, column=0, padx=10, sticky="w")
        ttk.Button(mode_frame, text="Odczytaj Tryb", command=self.read_pyro_mode).grid(row=0, column=1, padx=5)

        read_frame = ttk.LabelFrame(parent, text=" Szybki Odczyt Temperatury i Sygnału ", padding=10)
        read_frame.pack(fill="x", pady=(0, 8))

        self.pyro_temp_var = tk.StringVar(value="-- °C")
        ttk.Label(read_frame, textvariable=self.pyro_temp_var, font=("Consolas", 16, "bold")).pack(side="left", padx=10)
        ttk.Button(read_frame, text="Odczytaj Stan Na Żywo", command=self.read_pyro_temp).pack(side="left", padx=10)

        stat_frame = ttk.LabelFrame(parent, text=" Scenariusz 1: Pomiar Stacjonarny ", padding=10)
        stat_frame.pack(fill="both", expand=True)

        self.stat_freq_var = tk.StringVar(value="10.0")
        self.stat_dur_var = tk.StringVar(value="60.0")
        self.stat_prefix_var = tk.StringVar(value="pyro_stationary")

        f = ttk.Frame(stat_frame)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="Częstotliwość (Hz):", width=18).pack(side="left")
        ttk.Entry(f, textvariable=self.stat_freq_var, width=10).pack(side="left", padx=5)

        f2 = ttk.Frame(stat_frame)
        f2.pack(fill="x", pady=3)
        ttk.Label(f2, text="Czas trwania (s):", width=18).pack(side="left")
        ttk.Entry(f2, textvariable=self.stat_dur_var, width=10).pack(side="left", padx=5)

        f3 = ttk.Frame(stat_frame)
        f3.pack(fill="x", pady=3)
        ttk.Label(f3, text="Prefiks pliku CSV:", width=18).pack(side="left")
        ttk.Entry(f3, textvariable=self.stat_prefix_var, width=25).pack(side="left", padx=5)

        b_box = ttk.Frame(stat_frame)
        b_box.pack(fill="x", pady=10)
        ttk.Button(b_box, text="Start Pomiaru Stacjonarnego", command=self.start_stationary).pack(side="left", padx=5)
        ttk.Button(b_box, text="Przerwij Pomiar", command=self.stop_scenario).pack(side="left", padx=5)

    def _build_scan_tab(self, parent):
        p_frame = ttk.LabelFrame(parent, text=" Parametry Skanu Krokowego i Poszukiwania Ogniska ", padding=10)
        p_frame.pack(fill="both", expand=True)

        self.scan_x_start = tk.StringVar(value="0.0")
        self.scan_x_stop = tk.StringVar(value="50.0")
        self.scan_x_step = tk.StringVar(value="5.0")

        self.scan_z_start = tk.StringVar(value="0.0")
        self.scan_z_stop = tk.StringVar(value="4.0")
        self.scan_z_step = tk.StringVar(value="0.1")

        self.scan_settle = tk.StringVar(value="1.0")
        self.scan_samples = tk.StringVar(value="21")
        self.scan_drop_low = tk.StringVar(value="3")
        self.scan_drop_high = tk.StringVar(value="3")

        # Lokalne dogęszczanie Z na każdym X
        self.scan_refine_local_z_active = tk.BooleanVar(value=True)
        self.scan_refine_local_z_span = tk.StringVar(value="0.3")
        self.scan_refine_local_z_step = tk.StringVar(value="0.02")

        # Globalne dogęszczanie
        self.scan_refine_active = tk.BooleanVar(value=True)
        self.scan_refine_x_span = tk.StringVar(value="5.0")
        self.scan_refine_x_step = tk.StringVar(value="1.0")
        self.scan_refine_z_span = tk.StringVar(value="0.2")
        self.scan_refine_z_step = tk.StringVar(value="0.02")
        self.scan_prefix = tk.StringVar(value="pyro_focal_scan")

        r = 0
        ttk.Label(p_frame, text="Skan Zgrubny X (mm) [Od / Do / Krok]:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(p_frame, textvariable=self.scan_x_start, width=8).grid(row=r, column=1)
        ttk.Entry(p_frame, textvariable=self.scan_x_stop, width=8).grid(row=r, column=2)
        ttk.Entry(p_frame, textvariable=self.scan_x_step, width=8).grid(row=r, column=3)

        r += 1
        ttk.Label(p_frame, text="Skan Zgrubny Z (mm) [Od / Do / Krok]:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(p_frame, textvariable=self.scan_z_start, width=8).grid(row=r, column=1)
        ttk.Entry(p_frame, textvariable=self.scan_z_stop, width=8).grid(row=r, column=2)
        ttk.Entry(p_frame, textvariable=self.scan_z_step, width=8).grid(row=r, column=3)

        r += 1
        ttk.Label(p_frame, text="Czas stabilizacji po ruchu (s):").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(p_frame, textvariable=self.scan_settle, width=8).grid(row=r, column=1)

        r += 1
        ttk.Label(p_frame, text="Próbki na punkt / Odrzucanie (Min/Max):").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(p_frame, textvariable=self.scan_samples, width=8).grid(row=r, column=1)
        ttk.Entry(p_frame, textvariable=self.scan_drop_low, width=8).grid(row=r, column=2)
        ttk.Entry(p_frame, textvariable=self.scan_drop_high, width=8).grid(row=r, column=3)

        r += 1
        ttk.Checkbutton(p_frame, text="Lokalne dogęszczanie Z na każdym X (Okno +/- mm / Krok mm):",
                        variable=self.scan_refine_local_z_active).grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(p_frame, textvariable=self.scan_refine_local_z_span, width=8).grid(row=r, column=1)
        ttk.Entry(p_frame, textvariable=self.scan_refine_local_z_step, width=8).grid(row=r, column=2)

        r += 1
        ttk.Checkbutton(p_frame, text="Globalne zagęszczanie X/Z wokół ogniska:",
                        variable=self.scan_refine_active).grid(row=r, column=0, sticky="w", pady=3)

        r += 1
        ttk.Label(p_frame, text=" - Globalne X (Okno +/- mm / Krok mm):").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(p_frame, textvariable=self.scan_refine_x_span, width=8).grid(row=r, column=1)
        ttk.Entry(p_frame, textvariable=self.scan_refine_x_step, width=8).grid(row=r, column=2)

        r += 1
        ttk.Label(p_frame, text=" - Globalne Z (Okno +/- mm / Krok mm):").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(p_frame, textvariable=self.scan_refine_z_span, width=8).grid(row=r, column=1)
        ttk.Entry(p_frame, textvariable=self.scan_refine_z_step, width=8).grid(row=r, column=2)

        r += 1
        ttk.Label(p_frame, text="Prefiks pliku wyjściowego CSV:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(p_frame, textvariable=self.scan_prefix, width=25).grid(row=r, column=1, columnspan=2, sticky="w")

        btn_box = ttk.Frame(p_frame)
        btn_box.grid(row=r+1, column=0, columnspan=4, pady=10, sticky="w")
        ttk.Button(btn_box, text="Uruchom Skan Krokowy & Szukanie Ogniska", command=self.start_focal_scan).pack(side="left", padx=5)
        ttk.Button(btn_box, text="Przerwij Skan", command=self.stop_scenario).pack(side="left", padx=5)

        prog_frame = ttk.Frame(p_frame)
        prog_frame.grid(row=r+2, column=0, columnspan=4, sticky="ew", pady=(5, 0))

        self.scan_prog_label = ttk.Label(prog_frame, text="Postęp: 0 / 0 (0%)", font=("TkDefaultFont", 9, "bold"))
        self.scan_prog_label.pack(anchor="w")

        self.scan_progressbar = ttk.Progressbar(prog_frame, orient="horizontal", mode="determinate")
        self.scan_progressbar.pack(fill="x", expand=True, pady=(4, 0))

    def _update_buttons(self, axis):
        connected = self.stages is not None
        row = self.rows[axis]
        action = "normal" if connected and not self.busy[axis] else "disabled"
        for b in row["action_buttons"]:
            b.config(state=action)
        row["stop_button"].config(state="normal" if connected else "disabled")

    def _update_all_buttons(self):
        for a in sc.AXES:
            self._update_buttons(a)
        self.stop_all_btn.config(state="normal" if self.stages else "disabled")
        self.connect_btn.config(state="disabled" if self.stages else "normal")

    def set_status(self, msg, error=False):
        self.status_var.set(msg)
        self.status_label.config(foreground="#b00020" if error else "black")

    # --------------------Połączenia--------------------
    def start_connect(self):
        self.connect_btn.config(state="disabled")
        self.set_status("Łączenie z kontrolerami...")

        def worker():
            try:
                stages = sc.Stages()
                self.events.put(("connected", stages))
            except Exception as e:
                self.events.put(("connect_failed", e))

        threading.Thread(target=worker, daemon=True).start()

    def connect_pyro(self):
        try:
            self.pyro_server.address = self.pyro_ip_var.get().strip()
            self.pyro_server.port = int(self.pyro_port_var.get().strip())
            self.pyro_server.connect()
            self.pyro_status_var.set("Połączono")
            self.set_status("Połączono z serwerem pirometru.")
        except Exception as e:
            self.pyro_status_var.set("Błąd")
            self.set_status(f"Błąd pirometru: {e}", error=True)

    def read_pyro_mode(self):
        def worker():
            try:
                mode = self.pyro_reader.get_mode()
                self.pyro_mode_var.set(f"Tryb: {mode}")
                self.set_status(f"Odczytano tryb pirometru: {mode}")
            except Exception as e:
                self.pyro_mode_var.set("Tryb: Błąd")
                self.set_status(f"Błąd odczytu trybu: {e}", error=True)

        threading.Thread(target=worker, daemon=True).start()

    def read_pyro_temp(self):
        def worker():
            try:
                temp, (sig1, sig2), (lvl1, lvl2) = self.pyro_reader.read_full()
                self.pyro_temp_var.set(f"{temp:.2f} °C")
                self.set_status(f"Temp: {temp:.2f} °C | Sig: {sig1:.3e} / {sig2:.3e} A | Lvl: {lvl1:.1f}% / {lvl2:.1f}%")
            except Exception as e:
                self.pyro_temp_var.set("Błąd")
                self.set_status(f"Błąd odczytu: {e}", error=True)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _parse(var, what):
        try:
            return float(var.get().replace(",", "."))
        except ValueError:
            raise ValueError(f"{what}: '{var.get()}' nie jest prawidłową liczbą")

    def run_axis_task(self, axis, label, fn):
        if self.busy[axis] or self.stages is None:
            return
        self.busy[axis] = True
        self._update_buttons(axis)
        self.set_status(f"{axis}: {label}...")

        def worker():
            try:
                fn()
                self.events.put(("done", axis, f"{axis}: {label} - zakończono", None))
            except sc.MoveAborted:
                self.events.put(("done", axis, f"{axis}: zatrzymano", None))
            except Exception as e:
                self.events.put(("done", axis, None, e))

        t = threading.Thread(target=worker, daemon=True)
        self.threads.append(t)
        t.start()

    def go_abs(self, axis):
        try:
            mm = self._parse(self.rows[axis]["target"], "Pozycja docelowa")
        except ValueError as e:
            return self.set_status(str(e), error=True)
        self.run_axis_task(axis, f"ruch do {mm:g} mm", lambda: self.stages.move_to(axis, mm))

    def jog(self, axis, direction):
        try:
            step = self._parse(self.rows[axis]["step"], "Krok")
        except ValueError as e:
            return self.set_status(str(e), error=True)
        d = direction * step
        self.run_axis_task(axis, f"ruch o {d:+g} mm", lambda: self.stages.move_by(axis, d))

    def go_to_zero(self, axis):
        self.run_axis_task(axis, "ruch do pozycji 0.0 mm", lambda: self.stages.move_to(axis, 0.0))

    def home(self, axis):
        if not messagebox.askyesno("Bazowanie", f"Czy rozpocząć bazowanie osi {axis}?\nUpewnij się, że tor ruchu jest wolny."):
            return
        self.run_axis_task(axis, "bazowanie", lambda: self.stages.home(axis))

    def stop(self, axis):
        if self.stages:
            try:
                self.stages.stop(axis)
                self.set_status(f"{axis}: zatrzymano")
            except Exception as e:
                self.set_status(str(e), error=True)

    def stop_all(self):
        self.stop_scenario_event.set()
        if self.stages:
            self.stages.stop_all()
            self.set_status("Wszystkie osie zatrzymane.")

    def stop_scenario(self):
        self.stop_scenario_event.set()
        self.set_status("Wysłano sygnał przerwania scenariusza.")

    # ----------------Scenariusze Pomiarowe---------------------------
    def start_stationary(self):
        try:
            freq = float(self.stat_freq_var.get())
            dur = float(self.stat_dur_var.get())
            prefix = self.stat_prefix_var.get().strip()
        except ValueError:
            return self.set_status("Nieprawidłowe parametry pomiaru stacjonarnego", error=True)

        p = ps.StationaryParams(frequency_hz=freq, duration_s=dur, prefix=prefix)
        self.stop_scenario_event.clear()

        def worker():
            try:
                self.set_status("Pomiar stacjonarny w toku...")
                path, n, stopped = ps.run_stationary(
                    self.pyro_reader, p, self.stop_scenario_event,
                    on_sample=lambda ts, val: self.pyro_temp_var.set(f"{val:.2f} °C")
                )
                status = "przerwany" if stopped else "zakończony"
                self.set_status(f"Pomiar stacjonarny {status}. Zapisano {n} próbek do {path.name}")
            except Exception as e:
                self.set_status(f"Błąd pomiaru stacjonarnego: {e}", error=True)

        threading.Thread(target=worker, daemon=True).start()

    def start_focal_scan(self):
        if self.stages is None:
            return messagebox.showerror("Błąd", "Stoliki nie są połączone.")
        try:
            p = ps.ScanParams(
                x_start=float(self.scan_x_start.get()),
                x_stop=float(self.scan_x_stop.get()),
                x_step=float(self.scan_x_step.get()),
                z_start=float(self.scan_z_start.get()),
                z_stop=float(self.scan_z_stop.get()),
                z_step=float(self.scan_z_step.get()),
                settle_s=float(self.scan_settle.get()),
                n_samples=int(self.scan_samples.get()),
                drop_low=int(self.scan_drop_low.get()),
                drop_high=int(self.scan_drop_high.get()),
                refine_local_z=self.scan_refine_local_z_active.get(),
                refine_local_z_halfwidth=float(self.scan_refine_local_z_span.get()),
                refine_local_z_step=float(self.scan_refine_local_z_step.get()),
                refine=self.scan_refine_active.get(),
                refine_x_halfwidth=float(self.scan_refine_x_span.get()),
                refine_x_step=float(self.scan_refine_x_step.get()),
                refine_z_halfwidth=float(self.scan_refine_z_span.get()),
                refine_z_step=float(self.scan_refine_z_step.get()),
                prefix=self.scan_prefix.get().strip()
            )
        except ValueError as e:
            return messagebox.showerror("Błąd parametrów", str(e))

        self.stop_scenario_event.clear()

        def worker():
            try:
                self.set_status("Rozpoczynanie skanu krokowego / ogniska...")
                res = ps.run_stepwise_scan(
                    self.stages, self.pyro_reader, p, self.stop_scenario_event,
                    on_info=lambda txt: self.set_status(txt),
                    on_progress=lambda phase, done, total: self.events.put(("scan_progress", phase, done, total)),
                    on_point=lambda pt: self.pyro_temp_var.set(f"{pt.mean:.2f} °C")
                )
                if res.stopped:
                    msg = "Skanowanie zostało przerwane przez użytkownika."
                    self.events.put(("scan_done", msg, False))
                elif res.focus:
                    msg = (f"Skanowanie zakończone sukcesem!\n\n"
                           f"Wyznaczone ognisko:\n"
                           f"• X = {res.focus.x:g} mm\n"
                           f"• Z = {res.focus.z:g} mm\n"
                           f"• Temp max = {res.focus.mean:.2f} °C\n\n"
                           f"Plik wyjściowy: {res.summary_path.name if res.summary_path else '-'}")
                    self.events.put(("scan_done", msg, True))
                else:
                    msg = "Skanowanie zakończone bez wyznaczenia ogniska."
                    self.events.put(("scan_done", msg, False))
            except Exception as e:
                self.set_status(f"Błąd skanowania: {e}", error=True)

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------------------------------------------- Główna pętla
    def _handle(self, ev):
        kind = ev[0]
        if kind == "connected":
            self.stages = ev[1]
            for key in sc.CONTROLLERS:
                axis = next(a for a, (k, _) in sc.AXES.items() if k == key)
                note = " (OVERRIDE)" if self.stages.k_overridden[axis] else ""
                self.scale_vars[key].set(
                    f"Kontroler {sc.CONTROLLERS[key].decode()} | {self.stages.k[axis]:.0f} kroków/mm{note}")
            self._update_all_buttons()
            self.set_status("Połączono ze stolikami.")
        elif kind == "connect_failed":
            self._update_all_buttons()
            self.set_status(f"Błąd połączenia ze stolikami: {ev[1]}", error=True)
            messagebox.showerror("Błąd połączenia", str(ev[1]))
        elif kind == "done":
            _, axis, msg, err = ev
            self.busy[axis] = False
            self._update_buttons(axis)
            if err is not None:
                self.set_status(str(err), error=True)
            else:
                self.set_status(msg)
        elif kind == "scan_progress":
            _, phase, done, total = ev
            pct = (done / total * 100) if total > 0 and done <= total else (100.0 if done >= total else 0.0)
            self.scan_progressbar["value"] = pct
            
            faza_name = {
                "coarse": "Skan zgrubny",
                "refine_local_z": "Lokalne dogęszczanie Z (na X)",
                "refine_x": "Globalne zagęszczanie osi X",
                "refine_z": "Globalne zagęszczanie osi Z"
            }.get(phase, phase)
            
            self.scan_prog_label.config(
                text=f"Faza: {faza_name} | Wykonano {done} z szacowanych {total} punktów ({pct:.1f}%)"
            )
        elif kind == "scan_done":
            _, msg, is_success = ev
            self.set_status(msg.replace("\n", " "))
            if is_success:
                messagebox.showinfo("Wynik Ogniska", msg)
            else:
                messagebox.showwarning("Skanowanie", msg)

    def _tick(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass

        if self.stages is not None:
            try:
                self.stages.request_updates()
                for axis, st in self.stages.snapshot().items():
                    row = self.rows[axis]
                    row["pos"].set(f"{st.position_mm:9.4f} mm")
                    parts = ["homing" if st.homing else "moving" if st.moving else "idle"]
                    if st.homed:
                        parts.append("homed")
                    if not st.enabled:
                        parts.append("NIEAKTYWNY")
                    row["state"].set(" | ".join(parts))
            except sc.StageError:
                pass
            except Exception as e:
                self.set_status(f"Błąd odświeżania: {e}", error=True)

        self.root.after(POLL_MS, self._tick)

    def on_close(self):
        self.stop_scenario_event.set()
        if self.stages is not None:
            self.stages.stop_all()
            for t in self.threads:
                t.join(timeout=1.0)
            self.stages.close()
            self.stages = None
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()