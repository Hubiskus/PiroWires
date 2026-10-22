"""Measurement routines, filtering, CSV exporters, and focal search algorithms."""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple

from pyrometer_classes import PyroServer, PyroStage


class Measurement(NamedTuple):
    measurement_id: int
    timestamp: float
    position: tuple[float, ...]
    value: float


class Results:
    """Collect processed scan results and raw sample logs."""

    def __init__(self) -> None:
        self.start_time = time.monotonic()
        self._measurements: list[Measurement] = []
        self._raw_samples: list[dict] = []
        self._current_id = 1

    def add_result(
        self,
        value: float,
        position: tuple[float, ...],
        raw_samples: list[float] | None = None,
    ) -> int:
        meas_id = self._current_id
        self._current_id += 1
        elapsed = time.monotonic() - self.start_time

        meas = Measurement(meas_id, elapsed, tuple(map(float, position)), float(value))
        self._measurements.append(meas)

        if raw_samples:
            for s_idx, raw_val in enumerate(raw_samples, start=1):
                self._raw_samples.append(
                    {
                        "measurement_id": meas_id,
                        "sample_index": s_idx,
                        "timestamp": elapsed,
                        "position": position,
                        "raw_value": raw_val,
                    }
                )
        return meas_id

    def get_results(self) -> list[Measurement]:
        return self._measurements

    def get_raw_results(self) -> list[dict]:
        return self._raw_samples


def parse_temperature(response: str) -> float:
    fields = response.split()
    for idx in (4, 3):
        if len(fields) > idx:
            try:
                return float(fields[idx])
            except ValueError:
                pass
    raise ValueError(f"Could not parse numeric temperature from response: {response!r}")


def get_temperature(server: PyroServer) -> float:
    response = server.getTemp()
    return parse_temperature(response)


def robust_temperature_read(
    server: PyroServer,
    samples: int = 21,
    trim: int = 3,
    sample_delay: float = 1.0 / 21,
) -> tuple[float, list[float]]:
    """Collect `samples` temperature readings, trimmed by `trim` extremes on both sides."""
    if samples <= 2 * trim:
        raise ValueError("samples must be greater than twice trim")

    raw_values: list[float] = []
    for _ in range(samples):
        temp_val = get_temperature(server)
        raw_values.append(temp_val)
        if sample_delay > 0:
            time.sleep(sample_delay)

    sorted_vals = sorted(raw_values)
    kept = sorted_vals[trim : len(sorted_vals) - trim] if trim else sorted_vals
    avg_temp = sum(kept) / len(kept)
    return avg_temp, raw_values


def _generate_axis_points(low: float, high: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("Krok musi być dodatni")
    if high < low:
        raise ValueError("Maksimum zakresu musi być >= minimum")
    count = int(round((high - low) / step))
    return [round(low + i * step, 4) for i in range(count + 1)]


def measure_single_point(
    fine_stage: PyroStage,
    server: PyroServer,
    duration_sec: float,
    frequency_hz: float,
    output_prefix: str = "Single_point",
    stop_event: threading.Event | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> str:
    """Perform continuous single-point acquisition at CURRENT stage position (NO STAGE MOVEMENT)."""

    def log(msg: str):
        if log_callback:
            log_callback(msg)
        print(msg)

    pos = fine_stage.get_position()
    x, y, z = pos[0], pos[1], pos[2] if len(pos) > 2 else 0.0

    filename = f"{output_prefix}_X{x:.2f}_Y{y:.2f}_Z{z:.2f}_{duration_sec:.1f}s_{frequency_hz:.1f}Hz.csv"
    path = Path(filename)

    log(
        f"Pomiar stacjonarny na bieżącej pozycji X={x:.3f}, Y={y:.3f}, Z={z:.3f} mm (bez ruchu stolika)."
    )
    log(f"Czas: {duration_sec}s, Częstotliwość: {frequency_hz}Hz.")
    log(f"Zapis do pliku: {path.name}")

    interval = 1.0 / frequency_hz
    total_samples = int(duration_sec * frequency_hz)

    records = []
    start_time = time.monotonic()

    for idx in range(1, total_samples + 1):
        if stop_event is not None and stop_event.is_set():
            log("Pomiar stacjonarny przerwany przez użytkownika.")
            break

        sample_time = time.monotonic() - start_time
        temp_val = get_temperature(server)

        records.append(
            {
                "sample_index": idx,
                "timestamp": sample_time,
                "position": (x, y, z),
                "raw_value": temp_val,
            }
        )
        time.sleep(interval)

    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            [
                "Sample_Index",
                "Timestamp (s)",
                "Position X (mm)",
                "Position Y (mm)",
                "Position Z (mm)",
                "Raw_Temperature",
            ]
        )
        for rec in records:
            writer.writerow(
                [
                    rec["sample_index"],
                    f"{rec['timestamp']:.4f}",
                    rec["position"][0],
                    rec["position"][1],
                    rec["position"][2],
                    rec["raw_value"],
                ]
            )
    log(f"Zapisano {len(records)} próbek do pliku {path.resolve()}")
    return str(path)


def find_focal_plane(
    coarse_stage: PyroStage,
    fine_stage: PyroStage,
    server: PyroServer,
    *,
    x_range: tuple[float, float] = (10.0, 40.0),
    x_step: float = 5.0,
    z_range: tuple[float, float] = (0.0, 4.0),
    z_step: float = 0.1,
    x_fine_span: float = 5.0,
    x_fine_step: float = 1.0,
    z_fine_span: float = 0.2,
    z_fine_step: float = 0.02,
    settle_seconds: float = 0.5,
    output_prefix: str = "Focal_search",
    stop_event: threading.Event | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> tuple[float, float, float]:
    """Search for focal plane, logging step CSVs and outputting a spatially sorted unified dataset for continuous curve plotting."""

    def log(msg: str):
        if log_callback:
            log_callback(msg)
        print(msg)

    # Slownik do przechowywania unikalnych punktow (zapobiega dublowaniu przy ponownym pomiarze)
    points_map: dict[tuple[float, float], dict] = {}

    def save_step_csv(
        phase: str,
        cx: float,
        fz: float,
        temp_val: float,
        raw_samples: list[float] | None = None,
    ):
        step_filename = f"{output_prefix}_{phase}_CX{cx:.2f}_FZ{fz:.3f}.csv"
        step_path = Path(step_filename)
        with step_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["Phase", "Coarse_X_mm", "Fine_Z_mm", "Temperature_degC"])
            writer.writerow([phase, cx, fz, f"{temp_val:.4f}"])
            if raw_samples:
                writer.writerow([])
                writer.writerow(["Sample_Index", "Raw_Temperature_degC"])
                for s_idx, r_val in enumerate(raw_samples, start=1):
                    writer.writerow([s_idx, f"{r_val:.4f}"])

    # --- ETAP 1: Skanowanie zgrubne ---
    log("=== ETAP 1: Rozpoczęcie skanowania zgrubnego ===")
    x_coarse_pts = _generate_axis_points(x_range[0], x_range[1], x_step)
    z_coarse_pts = _generate_axis_points(z_range[0], z_range[1], z_step)

    best_temp = -999.0
    best_x = x_coarse_pts[0]
    best_z = z_coarse_pts[0]

    for x in x_coarse_pts:
        if stop_event and stop_event.is_set():
            break
        coarse_stage.move_to(x, None)
        for z in z_coarse_pts:
            if stop_event and stop_event.is_set():
                break
            fine_stage.move_to(None, None, z)
            if settle_seconds > 0:
                time.sleep(settle_seconds)

            temp_val, raw_samples = robust_temperature_read(
                server, samples=7, trim=1, sample_delay=0.02
            )
            save_step_csv("coarse", x, z, temp_val, raw_samples)

            pos_key = (round(x, 4), round(z, 4))
            points_map[pos_key] = {
                "phase": "coarse",
                "x_coarse_mm": x,
                "z_fine_mm": z,
                "temperature": temp_val,
            }

            log(f"[Zgrubny] X={x:.2f} mm, Z={z:.3f} mm -> Temp={temp_val:.2f} °C")

            if temp_val > best_temp:
                best_temp = temp_val
                best_x = x
                best_z = z

    log(
        f"Maksimum skanu zgrubnego: Temp={best_temp:.2f} °C w X={best_x:.2f} mm, Z={best_z:.3f} mm"
    )

    # --- ETAP 2: Skanowanie zagęszczone (precyzyjne) ---
    if not (stop_event and stop_event.is_set()):
        log("=== ETAP 2: Skanowanie zagęszczone (precyzyjne) ===")
        x_fine_low = max(x_range[0], best_x - x_fine_span)
        x_fine_high = min(x_range[1], best_x + x_fine_span)
        z_fine_low = max(z_range[0], best_z - z_fine_span)
        z_fine_high = min(z_range[1], best_z + z_fine_span)

        x_fine_pts = _generate_axis_points(x_fine_low, x_fine_high, x_fine_step)
        z_fine_pts = _generate_axis_points(z_fine_low, z_fine_high, z_fine_step)

        for x in x_fine_pts:
            if stop_event and stop_event.is_set():
                break
            coarse_stage.move_to(x, None)
            for z in z_fine_pts:
                if stop_event and stop_event.is_set():
                    break
                fine_stage.move_to(None, None, z)
                if settle_seconds > 0:
                    time.sleep(settle_seconds)

                temp_val, raw_samples = robust_temperature_read(
                    server, samples=15, trim=2, sample_delay=0.02
                )
                save_step_csv("fine", x, z, temp_val, raw_samples)

                pos_key = (round(x, 4), round(z, 4))
                # Zapisujemy/nadpisujemy punkt z wyższą dokładnością fazy fine
                points_map[pos_key] = {
                    "phase": "fine",
                    "x_coarse_mm": x,
                    "z_fine_mm": z,
                    "temperature": temp_val,
                }

                log(
                    f"[Precyzyjny] X={x:.2f} mm, Z={z:.3f} mm -> Temp={temp_val:.2f} °C"
                )

                if temp_val > best_temp:
                    best_temp = temp_val
                    best_x = x
                    best_z = z

    log("=== ZAKOŃCZONO POSZUKIWANIE OGNISKA ===")
    log(
        f"Najwyższa temperatura: {best_temp:.2f} °C w położeniu X={best_x:.2f} mm, Z={best_z:.3f} mm."
    )
    log("Ustawianie stolików w wyznaczonym ognisku...")

    coarse_stage.move_to(best_x, None)
    fine_stage.move_to(None, None, best_z)

    # SORTOWANIE PRZESTRZENNE: Scalenie i posortowanie punktów wg współrzędnych (X, Z)
    # Tworzy to jedną spójną krzywą/profil z zagęszczeniem w strefie ogniska
    sorted_records = sorted(
        points_map.values(),
        key=lambda r: (r["x_coarse_mm"], r["z_fine_mm"]),
    )

    summary_path = Path(f"{output_prefix}_summary_sorted.csv")
    with summary_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Phase", "X_Coarse_mm", "Z_Fine_mm", "Temperature_degC"])
        for r in sorted_records:
            writer.writerow(
                [
                    r["phase"],
                    f"{r['x_coarse_mm']:.4f}",
                    f"{r['z_fine_mm']:.4f}",
                    f"{r['temperature']:.4f}",
                ]
            )
    log(
        f"Posortowane podsumowanie (jedna spójna krzywa profilu) zapisano do: {summary_path.resolve()}"
    )

    return best_x, best_z, best_temp


def run_grid_scan(
    fine_stage: PyroStage,
    server: PyroServer,
    *,
    x_range: tuple[float, float] = (1.0, 3.0),
    y_range: tuple[float, float] = (1.0, 3.0),
    z_range: tuple[float, float] = (1.0, 3.0),
    step_x: float = 0.5,
    step_y: float = 0.5,
    step_z: float = 0.5,
    settle_seconds: float = 1.0,
    output_prefix: str = "Test_measurement",
    on_measurement: Callable[[Measurement], None] | None = None,
    stop_event: threading.Event | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> Results:
    """Run 3D grid scan, logging EACH step into a separate CSV named with stage settings."""

    def log(msg: str):
        if log_callback:
            log_callback(msg)
        print(msg)

    if step_x <= 0 or step_y <= 0 or step_z <= 0:
        raise ValueError("steps must be positive")

    ranges = (x_range, y_range, z_range)
    steps = (step_x, step_y, step_z)
    axes = []
    for ax_range, step in zip(ranges, steps):
        low, high = ax_range
        if high < low:
            raise ValueError("Range max must be >= min")
        count = int(round((high - low) / step))
        axes.append([low + i * step for i in range(count + 1)])

    results = Results()
    log("Starting 3D Grid Scan...")

    stopped = False
    for x in axes[0]:
        for z in axes[2]:
            for y in axes[1]:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                target = (x, y, z)
                fine_stage.move_to(*target)

                if settle_seconds > 0:
                    deadline = time.monotonic() + settle_seconds
                    while time.monotonic() < deadline:
                        if stop_event is not None and stop_event.is_set():
                            stopped = True
                            break
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                if stopped:
                    break

                avg_temp, raw_samples = robust_temperature_read(
                    server, samples=21, trim=3, sample_delay=1.0 / 21
                )
                meas_id = results.add_result(avg_temp, target, raw_samples)
                meas = results.get_results()[-1]

                step_filename = f"{output_prefix}_X{x:.2f}_Y{y:.2f}_Z{z:.2f}.csv"
                step_path = Path(step_filename)
                with step_path.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream, delimiter=";")
                    writer.writerow(
                        [
                            "Measurement_ID",
                            "Position_X_mm",
                            "Position_Y_mm",
                            "Position_Z_mm",
                            "Avg_Temperature_degC",
                        ]
                    )
                    writer.writerow(
                        [
                            meas_id,
                            f"{x:.4f}",
                            f"{y:.4f}",
                            f"{z:.4f}",
                            f"{avg_temp:.4f}",
                        ]
                    )
                    writer.writerow([])
                    writer.writerow(["Sample_Index", "Raw_Temperature_degC"])
                    for s_idx, raw_v in enumerate(raw_samples, start=1):
                        writer.writerow([s_idx, f"{raw_v:.4f}"])

                if on_measurement is not None:
                    on_measurement(meas)
                log(
                    f"ID:{meas_id} Pos:({x:.2f}, {y:.2f}, {z:.2f}) Temp:{avg_temp:.2f} °C -> Zapisano {step_filename}"
                )
            if stopped:
                break
        if stopped:
            break

    processed_path = Path(f"{output_prefix}_processed_all.csv")
    raw_path = Path(f"{output_prefix}_raw_all.csv")

    with processed_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            [
                "Measurement_ID",
                "Timestamp (s)",
                "Position X (mm)",
                "Position Y (mm)",
                "Position Z (mm)",
                "Temperature",
            ]
        )
        for m in results.get_results():
            writer.writerow(
                [
                    m.measurement_id,
                    f"{m.timestamp:.4f}",
                    m.position[0],
                    m.position[1],
                    m.position[2],
                    f"{m.value:.4f}",
                ]
            )

    with raw_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            [
                "Measurement_ID",
                "Sample_Index",
                "Timestamp (s)",
                "Position X (mm)",
                "Position Y (mm)",
                "Position Z (mm)",
                "Raw_Temperature",
            ]
        )
        for r in results.get_raw_results():
            writer.writerow(
                [
                    r["measurement_id"],
                    r["sample_index"],
                    f"{r['timestamp']:.4f}",
                    r["position"][0],
                    r["position"][1],
                    r["position"][2],
                    f"{r['raw_value']:.4f}",
                ]
            )

    log(f"Zbiorcze podsumowanie zapisano do: {processed_path.resolve()}")
    return results
