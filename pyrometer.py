import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import pylablib as pll
from pylablib.devices import Thorlabs


# ==============================================================================
# KLASY WYJĄTKÓW I OBSŁUGI SPRZĘTU
# ==============================================================================
class ThorlabsError(Exception):
    pass


class DeviceNotConnected(ThorlabsError):
    pass


class DeviceNotFound(ThorlabsError):
    pass


class ThorlabsStage:
    def __init__(
        self,
        device_name: str,
        port_id: str,
        steps_per_mm: float,
        num_channels: int,
        limits: list[tuple[float, float]],
    ):
        """
        :param device_name: Nazwa stolika (np. 'Stolik Zgrubny XY')
        :param port_id: Numer seryjny/PORT (np. '70817296')
        :param steps_per_mm: Przelicznik kroków na mm
        :param num_channels: Liczba osi/kanałów (2 lub 3)
        :param limits: Lista krotek z limitami osi [(min_x, max_x), (min_y, max_y), ...]
        """
        self.device_name = device_name
        self.port_id = str(port_id)
        self.steps_per_mm = steps_per_mm
        self.num_channels = num_channels
        self.limits = limits
        self.stage: Thorlabs.KinesisMotor | None = None

    def connect(self):
        thor_devices = Thorlabs.list_kinesis_devices()
        print(f"[{self.device_name}] Znalezione urządzenia:", thor_devices)

        found = any(dev[0] == self.port_id for dev in thor_devices)
        if not found:
            raise DeviceNotFound(
                f"Nie znaleziono urządzenia {self.device_name} (ID: {self.port_id})."
            )

        self.stage = Thorlabs.KinesisMotor(
            self.port_id, is_rack_system=True, scale=self.steps_per_mm
        )

        if self.stage is None:
            raise DeviceNotConnected(
                f"Błąd połączenia ze stolikiem {self.device_name}."
            )

        self.stage.set_supported_channels(self.num_channels)
        print(f"[{self.device_name}] Połączono pomyślnie (PORT ID: {self.port_id}).")

    def _mm_to_steps(self, mm: float) -> int:
        return int(mm * self.steps_per_mm)

    def _steps_to_mm(self, steps: int) -> float:
        return steps / self.steps_per_mm

    def home(self, channel: int | None = None):
        if self.stage is None:
            raise DeviceNotConnected(f"Stolik {self.device_name} niepodłączony.")

        channels = (
            [channel] if channel is not None else list(range(1, self.num_channels + 1))
        )
        for ch in channels:
            if self.stage.is_homed(ch):
                print(f"[{self.device_name}] Os {ch} jest już zabazowana.")
                continue

            self.stage.home(channel=ch, sync=False, force=True)
            print(f"[{self.device_name}] Rozpoczęto bazowanie osi {ch}...")
            try:
                self.stage.wait_for_home(channel=ch, timeout=30)
            except Thorlabs.ThorlabsTimeoutError as e:
                raise RuntimeError(
                    f"Przekroczono czas bazowania dla {self.device_name}, oś {ch}."
                ) from e
            print(
                f"[{self.device_name}] Os {ch} zabazowana:",
                self.stage.is_homed(ch),
            )

    def move_to(self, targets: list[float | None]):
        """
        targets: lista wartości pozycji dla osi [X, Y] lub [X, Y, Z].
        Wartość None oznacza brak ruchu dla danej osi.
        """
        if self.stage is None:
            raise DeviceNotConnected(f"Stolik {self.device_name} niepodłączony.")

        for i, target in enumerate(targets):
            if target is not None:
                ch = i + 1
                min_lim, max_lim = self.limits[i]
                if not (min_lim <= target <= max_lim):
                    raise ValueError(
                        f"[{self.device_name}] Pozycja Osi {ch} ({target} mm) poza zakresem [{min_lim}, {max_lim}] mm."
                    )
                self.stage.move_to(self._mm_to_steps(target), channel=ch, scale=False)
                self.stage.wait_move(channel=ch, timeout=30)

    def move_relative(self, deltas: list[float | None]):
        if self.stage is None:
            raise DeviceNotConnected(f"Stolik {self.device_name} niepodłączony.")

        current_positions = self.get_position()
        targets = []
        for i, delta in enumerate(deltas):
            if delta is not None:
                targets.append(current_positions[i] + delta)
            else:
                targets.append(None)

        self.move_to(targets)

    def get_position(self) -> list[float]:
        if self.stage is None:
            raise DeviceNotConnected(f"Stolik {self.device_name} niepodłączony.")

        positions = []
        for ch in range(1, self.num_channels + 1):
            steps = self.stage.get_position(channel=ch, scale=False)
            positions.append(round(self._steps_to_mm(steps), 4))
        return positions


# ==============================================================================
# INTERFEJS GRAFICZNY (TKINTER GUI)
# ==============================================================================
class StageControlPanel(ttk.LabelFrame):
    """Panel kontrolny dla pojedynczego stolika (XY lub XYZ)"""

    def __init__(self, parent, stage: ThorlabsStage, log_callback):
        super().__init__(parent, text=f" {stage.device_name} ", padding=10)
        self.stage = stage
        self.log = log_callback
        self.axis_names = ["X", "Y", "Z"][: stage.num_channels]

        self.entries_abs = {}
        self.entries_rel = {}
        self.pos_labels = {}

        self._create_widgets()

    def _create_widgets(self):
        # 1. Sekcja aktualnej pozycji
        pos_frame = ttk.Frame(self)
        pos_frame.pack(fill="x", pady=5)

        ttk.Label(
            pos_frame, text="Aktualna pozycja: ", font=("Helvetica", 9, "bold")
        ).pack(side="left")
        for i, name in enumerate(self.axis_names):
            lbl = ttk.Label(pos_frame, text=f"{name}: --- mm ", font=("Consolas", 10))
            lbl.pack(side="left", padx=5)
            self.pos_labels[i] = lbl

        # 2. Przycisk Homing
        btn_home = ttk.Button(
            self, text="Bazuj wszystkie osie (Home)", command=self._on_home
        )
        btn_home.pack(fill="x", pady=5)

        # 3. Sekcja Ruchu Bezwzględnego (Move Absolute)
        abs_frame = ttk.LabelFrame(self, text=" Ruch bezwzględny (mm) ")
        abs_frame.pack(fill="x", pady=5)

        for i, name in enumerate(self.axis_names):
            min_lim, max_lim = self.stage.limits[i]
            row_f = ttk.Frame(abs_frame)
            row_f.pack(fill="x", padx=5, pady=2)

            ttk.Label(
                row_f, text=f"Os {name} [{min_lim}-{max_lim} mm]:", width=20
            ).pack(side="left")
            entry = ttk.Entry(row_f, width=10)
            entry.pack(side="left", padx=5)
            self.entries_abs[i] = entry

        ttk.Button(abs_frame, text="Jedź do pozycji", command=self._on_move_abs).pack(
            fill="x", padx=5, pady=5
        )

        # 4. Sekcja Ruchu Względnego (Move Relative)
        rel_frame = ttk.LabelFrame(self, text=" Ruch względny (Δ mm) ")
        rel_frame.pack(fill="x", pady=5)

        for i, name in enumerate(self.axis_names):
            row_f = ttk.Frame(rel_frame)
            row_f.pack(fill="x", padx=5, pady=2)

            ttk.Label(row_f, text=f"Delta {name}:", width=20).pack(side="left")
            entry = ttk.Entry(row_f, width=10)
            entry.pack(side="left", padx=5)
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
        """Uruchamia operację ruchu w osobnym wątku"""

        def worker():
            try:
                target_func(*args)
                self.update_position_display()
                self.log(f"[{self.stage.device_name}] Operacja zakończona pomyślnie.")
            except Exception as e:
                self.log(f"[{self.stage.device_name}] BŁĄD: {e}")
                messagebox.showerror("Błąd ruchu", str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_home(self):
        self.log(f"[{self.stage.device_name}] Rozpoczynanie bazowania...")
        self._run_in_thread(self.stage.home)

    def _on_move_abs(self):
        targets = []
        for i in range(self.stage.num_channels):
            val_str = self.entries_abs[i].get().strip()
            if val_str:
                try:
                    targets.append(float(val_str))
                except ValueError:
                    messagebox.showerror(
                        "Błąd wejścia",
                        f"Nieprawidłowa wartość dla osi {self.axis_names[i]}",
                    )
                    return
            else:
                targets.append(None)

        if all(t is None for t in targets):
            return

        self.log(f"[{self.stage.device_name}] Ruch bezwzględny do pozycji: {targets}")
        self._run_in_thread(self.stage.move_to, targets)

    def _on_move_rel(self):
        deltas = []
        for i in range(self.stage.num_channels):
            val_str = self.entries_rel[i].get().strip()
            if val_str:
                try:
                    deltas.append(float(val_str))
                except ValueError:
                    messagebox.showerror(
                        "Błąd wejścia",
                        f"Nieprawidłowa wartość delty dla osi {self.axis_names[i]}",
                    )
                    return
            else:
                deltas.append(None)

        if all(d is None for d in deltas):
            return

        self.log(f"[{self.stage.device_name}] Ruch względny o przesunięcia: {deltas}")
        self._run_in_thread(self.stage.move_relative, deltas)


class ThorlabsGUI(tk.Tk):
    def __init__(self, coarse_stage: ThorlabsStage, fine_stage: ThorlabsStage):
        super().__init__()
        self.title("Thorlabs Dual Stage Controller")
        self.geometry("750x650")

        self.coarse_stage = coarse_stage
        self.fine_stage = fine_stage

        self._create_layout()
        self._connect_devices()

    def _create_layout(self):
        # Główny kontener dwóch paneli
        stages_frame = ttk.Frame(self, padding=10)
        stages_frame.pack(fill="both", expand=True)

        # Left: Stolik Zgrubny XY
        self.panel_coarse = StageControlPanel(
            stages_frame, self.coarse_stage, self.log_message
        )
        self.panel_coarse.pack(side="left", fill="both", expand=True, padx=5, pady=5)

        # Right: Stolik Precyzyjny XYZ
        self.panel_fine = StageControlPanel(
            stages_frame, self.fine_stage, self.log_message
        )
        self.panel_fine.pack(side="right", fill="both", expand=True, padx=5, pady=5)

        # Dolny panel konsoli / logów
        log_frame = ttk.LabelFrame(self, text=" Dziennik zdarzeń ", padding=5)
        log_frame.pack(fill="x", padx=15, pady=10)

        self.log_text = tk.Text(log_frame, height=8, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def log_message(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}\n"

        self.log_text.config(state="normal")
        self.log_text.insert("end", formatted)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _connect_devices(self):
        def connect_worker():
            # Połączenie ze stolikiem Zgrubnym
            try:
                self.coarse_stage.connect()
                self.panel_coarse.update_position_display()
                self.log_message("Stolik Zgrubny XY gotowy.")
            except Exception as e:
                self.log_message(f"Błąd połączenia z Zgrubnym XY: {e}")

            # Połączenie ze stolikiem Precyzyjnym
            try:
                self.fine_stage.connect()
                self.panel_fine.update_position_display()
                self.log_message("Stolik Precyzyjny XYZ gotowy.")
            except Exception as e:
                self.log_message(f"Błąd połączenia z Precyzyjnym XYZ: {e}")

        threading.Thread(target=connect_worker, daemon=True).start()


# ==============================================================================
# MAIN PROGRAM
# ==============================================================================
def main():
    # 1. Konfiguracja Stolika Zgrubnego XY
    # - Port ID: 70817296
    # - Os 1 (X): 0 do 50 mm, Os 2 (Y): 0 do 75 mm
    # - Przelicznik: 2560 kroków/mm
    coarse_stage = ThorlabsStage(
        device_name="Stolik Zgrubny XY",
        port_id="70817296",
        steps_per_mm=2560,
        num_channels=2,
        limits=[(0.0, 50.0), (0.0, 75.0)],
    )

    # 2. Konfiguracja Stolika Precyzyjnego XYZ
    # - Port ID: 70821739
    # - Osie X, Y, Z: 0 do 4 mm każda
    # - Przelicznik: 51200 kroków/mm
    fine_stage = ThorlabsStage(
        device_name="Stolik Precyzyjny XYZ",
        port_id="70821739",
        steps_per_mm=51200,
        num_channels=3,
        limits=[(0.0, 4.0), (0.0, 4.0), (0.0, 4.0)],
    )

    # 3. Uruchomienie aplikacji GUI
    app = ThorlabsGUI(coarse_stage=coarse_stage, fine_stage=fine_stage)
    app.mainloop()


if __name__ == "__main__":
    main()
