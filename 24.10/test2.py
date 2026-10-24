import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time

try:
    import thorlabs_apt as apt

    APT_AVAILABLE = True
except ImportError:
    APT_AVAILABLE = False

SERIAL_NUMBER = 70817296  # thorlabs_apt wymaga liczby całkowitej (int)
LIMITS = [(0.0, 50.0), (0.0, 75.0)]  # Limity w mm dla Osi 1 (X) i Osi 2 (Y)


class ThorlabsAptGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Thorlabs APT Test - Stolik Zgrubny (70817296)")
        self.root.geometry("480x520")
        self.root.resizable(False, False)

        self.motors = {}  # Przechowuje obiekty silników dla kanałów
        self.is_connected = False

        self._build_gui()

        if not APT_AVAILABLE:
            self.log(
                "BŁĄD: Brak zainstalowanej biblioteki 'thorlabs_apt' (pip install thorlabs-apt)."
            )

    def _build_gui(self):
        # 1. Sekcja Połączenia
        conn_frame = ttk.LabelFrame(
            self.root, text=" Połączenie (thorlabs_apt) ", padding=10
        )
        conn_frame.pack(fill="x", padx=10, pady=5)

        self.btn_connect = ttk.Button(
            conn_frame, text="Połącz / Ponów połączenie", command=self.action_connect
        )
        self.btn_connect.pack(fill="x", pady=2)

        self.lbl_status = ttk.Label(
            conn_frame,
            text="Stan: Rozłączony",
            foreground="red",
            font=("Arial", 10, "bold"),
        )
        self.lbl_status.pack(anchor="w", pady=2)

        # 2. Wybór Osi
        axis_frame = ttk.LabelFrame(self.root, text=" Wybór Osi ", padding=10)
        axis_frame.pack(fill="x", padx=10, pady=5)

        self.selected_axis = tk.IntVar(value=1)
        ttk.Radiobutton(
            axis_frame,
            text="Oś 1 (X) [Limit: 0-50 mm]",
            variable=self.selected_axis,
            value=1,
        ).pack(anchor="w")
        ttk.Radiobutton(
            axis_frame,
            text="Oś 2 (Y) [Limit: 0-75 mm]",
            variable=self.selected_axis,
            value=2,
        ).pack(anchor="w")

        # 3. Sekcja Bazowania (Home)
        home_frame = ttk.LabelFrame(self.root, text=" Bazowanie ", padding=10)
        home_frame.pack(fill="x", padx=10, pady=5)

        self.btn_home = ttk.Button(
            home_frame,
            text="Bazuj wybraną oś (Home)",
            command=self.action_home,
            state="disabled",
        )
        self.btn_home.pack(fill="x")

        # 4. Sekcja Ruchu Addytywnego (Względnego)
        move_frame = ttk.LabelFrame(
            self.root, text=" Ruch Addytywny (Względny) ", padding=10
        )
        move_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(move_frame, text="Wartość kroku [mm]:").pack(anchor="w")
        self.entry_step = ttk.Entry(move_frame)  # Puste pole - brak wartości domyślnej
        self.entry_step.pack(fill="x", pady=5)

        btn_grid = ttk.Frame(move_frame)
        btn_grid.pack(fill="x", pady=2)

        self.btn_step_neg = ttk.Button(
            btn_grid,
            text="◄ Krok ujemny (-)",
            command=lambda: self.action_step(-1),
            state="disabled",
        )
        self.btn_step_neg.pack(side="left", expand=True, fill="x", padx=2)

        self.btn_step_pos = ttk.Button(
            btn_grid,
            text="Krok dodatni (+) ►",
            command=lambda: self.action_step(1),
            state="disabled",
        )
        self.btn_step_pos.pack(side="right", expand=True, fill="x", padx=2)

        # 5. Odczyt Pozycji i Logi
        log_frame = ttk.LabelFrame(self.root, text=" Odczyt i Logi ", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.lbl_pos = ttk.Label(
            log_frame, text="Pozycja Oś 1: --- mm | Pozycja Oś 2: --- mm"
        )
        self.lbl_pos.pack(anchor="w", pady=2)

        self.txt_log = tk.Text(log_frame, height=8, state="disabled")
        self.txt_log.pack(fill="both", expand=True, pady=2)

    def log(self, text: str):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _run_in_thread(self, target_func):
        threading.Thread(target=target_func, daemon=True).start()

    # --- AKCJE SINKOWE / HARDWARE ---

    def action_connect(self):
        if not APT_AVAILABLE:
            messagebox.showerror("Błąd", "Zainstaluj bibliotekę thorlabs-apt!")
            return

        def task():
            self.log("Rozpoczynanie połączenia przez thorlabs_apt...")
            try:
                # Inicjalizacja silnika wg numeru seryjnego
                motor = apt.Motor(SERIAL_NUMBER)
                motor.identify()

                self.motors[1] = motor
                # W thorlabs_apt dla kontrolerów dwukanałowych odwołanie następuje przez ten sam obiekt lub kolejne kanały
                self.motors[2] = motor

                self.is_connected = True
                self.root.after(0, self._update_ui_connected, True)
                self.log("Połączono pomyślnie z urządzeniem APT.")
                self.update_positions()

            except Exception as e:
                self.is_connected = False
                self.root.after(0, self._update_ui_connected, False)
                self.log(f"BŁĄD Połączenia APT: {e}")

        self._run_in_thread(task)

    def action_home(self):
        def task():
            ch = self.selected_axis.get()
            self.log(f"Bazowanie osi {ch} (APT)...")
            try:
                motor = self.motors.get(ch)
                if motor:
                    motor.move_home(True)  # True = blokuj do zakończenia bazowania
                    self.log(f"Oś {ch} zbazowana pomyślnie.")
                    self.update_positions()
            except Exception as e:
                self.log(f"BŁĄD bazowania osi {ch}: {e}")

        self._run_in_thread(task)

    def action_step(self, direction: int):
        val_str = self.entry_step.get().strip()
        if not val_str:
            messagebox.showwarning("Brak wartości", "Wprowadź wartość kroku w mm!")
            return

        try:
            step_mm = float(val_str) * direction
        except ValueError:
            messagebox.showerror("Błąd", "Wprowadź poprawną liczbę!")
            return

        def task():
            ch = self.selected_axis.get()
            try:
                motor = self.motors.get(ch)
                curr_pos = float(motor.position)
                target = curr_pos + step_mm

                limit_min, limit_max = LIMITS[ch - 1]
                if not (limit_min <= target <= limit_max):
                    self.log(
                        f"ODRZUCONO: Cel {target:.3f} mm wykracza poza limit ({limit_min}-{limit_max} mm)."
                    )
                    return

                self.log(
                    f"Ruch osi {ch}: {curr_pos:.3f} mm -> {target:.3f} mm (krok: {step_mm:+.3f} mm)"
                )
                motor.move_relative(step_mm, True)  # True = czekaj na ruch

                new_pos = float(motor.position)
                self.log(f"Ruch osi {ch} zakończony na: {new_pos:.3f} mm")
                self.update_positions()
            except Exception as e:
                self.log(f"BŁĄD ruchu osi {ch}: {e}")

        self._run_in_thread(task)

    def update_positions(self):
        if not self.is_connected or 1 not in self.motors:
            return
        try:
            p1 = self.motors[1].position
            p2 = self.motors[2].position if 2 in self.motors else 0.0
            self.root.after(
                0,
                lambda: self.lbl_pos.config(
                    text=f"Pozycja Oś 1: {p1:.3f} mm | Pozycja Oś 2: {p2:.3f} mm"
                ),
            )
        except Exception:
            pass

    def _update_ui_connected(self, connected: bool):
        state = "normal" if connected else "disabled"
        self.btn_home.config(state=state)
        self.btn_step_neg.config(state=state)
        self.btn_step_pos.config(state=state)

        if connected:
            self.lbl_status.config(text="Stan: Połączono (APT)", foreground="green")
        else:
            self.lbl_status.config(text="Stan: Błąd / Rozłączony", foreground="red")


if __name__ == "__main__":
    root = tk.Tk()
    app = ThorlabsAptGUI(root)
    root.mainloop()
