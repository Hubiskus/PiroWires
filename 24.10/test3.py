import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import sys

# Konfiguracja ładowania bibliotek .NET przez pythonnet
DOTNET_OK = False
DOTNET_ERR_MSG = ""

try:
    import clr
    import System
    from System import Decimal

    KINESIS_PATH = r"C:\Program Files\Thorlabs\Kinesis"
    sys.path.append(KINESIS_PATH)

    clr.AddReference("Thorlabs.MotionControl.DeviceManagerCLI")
    clr.AddReference("Thorlabs.MotionControl.GenericMotorCLI")
    clr.AddReference("Thorlabs.MotionControl.Benchtop.StepperMotorCLI")

    from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
    from Thorlabs.MotionControl.Benchtop.StepperMotorCLI import BenchtopStepperMotor

    DOTNET_OK = True
except Exception as err:
    DOTNET_OK = False
    DOTNET_ERR_MSG = str(err)

SERIAL_NUMBER = "70817296"
LIMITS = [(0.0, 50.0), (0.0, 75.0)]


class KinesisDotNetGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Thorlabs Kinesis .NET Test - Stolik Zgrubny (70817296)")
        self.root.geometry("480x520")
        self.root.resizable(False, False)

        self.device = None
        self.channels = {}
        self.is_connected = False

        self._build_gui()

        if not DOTNET_OK:
            self.log(f"BŁĄD Ładowania Kinesis .NET DLL: {DOTNET_ERR_MSG}")

    def _build_gui(self):
        # 1. Sekcja Połączenia
        conn_frame = ttk.LabelFrame(
            self.root, text=" Połączenie (Kinesis .NET API) ", padding=10
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

        # 4. Sekcja Ruchu Addytywnego
        move_frame = ttk.LabelFrame(
            self.root, text=" Ruch Addytywny (Względny) ", padding=10
        )
        move_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(move_frame, text="Wartość kroku [mm]:").pack(anchor="w")
        self.entry_step = ttk.Entry(move_frame)  # Puste pole
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

    # --- HARDWARE KINESIS .NET ---

    def action_connect(self):
        if not DOTNET_OK:
            messagebox.showerror(
                "Błąd", f"Nie można załadować Kinesis .NET API:\n{DOTNET_ERR_MSG}"
            )
            return

        def task():
            self.log("Rozpoczynanie połączenia przez Kinesis .NET API...")
            try:
                # Czyszczenie poprzedniej sesji
                if self.device:
                    try:
                        self.device.Disconnect()
                    except Exception:
                        pass

                DeviceManagerCLI.BuildDeviceList()
                self.device = BenchtopStepperMotor.CreateBenchtopStepperMotor(
                    SERIAL_NUMBER
                )
                self.device.Connect(SERIAL_NUMBER)
                time.sleep(0.3)

                # Inicjalizacja kanałów 1 i 2
                for ch_num in (1, 2):
                    ch = self.device.GetChannel(ch_num)
                    ch.StartPolling(250)
                    ch.EnableDevice()
                    time.sleep(0.2)
                    self.channels[ch_num] = ch

                self.is_connected = True
                self.root.after(0, self._update_ui_connected, True)
                self.log("Połączono pomyślnie z Kinesis .NET API.")
                self.update_positions()

            except Exception as e:
                self.is_connected = False
                self.root.after(0, self._update_ui_connected, False)
                self.log(f"BŁĄD Połączenia .NET: {e}")

        self._run_in_thread(task)

    def action_home(self):
        def task():
            ch_num = self.selected_axis.get()
            self.log(f"Bazowanie osi {ch_num} (.NET)...")
            try:
                ch = self.channels.get(ch_num)
                if ch:
                    ch.Home(60000)  # Timeout 60 sek
                    self.log(f"Oś {ch_num} zbazowana pomyślnie.")
                    self.update_positions()
            except Exception as e:
                self.log(f"BŁĄD bazowania osi {ch_num}: {e}")

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
            ch_num = self.selected_axis.get()
            try:
                ch = self.channels.get(ch_num)
                curr_pos = float(Decimal.ToDouble(ch.Position))
                target = curr_pos + step_mm

                limit_min, limit_max = LIMITS[ch_num - 1]
                if not (limit_min <= target <= limit_max):
                    self.log(
                        f"ODRZUCONO: Cel {target:.3f} mm wykracza poza limit ({limit_min}-{limit_max} mm)."
                    )
                    return

                self.log(
                    f"Ruch osi {ch_num}: {curr_pos:.3f} mm -> {target:.3f} mm (krok: {step_mm:+.3f} mm)"
                )

                # Wykonanie ruchu addytywnego
                ch.MoveBy(Decimal(step_mm), 60000)

                new_pos = float(Decimal.ToDouble(ch.Position))
                self.log(f"Ruch osi {ch_num} zakończony na: {new_pos:.3f} mm")
                self.update_positions()
            except Exception as e:
                self.log(f"BŁĄD ruchu osi {ch_num}: {e}")

        self._run_in_thread(task)

    def update_positions(self):
        if not self.is_connected or 1 not in self.channels:
            return
        try:
            p1 = float(Decimal.ToDouble(self.channels[1].Position))
            p2 = float(Decimal.ToDouble(self.channels[2].Position))
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
            self.lbl_status.config(
                text="Stan: Połączono (.NET API)", foreground="green"
            )
        else:
            self.lbl_status.config(text="Stan: Błąd / Rozłączony", foreground="red")


if __name__ == "__main__":
    root = tk.Tk()
    app = KinesisDotNetGUI(root)
    root.mainloop()
