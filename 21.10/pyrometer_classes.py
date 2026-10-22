"""Hardware access classes for Thorlabs Kinesis stages and PyroServer TCP data source.

Supports both 2-axis (Coarse XY) and 3-axis (Fine XYZ) stage configurations.
"""

from __future__ import annotations

import socket
import threading
import time
from enum import Enum
from typing import Any

try:
    from pylablib.devices import Thorlabs
except ImportError:
    Thorlabs = None


class ThorlabsError(RuntimeError):
    """Base error for stage operations."""


class DeviceNotConnected(ThorlabsError):
    """The stage has not been connected, or the connection was lost."""


class DeviceNotFound(ThorlabsError):
    """No matching Thorlabs device was found."""


class ConnectionError(RuntimeError):
    """Connection or communication failure with the data server."""


class PyroStage:
    """Wrapper for Thorlabs Kinesis stages (2-axis or 3-axis).

    Public position units are millimetres.
    """

    def __init__(
        self,
        device_name: str = "Kinesis Stage",
        serial_number: str | None = "70821739",
        steps_per_mm: float = 51200,
        travel_limits_mm: tuple[float, float] | list[tuple[float, float]] | None = (
            0.0,
            4.0,
        ),
        num_channels: int = 3,
    ) -> None:
        if steps_per_mm <= 0:
            raise ValueError("steps_per_mm must be positive")
        if num_channels not in (2, 3):
            raise ValueError("num_channels must be 2 or 3")

        self.device_name = device_name
        self.serial_number = str(serial_number) if serial_number is not None else None
        self.steps_per_mm = float(steps_per_mm)
        self.num_channels = num_channels
        self.axis_channels = tuple(range(1, num_channels + 1))

        # Normalizacja limitów dla poszczególnych osi
        if travel_limits_mm is None:
            self.travel_limits_mm = None
        elif (
            isinstance(travel_limits_mm, (list, tuple))
            and len(travel_limits_mm) > 0
            and isinstance(travel_limits_mm[0], (int, float))
        ):
            self.travel_limits_mm = [
                (float(travel_limits_mm[0]), float(travel_limits_mm[1]))
                for _ in range(num_channels)
            ]
        else:
            self.travel_limits_mm = [
                (float(lim[0]), float(lim[1])) for lim in travel_limits_mm
            ]

        self.stage: Thorlabs.KinesisMotor | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _device_serial(device_info: Any) -> str | None:
        if isinstance(device_info, (tuple, list)) and device_info:
            return str(device_info[0])
        if isinstance(device_info, dict):
            for key in ("serial", "serial_number", "SN"):
                if key in device_info:
                    return str(device_info[key])
        return None

    def connect(self) -> None:
        if Thorlabs is None:
            raise ImportError(
                "pylablib is required for Thorlabs stage control. Install it with `pip install pylablib`."
            )

        with self._lock:
            if self.stage is not None:
                return
            devices = Thorlabs.list_kinesis_devices()
            if not devices:
                raise DeviceNotFound("No Kinesis devices were detected.")

            selected = None
            if self.serial_number is None:
                selected = devices[0]
            else:
                for item in devices:
                    serial = self._device_serial(item)
                    if serial == self.serial_number:
                        selected = item
                        break
                if selected is None:
                    found = ", ".join(
                        filter(
                            None,
                            (self._device_serial(d) for d in devices),
                        )
                    )
                    raise DeviceNotFound(
                        f"Kinesis device {self.serial_number!r} was not found. Detected serials: {found or devices!r}"
                    )

            serial = self._device_serial(selected)
            if serial is None:
                raise DeviceNotFound(
                    f"Could not identify a serial number in device entry: {selected!r}"
                )

            stage = None
            try:
                stage = Thorlabs.KinesisMotor(
                    serial,
                    is_rack_system=True,
                    scale=self.steps_per_mm,
                )
                stage.set_supported_channels(self.num_channels)
            except Exception:
                if stage is not None:
                    try:
                        stage.close()
                    except Exception:
                        pass
                raise
            self.stage = stage

    def disconnect(self) -> None:
        with self._lock:
            stage, self.stage = self.stage, None
            if stage is not None:
                try:
                    stage.close()
                except Exception:
                    pass

    close = disconnect

    def _require_stage(self):
        if self.stage is None:
            raise DeviceNotConnected(f"Connect to the {self.device_name} stage first.")
        return self.stage

    def _validate_position(self, value: float, axis_index: int) -> float:
        value = float(value)
        if self.travel_limits_mm is not None:
            low, high = self.travel_limits_mm[axis_index]
            if not low <= value <= high:
                raise ValueError(
                    f"[{self.device_name}] Position {value:g} mm on Axis {axis_index + 1} is outside limits [{low:g}, {high:g}] mm."
                )
        return value

    def home_axis(self, axis: int, timeout: float = 30.0) -> None:
        if axis not in self.axis_channels:
            raise ValueError(f"axis must be one of {self.axis_channels}")
        stage = self._require_stage()
        with self._lock:
            if stage.is_homed(channel=axis):
                return
            stage.home(channel=axis, sync=False, force=True)
            try:
                stage.wait_for_home(channel=axis, timeout=timeout)
            except Exception as exc:
                timeout_type = (
                    getattr(Thorlabs, "ThorlabsTimeoutError", ()) if Thorlabs else ()
                )
                if timeout_type and isinstance(exc, timeout_type):
                    raise TimeoutError(f"Homing timed out on axis {axis}.") from exc
                raise
            if not stage.is_homed(channel=axis):
                raise ThorlabsError(
                    f"Axis {axis} finished homing but is not reporting homed."
                )

    def home(self, timeout: float = 30.0) -> None:
        for channel in self.axis_channels:
            self.home_axis(channel, timeout=timeout)

    def is_homed(self, axis: int) -> bool:
        if axis not in self.axis_channels:
            raise ValueError(f"axis must be one of {self.axis_channels}")
        return bool(self._require_stage().is_homed(channel=axis))

    def move_to(self, *targets: float | None, timeout: float = 30.0) -> None:
        stage = self._require_stage()
        with self._lock:
            for idx, (channel, target) in enumerate(zip(self.axis_channels, targets)):
                if target is None:
                    continue
                validated = self._validate_position(target, idx)
                stage.move_to(
                    int(round(validated * self.steps_per_mm)),
                    channel,
                    scale=False,
                )
            for channel, target in zip(self.axis_channels, targets):
                if target is not None:
                    stage.wait_move(channel, timeout=timeout)

    def move_relative(self, *deltas: float | None, timeout: float = 30.0) -> None:
        stage = self._require_stage()
        with self._lock:
            new_targets: list[float | None] = []
            for idx, (channel, delta) in enumerate(zip(self.axis_channels, deltas)):
                if delta is not None:
                    current_steps = stage.get_position(channel, scale=False)
                    target_mm = (
                        current_steps + int(round(float(delta) * self.steps_per_mm))
                    ) / self.steps_per_mm
                    new_targets.append(self._validate_position(target_mm, idx))
                else:
                    new_targets.append(None)
            self.move_to(*new_targets, timeout=timeout)

    def get_position(self) -> tuple[float, ...]:
        stage = self._require_stage()
        with self._lock:
            return tuple(
                float(stage.get_position(channel, scale=False)) / self.steps_per_mm
                for channel in self.axis_channels
            )

    def stop(self, axis: int | None = None) -> None:
        stage = self._require_stage()
        channels = self.axis_channels if axis is None else (axis,)
        with self._lock:
            for channel in channels:
                if channel not in self.axis_channels:
                    raise ValueError(f"axis must be one of {self.axis_channels}")
                stage.stop(channel=channel)


class PyroCommands(Enum):
    GETTEMP = "GetTemp"
    GETINT = "GetInt"
    PING = "GetInt"


class PyroServer:
    """Thread-safe CRLF-delimited ASCII TCP client for the pyrometer data server."""

    END_MESSAGE = b"\r\n"
    MAX_RESPONSE_BYTES = 65536

    def __init__(
        self,
        address: str = "192.168.1.112",
        port: int = 9125,
        timeout: float = 10.0,
    ) -> None:
        self.address = address
        self.port = int(port)
        self.timeout = float(timeout)
        self.protocol = "Text based TCP/IP"
        self.isConnected = False
        self.lastFrame = time.monotonic()
        self._socket: socket.socket | None = None
        self._receive_buffer = bytearray()
        self._lock = threading.RLock()

    @property
    def is_connected(self) -> bool:
        return self.isConnected

    def connect(self) -> None:
        with self._lock:
            if self.isConnected and self._socket is not None:
                return
            try:
                sock = socket.create_connection(
                    (self.address, self.port), timeout=self.timeout
                )
                sock.settimeout(self.timeout)
            except OSError as exc:
                raise ConnectionError(
                    f"Could not connect to data server {self.address}:{self.port}: {exc}"
                ) from exc
            self._socket = sock
            self._receive_buffer.clear()
            self.isConnected = True
            self.lastFrame = time.monotonic()

    def disconnect(self) -> None:
        with self._lock:
            self._disconnect_unlocked()

    close = disconnect

    def _disconnect_unlocked(self) -> None:
        sock, self._socket = self._socket, None
        self.isConnected = False
        self._receive_buffer.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _require_socket(self) -> socket.socket:
        if not self.isConnected or self._socket is None:
            raise ConnectionError("Not connected to PyroServer")
        return self._socket

    def _read_line_unlocked(self, timeout: float | None = None) -> str:
        sock = self._require_socket()
        old_timeout = sock.gettimeout()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                marker = self._receive_buffer.find(self.END_MESSAGE)
                if marker >= 0:
                    raw = bytes(self._receive_buffer[:marker])
                    del self._receive_buffer[: marker + len(self.END_MESSAGE)]
                    self.lastFrame = time.monotonic()
                    return raw.decode("ascii").rstrip("\r\n")
                if len(self._receive_buffer) > self.MAX_RESPONSE_BYTES:
                    raise ValueError("Server response exceeded max size without CRLF")
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Timed out waiting for server response")
                    sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Data server closed connection")
                self._receive_buffer.extend(chunk)
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            TimeoutError,
            ConnectionError,
        ):
            self._disconnect_unlocked()
            raise
        finally:
            if self._socket is sock:
                try:
                    sock.settimeout(old_timeout)
                except OSError:
                    pass

    def send_command(self, command: str, timeout: float = -1.0) -> str:
        if not isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty string")
        with self._lock:
            sock = self._require_socket()
            try:
                sock.sendall(command.encode("ascii") + self.END_MESSAGE)
                return self._read_line_unlocked(None if timeout < 0 else timeout)
            except (
                OSError,
                UnicodeEncodeError,
                ValueError,
                TimeoutError,
                ConnectionError,
            ):
                self._disconnect_unlocked()
                raise

    @staticmethod
    def _looks_like_temperature_response(line: str) -> bool:
        fields = line.split()
        for idx in (4, 3):
            if len(fields) > idx:
                try:
                    float(fields[idx])
                    return True
                except ValueError:
                    pass
        return False

    def getTemp(self, timeout: float | None = None) -> str:
        if timeout is None:
            timeout = self.timeout
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        with self._lock:
            sock = self._require_socket()
            deadline = time.monotonic() + timeout
            try:
                sock.sendall(
                    PyroCommands.GETTEMP.value.encode("ascii") + self.END_MESSAGE
                )
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Timed out waiting for temperature response")
                    line = self._read_line_unlocked(remaining)
                    if self._looks_like_temperature_response(line):
                        return line
            except (
                OSError,
                UnicodeEncodeError,
                ValueError,
                TimeoutError,
                ConnectionError,
            ):
                self._disconnect_unlocked()
                raise

    def getInt(self) -> str:
        return self.send_command(PyroCommands.GETINT.value)

    def _clearSocket(self) -> None:
        with self._lock:
            sock = self._require_socket()
            old_timeout = sock.gettimeout()
            try:
                sock.setblocking(False)
                while True:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            self._disconnect_unlocked()
                            raise ConnectionError("Server closed connection")
                        self._receive_buffer.extend(chunk)
                    except BlockingIOError:
                        break
                self._receive_buffer.clear()
            finally:
                if self._socket is sock:
                    sock.settimeout(old_timeout)

    def __enter__(self) -> "PyroServer":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.disconnect()
