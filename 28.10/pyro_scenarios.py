"""
pyro_scenarios.py - Logika scenariuszy pomiarowych pirometru.
"""
from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import stage_control as sc
from pyrometer_classes import ConnectionError as PyroConnectionError, PyroServer

TEMPERATURE_FIELD_INDEX = 3
ROUND_SIGNAL = 15        # Precyzja zaokrąglenia prądu detektorów (A)
ROUND_SIGNAL_LEVEL = 6  # Precyzja zaokrąglenia poziomu sygnału detektorów (%)


class ScanStopped(Exception):
    """Sygnał przerwania skanowania przez użytkownika."""
    pass


class ScanError(Exception):
    """Błąd wykonania scenariusza pomiarowego."""
    def __init__(self, message: str, result: ScanResult):
        super().__init__(message)
        self.result = result


def parse_temperature(response: str, field_index: int = TEMPERATURE_FIELD_INDEX) -> float:
    fields = response.split()
    if len(fields) <= field_index:
        raise ValueError(f"Odpowiedź pirometru ma za mało pól: {response!r}")
    try:
        return float(fields[field_index])
    except ValueError as exc:
        raise ValueError(f"Pole {field_index} nie jest liczbą: {response!r}") from exc


class PyroReader:
    """Odczyt pirometru z obsługą automatycznego ponawiania połączenia."""
    def __init__(self, server: PyroServer, field_index: int = TEMPERATURE_FIELD_INDEX,
                 max_retries: int = 5, retry_delay: float = 0.25):
        self.server = server
        self.field_index = int(field_index)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.current_mode: str = "unknown"

    def read_raw(self) -> str:
        tries = 0
        while True:
            try:
                if not self.server.isConnected:
                    self.server.connect()
                return self.server.getTemp()
            except (TimeoutError, PyroConnectionError, OSError):
                tries += 1
                if tries > self.max_retries:
                    raise
                time.sleep(self.retry_delay)
                self.server.disconnect()
                time.sleep(self.retry_delay)

    def read(self) -> float:
        return parse_temperature(self.read_raw(), self.field_index)

    def read_signal(self) -> tuple[float, float]:
        tries = 0
        while True:
            try:
                if not self.server.isConnected:
                    self.server.connect()
                return self.server.getSignal()
            except (TimeoutError, PyroConnectionError, OSError, ValueError):
                tries += 1
                if tries > self.max_retries:
                    raise
                time.sleep(self.retry_delay)
                self.server.disconnect()
                time.sleep(self.retry_delay)

    def read_signal_level(self) -> tuple[float, float]:
        tries = 0
        while True:
            try:
                if not self.server.isConnected:
                    self.server.connect()
                return self.server.getSignalLevel()
            except (TimeoutError, PyroConnectionError, OSError, ValueError):
                tries += 1
                if tries > self.max_retries:
                    raise
                time.sleep(self.retry_delay)
                self.server.disconnect()
                time.sleep(self.retry_delay)

    def read_full(self) -> tuple[float, tuple[float, float], tuple[float, float]]:
        """Zwraca (temperatura, (sig1, sig2), (lvl1, lvl2))."""
        temp = self.read()
        sig = self.read_signal()
        lvl = self.read_signal_level()
        return temp, sig, lvl

    def get_mode(self) -> str:
        try:
            mode = self.server.getPyroType()
            self.current_mode = mode
            return mode
        except Exception:
            return self.current_mode

    def set_mode(self, mode: str) -> str:
        try:
            self.server.setPyroType(mode)
            return self.get_mode()
        except Exception:
            return self.current_mode


def trimmed_stats(values: list[float], drop_low: int = 3, drop_high: int = 3) -> tuple[float, float, set[int]]:
    """Odrzuca `drop_low` najmniejszych i `drop_high` największych wartości."""
    n = len(values)
    if drop_low < 0 or drop_high < 0 or n <= drop_low + drop_high:
        raise ValueError("Za mało próbek w stosunku do liczby odrzucanych")
    order = sorted(range(n), key=values.__getitem__)
    kept_idx = set(order[drop_low:n - drop_high])
    kept = [values[i] for i in kept_idx]
    mean = sum(kept) / len(kept)
    var = sum((v - mean) ** 2 for v in kept) / (len(kept) - 1) if len(kept) > 1 else 0.0
    return mean, math.sqrt(var), kept_idx


def frange(start: float, stop: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("Krok musi być dodatni")
    n = int(math.floor((stop - start) / step + 1e-9))
    return [round(start + i * step, 6) for i in range(n + 1)]


def _new_paths(output_dir: str, prefix: str, mode: str, with_raw: bool) -> tuple[Path, Path | None]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%d%m%Y_%H%M%S")
    mode_str = f"_{mode}" if mode and mode != "unknown" else ""
    main = out / f"{prefix}{mode_str}_{stamp}.csv"
    raw = out / f"{prefix}{mode_str}_{stamp}_raw.csv" if with_raw else None
    return main, raw


# ---------------- Scenariusz 1: Pomiar Stacjonarny ----------------
@dataclass
class StationaryParams:
    frequency_hz: float = 10.0
    duration_s: float = 60.0
    output_dir: str = "measurements"
    prefix: str = "pyro_stationary"


def run_stationary(reader: PyroReader, p: StationaryParams, stop_event: threading.Event, *,
                   on_sample: Callable[[float, float], None] | None = None,
                   on_progress: Callable[[float, float, int], None] | None = None) -> tuple[Path, int, bool]:
    mode = reader.current_mode if reader.current_mode != "unknown" else reader.get_mode()
    path, _ = _new_paths(p.output_dir, p.prefix, mode, False)
    period = 1.0 / p.frequency_hz
    n = 0
    stopped = False
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Pomiar stacjonarny pirometru", f"Tryb: {mode}"])
        writer.writerow(["Czestotliwosc [Hz]:", p.frequency_hz, "Czas trwania [s]:", p.duration_s])
        writer.writerow(["Czas (s)", "Temperatura (C)", "Sygnal Det 1 (A)", "Sygnal Det 2 (A)", "Poziom Det 1 (%)", "Poziom Det 2 (%)"])
        t0 = time.perf_counter()
        next_t = t0
        while True:
            now = time.perf_counter()
            if now - t0 >= p.duration_s or stop_event.is_set():
                if stop_event.is_set():
                    stopped = True
                break
            wait = next_t - now
            if wait > 0 and stop_event.wait(wait):
                stopped = True
                break
            
            temp, (sig1, sig2), (lvl1, lvl2) = reader.read_full()
            ts = time.perf_counter() - t0
            writer.writerow([
                round(ts, 4),
                round(temp, 4),
                round(sig1, ROUND_SIGNAL),
                round(sig2, ROUND_SIGNAL),
                round(lvl1, ROUND_SIGNAL_LEVEL),
                round(lvl2, ROUND_SIGNAL_LEVEL)
            ])
            n += 1
            if on_sample:
                on_sample(ts, temp)
            if on_progress:
                on_progress(ts, p.duration_s, n)
            next_t += period
            if next_t < time.perf_counter():
                next_t = time.perf_counter()
    return path, n, stopped


# ---------------- Scenariusz 2: Pomiar Krokowy / Ognisko ----------------
@dataclass
class ScanParams:
    x_axis: str = "max_x"
    z_axis: str = "nano_z"
    x_start: float = 0.0
    x_stop: float = 50.0
    x_step: float = 5.0
    z_start: float = 0.0
    z_stop: float = 4.0
    z_step: float = 0.1
    settle_s: float = 1.0
    n_samples: int = 21
    drop_low: int = 3
    drop_high: int = 3
    sample_delay_s: float = 0.05
    # Lokalne dogęszczanie Z na każdym X
    refine_local_z: bool = True
    refine_local_z_halfwidth: float = 0.3
    refine_local_z_step: float = 0.02
    # Globalne dogęszczanie X i Z wokół ogniska
    refine: bool = True
    refine_x_halfwidth: float = 5.0
    refine_x_step: float = 1.0
    refine_z_halfwidth: float = 0.2
    refine_z_step: float = 0.02
    output_dir: str = "measurements"
    prefix: str = "pyro_focal_scan"
    save_raw: bool = True


@dataclass
class ScanPoint:
    phase: str
    timestamp: float
    x: float
    z: float
    mean: float
    std: float
    n_kept: int
    n_samples: int
    mean_sig1: float = 0.0
    mean_sig2: float = 0.0
    mean_lvl1: float = 0.0
    mean_lvl2: float = 0.0


@dataclass
class ScanResult:
    points: list[ScanPoint]
    focus: ScanPoint | None
    summary_path: Path | None
    raw_path: Path | None
    stopped: bool


def run_stepwise_scan(stages: sc.Stages, reader: PyroReader, p: ScanParams, stop_event: threading.Event, *,
                      on_progress: Callable[[str, int, int], None] | None = None,
                      on_point: Callable[[ScanPoint], None] | None = None,
                      on_info: Callable[[str], None] | None = None) -> ScanResult:
    info = on_info or (lambda text: None)
    mode = reader.current_mode if reader.current_mode != "unknown" else reader.get_mode()
    summary_path, raw_path = _new_paths(p.output_dir, p.prefix, mode, p.save_raw)
    points: dict[tuple[float, float], ScanPoint] = {}
    t0 = time.monotonic()
    raw_stream = raw_writer = None
    stopped = False
    failure: Exception | None = None

    def check_stop():
        if stop_event.is_set():
            raise ScanStopped()

    def best() -> ScanPoint | None:
        return max(points.values(), key=lambda q: q.mean) if points else None

    def measure(x: float, z: float, phase: str) -> None:
        key = (round(x, 6), round(z, 6))
        if key in points:
            return
        check_stop()
        stages.move_to(p.x_axis, x)
        stages.move_to(p.z_axis, z)
        
        if p.settle_s > 0 and stop_event.wait(p.settle_s):
            raise ScanStopped()
            
        temps, sigs1, sigs2, lvls1, lvls2 = [], [], [], [], []
        for i in range(p.n_samples):
            check_stop()
            t_val, (s1, s2), (l1, l2) = reader.read_full()
            temps.append(t_val)
            sigs1.append(s1)
            sigs2.append(s2)
            lvls1.append(l1)
            lvls2.append(l2)
            if p.sample_delay_s > 0 and i < p.n_samples - 1 and stop_event.wait(p.sample_delay_s):
                raise ScanStopped()
                
        mean_temp, std_temp, kept = trimmed_stats(temps, p.drop_low, p.drop_high)
        mean_sig1 = sum(sigs1[i] for i in kept) / len(kept)
        mean_sig2 = sum(sigs2[i] for i in kept) / len(kept)
        mean_lvl1 = sum(lvls1[i] for i in kept) / len(kept)
        mean_lvl2 = sum(lvls2[i] for i in kept) / len(kept)

        pt = ScanPoint(
            phase=phase,
            timestamp=time.monotonic() - t0,
            x=key[0],
            z=key[1],
            mean=mean_temp,
            std=std_temp,
            n_kept=len(kept),
            n_samples=len(temps),
            mean_sig1=mean_sig1,
            mean_sig2=mean_sig2,
            mean_lvl1=mean_lvl1,
            mean_lvl2=mean_lvl2
        )
        points[key] = pt
        
        if raw_writer is not None:
            for i in range(len(temps)):
                raw_writer.writerow([
                    round(pt.timestamp, 3), key[0], key[1], i + 1,
                    round(temps[i], 4),
                    round(sigs1[i], ROUND_SIGNAL),
                    round(sigs2[i], ROUND_SIGNAL),
                    round(lvls1[i], ROUND_SIGNAL_LEVEL),
                    round(lvls2[i], ROUND_SIGNAL_LEVEL),
                    1 if i in kept else 0
                ])
            raw_stream.flush()
        if on_point:
            on_point(pt)

    try:
        if raw_path is not None:
            raw_stream = raw_path.open("w", newline="", encoding="utf-8")
            raw_writer = csv.writer(raw_stream, delimiter=";")
            raw_writer.writerow(["Skan krokowy pirometru - surowe probki", f"Tryb: {mode}"])
            raw_writer.writerow(["Czas (s)", "Pozycja X (mm)", "Pozycja Z (mm)", "Probka nr", "Temperatura (C)", "Sygnal Det 1 (A)", "Sygnal Det 2 (A)", "Poziom Det 1 (%)", "Poziom Det 2 (%)", "Uwzgledniona w sredniej (1/0)"])

        xs = frange(p.x_start, p.x_stop, p.x_step)
        zs = frange(p.z_start, p.z_stop, p.z_step)

        est_local_z = len(frange(-p.refine_local_z_halfwidth, p.refine_local_z_halfwidth, p.refine_local_z_step)) if p.refine_local_z else 0
        total_points_est = len(xs) * (len(zs) + est_local_z)
        done_points = 0

        info(f"Skanowanie: {len(xs)} kroków X")

        for x in xs:
            # 1. Zgrubny przejazd w Z dla aktualnego X
            for z in zs:
                measure(x, z, "coarse")
                done_points += 1
                if on_progress:
                    on_progress("coarse", done_points, total_points_est)

            # 2. Lokalne dogęszczanie wokół maksimum na tym konkretnym X
            if p.refine_local_z:
                pts_x = [pt for pt in points.values() if pt.x == round(x, 6)]
                if pts_x:
                    local_peak = max(pts_x, key=lambda q: q.mean)
                    lo_z = max(p.z_start, local_peak.z - p.refine_local_z_halfwidth)
                    hi_z = min(p.z_stop, local_peak.z + p.refine_local_z_halfwidth)
                    done_z = {pt.z for pt in pts_x}
                    new_zs = [z for z in frange(lo_z, hi_z, p.refine_local_z_step) if z not in done_z]

                    if new_zs:
                        info(f"Lokalne dogęszczanie Z na X = {x:g} mm wokół {local_peak.z:g} mm ({len(new_zs)} pkt)")
                        for z in new_zs:
                            measure(x, z, "refine_local_z")
                            done_points += 1
                            if on_progress:
                                on_progress("refine_local_z", done_points, total_points_est)

        focus = best()
        if focus:
            info(f"Wyznaczone ognisko (zgrubne/lokalne): X = {focus.x:g} mm, Z = {focus.z:g} mm, Temp = {focus.mean:.2f} C")

        # 3. Globalne dogęszczanie osi X i Z wokół ogniska po skanowaniu wszystkich płaszczyzn X
        if p.refine and focus:
            lo_x = max(p.x_start, focus.x - p.refine_x_halfwidth)
            hi_x = min(p.x_stop, focus.x + p.refine_x_halfwidth)
            done_x = {k[0] for k in points}
            new_xs = [x for x in frange(lo_x, hi_x, p.refine_x_step) if x not in done_x]
            if new_xs:
                info(f"Zagęszczanie globalne X wokół {focus.x:g} mm ({len(new_xs)} nowych płaszczyzn)")
                for x in new_xs:
                    for z in zs:
                        measure(x, z, "refine_x")
                        done_points += 1
                        if on_progress:
                            on_progress("refine_x", done_points, total_points_est)
                focus = best()

            if focus:
                lo_z = max(p.z_start, focus.z - p.refine_z_halfwidth)
                hi_z = min(p.z_stop, focus.z + p.refine_z_halfwidth)
                done_z = {k[1] for k in points if k[0] == round(focus.x, 6)}
                new_zs = [z for z in frange(lo_z, hi_z, p.refine_z_step) if z not in done_z]
                if new_zs:
                    info(f"Zagęszczanie globalne Z wokół {focus.z:g} mm na X = {focus.x:g} mm ({len(new_zs)} nowych pkt)")
                    for z in new_zs:
                        measure(focus.x, z, "refine_z")
                        done_points += 1
                        if on_progress:
                            on_progress("refine_z", done_points, total_points_est)
                focus = best()

            if focus:
                info(f"Ognisko końcowe: X = {focus.x:g} mm, Z = {focus.z:g} mm, Temp = {focus.mean:.2f} C")

    except (ScanStopped, sc.MoveAborted):
        stopped = True
    except Exception as exc:
        failure = exc
    finally:
        if raw_stream is not None:
            raw_stream.close()
        focus_final = best()
        if points:
            sorted_points = sorted(points.values(), key=lambda q: (q.x, q.z))
            with summary_path.open("w", newline="", encoding="utf-8") as stream:
                w = csv.writer(stream, delimiter=";")
                w.writerow(["Podsumowanie skanu krokowego / ogniska", f"Tryb: {mode}"])
                if focus_final:
                    w.writerow([
                        "Wyznaczone ognisko X [mm]:", focus_final.x,
                        "Z [mm]:", focus_final.z,
                        "Max Temp [C]:", round(focus_final.mean, 4),
                        "Sygnal Det 1 [A]:", round(focus_final.mean_sig1, ROUND_SIGNAL),
                        "Sygnal Det 2 [A]:", round(focus_final.mean_sig2, ROUND_SIGNAL),
                        "Poziom Det 1 [%]:", round(focus_final.mean_lvl1, ROUND_SIGNAL_LEVEL),
                        "Poziom Det 2 [%]:", round(focus_final.mean_lvl2, ROUND_SIGNAL_LEVEL)
                    ])
                w.writerow(["Faza", "X (mm)", "Z (mm)", "Srednia temp (C)", "Odchylenie std", "Sygnal Det 1 (A)", "Sygnal Det 2 (A)", "Poziom Det 1 (%)", "Poziom Det 2 (%)", "Uzytych probek", "Liczba probek", "Czas (s)"])
                for q in sorted_points:
                    w.writerow([
                        q.phase, q.x, q.z,
                        round(q.mean, 4),
                        round(q.std, 4),
                        round(q.mean_sig1, ROUND_SIGNAL),
                        round(q.mean_sig2, ROUND_SIGNAL),
                        round(q.mean_lvl1, ROUND_SIGNAL_LEVEL),
                        round(q.mean_lvl2, ROUND_SIGNAL_LEVEL),
                        q.n_kept, q.n_samples,
                        round(q.timestamp, 3)
                    ])

    result = ScanResult(list(points.values()), best(), summary_path if points else None, raw_path if points else None, stopped)
    if failure is not None:
        raise ScanError(str(failure), result) from failure
    return result