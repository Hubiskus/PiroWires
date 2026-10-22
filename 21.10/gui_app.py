"""Tkinter GUI application for Thorlabs Dual-Stage and Pyrometer control."""

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from pyro_logic import (
    find_focal_plane,
    get_temperature,
    measure_single_point,
    run_grid_scan,
)
from pyrometer_classes import PyroServer, PyroStage


class StageControlPanel(ttk.LabelFrame):
    """Panel kontrolny dla pojedynczego stolika (XY lub XYZ)"""

    def __init__(self, parent, stage: PyroStage, log_callback):
        super().__init__(parent, text=f" {stage.device_name} ", padding=10)
        self.stage = stage
        self.log = log_callback
        self.axis_names = ["X", "Y", "Z"][: stage.num_channels]

        self.entries_abs: dict[int, ttk.Entry] = {}
        self.entries_rel: dict[int, ttk.Entry] = {}
        self.pos_labels: dict[int, ttk.Label] = {}

        self._create_widgets()

    def _create_widgets(self):
        # 1. Pozycja aktualna
        pos_frame = ttk.Frame(self)
        pos_frame.pack(fill="x", pady=5)

        ttk.Label(pos_frame, text="Pozycja: ", font=("Helvetica", 9, "bold")).pack(
            side="left"
        )
        for i, name in enumerate(self.axis_names):
            lbl = ttk.Label(pos_frame, text=f"{name}: --- mm ", font=("Consolas", 10))
            lbl.pack(side="left", padx=5)
            self.pos_labels[i] = lbl

        # 2. Homing
        ttk.Button(
            self, text="Bazuj wszystkie osie (Home)", command=self._on_home
        ).pack(fill="x", pady=5)

        # 3. Ruch bezwzględny
        abs_frame = ttk.LabelFrame(self, text=" Ruch bezwzględny (mm) ")
        abs_frame.pack(fill="x", pady=5)

        for i, name in enumerate(self.axis_names):
            min_l, max_l = (
                self.stage.travel_limits_mm[i]
                if self.stage.travel_limits_mm
                else (0, 0)
            )
            row = ttk.Frame(abs_frame)
            row.pack(fill="x", padx=5, pady=2)
            ttk.Label(row, text=f"Os {name} [{min_l}-{max_l} mm]:", width=20).pack(
                side="left"
            )
            entry = ttk.Entry(row, width=10)
            entry.pack(side="left", padx=5)
            entry.bind("<Return>", lambda event: self._on_move_abs())
            self.entries_abs[i] = entry

        ttk.Button(abs_frame, text="Jedź do pozycji", command=self._on_move_abs).pack(
            fill="x", padx=5, pady=5
        )

        # 4. Ruch względny
        rel_frame = ttk.LabelFrame(self, text=" Ruch względny (Δ mm) ")
        rel_frame.pack(fill="x", pady=5)

        for i, name in enumerate(self.axis_names):
            row = ttk.Frame(rel_frame)
            row.pack(fill="x", padx=5, pady=2)
            ttk.Label(row, text=f"Delta {name}:", width=20).pack(side="left")
            entry = ttk.Entry(row, width=10)
            entry.pack(side="left", padx=5)
            entry.bind("<Return>", lambda event: self._on_move_rel())
            self.entries_rel[i] = entry

        ttk.Button(rel_frame, text="Przesuń o wartość", command=self._on_move_rel).pack(
            fill="x", padx=5, pady=5
        )

    def update_position_display(self):
        try:
            positions = self.stage.get_position()
            for i, val in enumerate(positions):
                self.pos_labels[i].config(text=f"{self.axis_names[i]}: {val:.4f} mm")
        except Exception as e:
            self.log(f"Błąd odczytu pozycji [{self.stage.device_name}]: {e}")

    def _run_in_thread(self, target_func, *args):
        def worker():
            try:
                target_func(*args)
                self.update_position_display()
                self.log(f"[{self.stage.device_name}] Operacja zakończona.")
            except Exception as e:
                self.log(f"[{self.stage.device_name}] BŁĄD: {e}")
                messagebox.showerror("Błąd", str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_home(self):
        self.log(f"[{self.stage.device_name}] Bazowanie...")
        self._run_in_thread(self.stage.home)

    def _on_move_abs(self):
        targets = []
        for i in range(self.stage.num_channels):
            v_str = self.entries_abs[i].get().strip()
            if v_str:
                try:
                    targets.append(float(v_str))
                except ValueError:
                    messagebox.showerror(
                        "Błąd", f"Zła wartość dla osi {self.axis_names[i]}"
                    )
                    return
            else:
                targets.append(None)
        if any(t is not None for t in targets):
            self.log(f"[{self.stage.device_name}] Ruch abs: {targets}")
            self._run_in_thread(self.stage.move_to, *targets)

    def _on_move_rel(self):
        deltas = []
        for i in range(self.stage.num_channels):
            v_str = self.entries_rel[i].get().strip()
            if v_str:
                try:
                    deltas.append(float(v_str))
                except ValueError:
                    messagebox.showerror(
                        "Błąd", f"Zła delta dla osi {self.axis_names[i]}"
                    )
                    return
            else:
                deltas.append(None)
        if any(d is not None for d in deltas):
            self.log(f"[{self.stage.device_name}] Ruch rel: {deltas}")
            self._run_in_thread(self.stage.move_relative, *deltas)


class PyrometerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pyrometer & Dual-Stage Controller")
        self.geometry("660x610")

        self.coarse_stage = PyroStage(
            device_name="Stolik Zgrubny XY",
            serial_number="70817296",
            steps_per_mm=25600,
            travel_limits_mm=[(0.0, 50.0), (0.0, 75.0)],
            num_channels=2,
        )
        self.fine_stage = PyroStage(
            device_name="Stolik Precyzyjny XYZ",
            serial_number="70821739",
            steps_per_mm=512000,
            travel_limits_mm=[(0.0, 4.0), (0.0, 4.0), (0.0, 4.0)],
            num_channels=3,
        )
        self.server = PyroServer(address="192.168.1.112", port=9125, timeout=5.0)

        self.stop_scan_event = threading.Event()

        self._create_widgets()
        self._connect_all_async()

    def _create_widgets(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # Tab 1: Sterowanie ręczne stolikami
        tab_stages = ttk.Frame(notebook)
        notebook.add(tab_stages, text=" Sterowanie Stolikami ")

        self.panel_coarse = StageControlPanel(
            tab_stages, self.coarse_stage, self.log_message
        )
        self.panel_coarse.pack(side="left", fill="both", expand=True, padx=5, pady=5)

        self.panel_fine = StageControlPanel(
            tab_stages, self.fine_stage, self.log_message
        )
        self.panel_fine.pack(side="right", fill="both", expand=True, padx=5, pady=5)

        # Tab 2: Pirometr i Pomiar Stacjonarny (BEZ RUCHU STOLIKA)
        tab_single = ttk.Frame(notebook)
        notebook.add(tab_single, text=" Pomiar Stacjonarny ")

        pyro_test_frame = ttk.LabelFrame(
            tab_single, text=" Szybki Odczyt Pirometru ", padding=10
        )
        pyro_test_frame.pack(fill="x", padx=10, pady=5)

        self.lbl_temp = ttk.Label(
            pyro_test_frame, text="Temperatura: --- °C", font=("Helvetica", 12)
        )
        self.lbl_temp.pack(side="left", padx=10)
        ttk.Button(
            pyro_test_frame,
            text="Odczytaj Temperaturę",
            command=self._on_read_temp,
        ).pack(side="right", padx=10)

        single_frame = ttk.LabelFrame(
            tab_single, text=" Pomiar Stacjonarny (Aktualna Pozycja) ", padding=10
        )
        single_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.lbl_single_pos = ttk.Label(
            single_frame,
            text="Aktualna pozycja: X: --- mm, Y: --- mm, Z: --- mm",
            font=("Consolas", 10, "bold"),
        )
        self.lbl_single_pos.pack(fill="x", pady=5)

        self.entry_sdur = self._add_field(single_frame, "Czas trwania (s):", "10.0")
        self.entry_sfreq = self._add_field(single_frame, "Częstotliwość (Hz):", "10.0")
        self.entry_sfile = self._add_field(
            single_frame,
            "Prefiks pliku wyjściowego:",
            "Single_point",
            entry_width=35,
        )

        ttk.Button(
            single_frame,
            text="Start Pomiaru Stacjonarnego",
            command=self._on_start_single,
        ).pack(fill="x", pady=10)

        # Tab 3: Skanowanie Siatki 3D
        tab_grid = ttk.Frame(notebook)
        notebook.add(tab_grid, text=" Skanowanie Siatki 3D ")

        grid_frame = ttk.LabelFrame(tab_grid, text=" Parametry Siatki 3D ", padding=10)
        grid_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.entry_xmin = self._add_field(grid_frame, "X Min / Max (mm):", "1.0", "3.0")
        self.entry_ymin = self._add_field(grid_frame, "Y Min / Max (mm):", "1.0", "3.0")
        self.entry_zmin = self._add_field(grid_frame, "Z Min / Max (mm):", "1.0", "3.0")
        self.entry_xstep = self._add_field(
            grid_frame, "Krok X / Y / Z (mm):", "0.5", "0.5", "0.5"
        )
        self.entry_settle = self._add_field(grid_frame, "Czas stabilizacji (s):", "1.0")
        self.entry_gfile = self._add_field(
            grid_frame,
            "Prefiks pliku wyjściowego:",
            "Test_measurement",
            entry_width=35,
        )

        btn_box = ttk.Frame(grid_frame)
        btn_box.pack(fill="x", pady=10)
        ttk.Button(btn_box, text="Uruchom Skan 3D", command=self._on_start_grid).pack(
            side="left", fill="x", expand=True, padx=5
        )
        ttk.Button(btn_box, text="Przerwij Pomiar", command=self._on_stop_scan).pack(
            side="right", fill="x", expand=True, padx=5
        )

        # Tab 4: Poszukiwanie Ogniska
        tab_focus = ttk.Frame(notebook)
        notebook.add(tab_focus, text=" Poszukiwanie Ogniska ")

        focus_frame = ttk.LabelFrame(
            tab_focus, text=" Parametry Szukania Ogniska ", padding=10
        )
        focus_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.entry_fx_range = self._add_field(
            focus_frame, "Skan Zgrubny X Min / Max (mm):", "10.0", "40.0"
        )
        self.entry_fx_step = self._add_field(focus_frame, "Krok Zgrubny X (mm):", "5.0")

        self.entry_fz_range = self._add_field(
            focus_frame, "Skan Precyzyjny Z Min / Max (mm):", "0.0", "4.0"
        )
        self.entry_fz_step = self._add_field(
            focus_frame, "Krok Precyzyjny Z (mm):", "0.1"
        )

        self.entry_f_fine_spans = self._add_field(
            focus_frame, "Otoczenie Zagęszczenia X / Z (mm):", "5.0", "0.2"
        )
        self.entry_f_fine_steps = self._add_field(
            focus_frame, "Krok Drobny X / Z (mm):", "1.0", "0.02"
        )

        self.entry_f_settle = self._add_field(
            focus_frame, "Czas stabilizacji (s):", "0.5"
        )
        self.entry_f_file = self._add_field(
            focus_frame,
            "Prefiks plików:",
            "Focal_search",
            entry_width=35,
        )

        f_btn_box = ttk.Frame(focus_frame)
        f_btn_box.pack(fill="x", pady=10)
        ttk.Button(
            f_btn_box,
            text="Uruchom Szukanie Ogniska",
            command=self._on_start_focus,
        ).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(f_btn_box, text="Przerwij Pomiar", command=self._on_stop_scan).pack(
            side="right", fill="x", expand=True, padx=5
        )

        # Dolny Dziennik Zdarzeń
        log_frame = ttk.LabelFrame(self, text=" Dziennik zdarzeń ", padding=5)
        log_frame.pack(fill="x", padx=10, pady=5)
        self.log_text = tk.Text(log_frame, height=7, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _add_field(
        self,
        parent,
        label_text: str,
        *default_vals,
        entry_width: int = 10,
        label_width: int = 36,
    ):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label_text, width=label_width).pack(side="left")
        entries = []
        for val in default_vals:
            e = ttk.Entry(row, width=entry_width)
            e.insert(0, val)
            e.pack(side="left", padx=3)
            entries.append(e)
        return entries if len(entries) > 1 else entries[0]

    def log_message(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _update_single_pos_label(self):
        try:
            pos = self.fine_stage.get_position()
            x, y, z = pos[0], pos[1], pos[2] if len(pos) > 2 else 0.0
            self.lbl_single_pos.config(
                text=f"Aktualna pozycja: X: {x:.3f} mm, Y: {y:.3f} mm, Z: {z:.3f} mm"
            )
        except Exception:
            self.lbl_single_pos.config(
                text="Aktualna pozycja: X: --- mm, Y: --- mm, Z: --- mm"
            )

    def _connect_all_async(self):
        def worker():
            try:
                self.coarse_stage.connect()
                self.panel_coarse.update_position_display()
                self.log_message("Stolik Zgrubny XY połączony.")
            except Exception as e:
                self.log_message(f"Błąd Zgrubny XY: {e}")

            try:
                self.fine_stage.connect()
                self.panel_fine.update_position_display()
                self._update_single_pos_label()
                self.log_message("Stolik Precyzyjny XYZ połączony.")
            except Exception as e:
                self.log_message(f"Błąd Precyzyjny XYZ: {e}")

            try:
                self.server.connect()
                self.log_message("Server Pirometru połączony.")
            except Exception as e:
                self.log_message(f"Błąd Servera Pirometru: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_read_temp(self):
        def worker():
            try:
                val = get_temperature(self.server)
                self.lbl_temp.config(text=f"Temperatura: {val:.2f} °C")
                self.log_message(f"Odczyt pirometru: {val:.2f}")
            except Exception as e:
                self.log_message(f"Błąd odczytu temperatury: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_start_single(self):
        try:
            dur = float(self.entry_sdur.get())
            freq = float(self.entry_sfreq.get())
            prefix = self.entry_sfile.get().strip()
        except ValueError:
            messagebox.showerror("Błąd", "Nieprawidłowe liczby w formularzu.")
            return

        self.stop_scan_event.clear()

        def worker():
            try:
                self._update_single_pos_label()
                measure_single_point(
                    fine_stage=self.fine_stage,
                    server=self.server,
                    duration_sec=dur,
                    frequency_hz=freq,
                    output_prefix=prefix,
                    stop_event=self.stop_scan_event,
                    log_callback=self.log_message,
                )
            except Exception as e:
                self.log_message(f"Błąd pomiaru stacjonarnego: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_start_grid(self):
        try:
            x_min, x_max = (
                float(self.entry_xmin[0].get()),
                float(self.entry_xmin[1].get()),
            )
            y_min, y_max = (
                float(self.entry_ymin[0].get()),
                float(self.entry_ymin[1].get()),
            )
            z_min, z_max = (
                float(self.entry_zmin[0].get()),
                float(self.entry_zmin[1].get()),
            )
            sx = float(self.entry_xstep[0].get())
            sy = float(self.entry_xstep[1].get())
            sz = float(self.entry_xstep[2].get())
            settle = float(self.entry_settle.get())
            prefix = self.entry_gfile.get().strip()
        except ValueError:
            messagebox.showerror("Błąd", "Nieprawidłowe dane siatki 3D.")
            return

        self.stop_scan_event.clear()

        def worker():
            try:
                run_grid_scan(
                    fine_stage=self.fine_stage,
                    server=self.server,
                    x_range=(x_min, x_max),
                    y_range=(y_min, y_max),
                    z_range=(z_min, z_max),
                    step_x=sx,
                    step_y=sy,
                    step_z=sz,
                    settle_seconds=settle,
                    output_prefix=prefix,
                    stop_event=self.stop_scan_event,
                    log_callback=self.log_message,
                )
                self.panel_fine.update_position_display()
                self._update_single_pos_label()
            except Exception as e:
                self.log_message(f"Błąd skanowania 3D: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_start_focus(self):
        try:
            fx_min, fx_max = (
                float(self.entry_fx_range[0].get()),
                float(self.entry_fx_range[1].get()),
            )
            fx_step = float(self.entry_fx_step.get())

            fz_min, fz_max = (
                float(self.entry_fz_range[0].get()),
                float(self.entry_fz_range[1].get()),
            )
            fz_step = float(self.entry_fz_step.get())

            xfine_span = float(self.entry_f_fine_spans[0].get())
            zfine_span = float(self.entry_f_fine_spans[1].get())

            xfine_step = float(self.entry_f_fine_steps[0].get())
            zfine_step = float(self.entry_f_fine_steps[1].get())

            settle = float(self.entry_f_settle.get())
            prefix = self.entry_f_file.get().strip()
        except ValueError:
            messagebox.showerror(
                "Błąd", "Nieprawidłowe liczby w formularzu poszukiwania ogniska."
            )
            return

        self.stop_scan_event.clear()

        def worker():
            try:
                find_focal_plane(
                    coarse_stage=self.coarse_stage,
                    fine_stage=self.fine_stage,
                    server=self.server,
                    x_range=(fx_min, fx_max),
                    x_step=fx_step,
                    z_range=(fz_min, fz_max),
                    z_step=fz_step,
                    x_fine_span=xfine_span,
                    x_fine_step=xfine_step,
                    z_fine_span=zfine_span,
                    z_fine_step=zfine_step,
                    settle_seconds=settle,
                    output_prefix=prefix,
                    stop_event=self.stop_scan_event,
                    log_callback=self.log_message,
                )
                self.panel_coarse.update_position_display()
                self.panel_fine.update_position_display()
                self._update_single_pos_label()
            except Exception as e:
                self.log_message(f"Błąd podczas szukania ogniska: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop_scan(self):
        self.stop_scan_event.set()
        self.log_message("Wysyłanie sygnału przerwania pomiaru...")


if __name__ == "__main__":
    app = PyrometerGUI()
    app.mainloop()
