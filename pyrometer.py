import socket
import threading
import time
from enum import Enum
from typing import List, Tuple

import pylablib as pll
from pylablib.devices import Thorlabs


class ThorlabsError(Exception):
    pass


class DeviceNotConnected(ThorlabsError):
    pass


class DeviceNotFound(ThorlabsError):
    pass


class ConnectionError(ThorlabsError):
    pass


class PyroStage:
    def __init__(self, device_name: str):
        self.stage: Thorlabs.KinesisMotor | None = None
        self.device_name = device_name
        self._steps_per_mm = 51200  # MAGIC NUMBER BEING LITERAL STEPS PER MM FOR THE STAGE BSC103 WITH STEPPERS.

    def connect(self):
        thor_devices = Thorlabs.list_kinesis_devices()
        print("Found devices", thor_devices)
        if thor_devices:
            sel_dev = thor_devices[0][0]
        else:
            raise DeviceNotFound("FLAG_1")
        self.stage = Thorlabs.KinesisMotor(
            sel_dev,
            is_rack_system=True,
            scale=self._steps_per_mm,  # steps/mm, only if verified for this axis
        )
        if self.stage is None:
            raise DeviceNotConnected()
        if self.stage._connection_parameters["port"] != "70821739":
            print("Connected to an unintended device.")
            raise ThorlabsError(
                "Not intended device connected. Please connect the correct device."
            )

        self.stage.set_supported_channels(3)

    def _mm_to_steps(self, mm: float) -> int:
        return int(mm * self._steps_per_mm)

    def _steps_to_mm(self, steps: int) -> float:
        return steps / self._steps_per_mm

    def home(self):
        if self.stage is None:
            raise DeviceNotConnected("Can't proceed with homing, stage not connected.")
        for i in range(3):
            channel = i + 1
            if self.stage.is_homed(channel):
                print(f"Axis {channel} is homed.")
                continue
            else:
                self.stage.home(channel=channel, sync=False, force=True)
                print(
                    f"Axis {channel} homing started; status:",
                    self.stage.get_status(channel=channel),
                )
                try:
                    self.stage.wait_for_home(channel=channel, timeout=10)
                except Thorlabs.ThorlabsTimeoutError as e:
                    raise RuntimeError(f"Homing timeout for axis {channel}.") from e
                print(
                    f"Axis {channel} homing complete:",
                    self.stage.is_homed(channel=channel),
                )

    def move_to(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
    ):
        if self.stage is None:
            raise DeviceNotConnected()
        target = [x, y, z]
        for i, t in enumerate(target):
            if t is not None:
                # Poprawiony warunek logiczny (lub zamiast i)
                if t < 0 or t > 4:
                    raise ValueError(
                        "Invalid position. Please provide a value between 0 and 4."
                    )
                self.stage.move_to(self._mm_to_steps(t), i + 1, scale=False)
                self.stage.wait_move(i + 1, timeout=10)

    def move_relative(
        self,
        dx: float | None = None,
        dy: float | None = None,
        dz: float | None = None,
    ):
        if self.stage is None:
            raise DeviceNotConnected()
        target = [dx, dy, dz]
        for i, t in enumerate(target):
            if t is not None:
                current_pos = self.stage.get_position(i + 1, scale=False)
                new_pos = current_pos + self._mm_to_steps(t)
                target[i] = self._steps_to_mm(new_pos)
        self.move_to(*target)

    def get_position(self) -> tuple[float, float, float]:
        if self.stage is None:
            raise DeviceNotConnected()
        positions = []
        for i in range(3):
            positions.append(
                self._steps_to_mm(self.stage.get_position(i + 1, scale=False))
            )
        return tuple(positions)


class PyroCommands(Enum):
    GETTEMP = "GetTemp"
    GETINT = "GetInt"


class PyroServer:
    def __init__(
        self,
        address="192.168.1.112",
        port=9125,
        timeout=10.0,
    ):
        self.address = address
        self.port = port
        self.protocol = "Text based TCP/IP"
        self.endMessageSymbol = "\r\n"

        self.timeout = timeout
        self.isConnected = False
        self.lastFrame = time.monotonic()

        self._socket = None
        self._lock = threading.Lock()

    def connect(self):
        """Establish a TCP connection."""
        print(f"Establishing connection: \nAddress:{self.address}\nPort:{self.port}")
        with self._lock:
            if self.isConnected:
                return

            sock = socket.create_connection(
                (self.address, self.port),
                timeout=self.timeout,
            )
            print("Socket created.")
            sock.settimeout(self.timeout)

            self._socket = sock
            self.isConnected = True
            self.lastFrame = time.monotonic()

    def disconnect(self):
        """Close the TCP connection safely."""
        with self._lock:
            self._disconnect_unlocked()

    def _disconnect_unlocked(self):
        sock = self._socket
        self._socket = None
        self.isConnected = False

        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                sock.close()

    def read_response(self, timeout=-1.0):
        """Read one CRLF-terminated response from the server."""
        return self._read_socket(timeout)

    def send_command(self, command, timeout=-1.0):
        """Send one command and return one CRLF-terminated response."""
        return self._send_command(command, timeout)

    def _read_socket(self, timeout=-1.0):
        """Return one CRLF-terminated string."""
        with self._lock:
            if not self.isConnected or self._socket is None:
                raise ConnectionError("Not connected to PyroServer")
            timestamp = time.monotonic()
            try:
                response = bytearray()
                while not response.endswith(b"\r\n"):
                    try:
                        chunk = self._socket.recv(1024)
                    except socket.error as e:
                        if e.errno == socket.EWOULDBLOCK:
                            chunk = b""
                        else:
                            raise
                    if len(chunk) > 0:
                        timestamp = time.monotonic()
                    if chunk is None:
                        raise ConnectionError("Server closed the connection")

                    response.extend(chunk)

                    if len(response) > 65536:
                        raise ValueError("Response exceeds maximum size")
                    if timeout < 0:
                        continue
                    if time.monotonic() - timestamp > timeout:
                        return None

                self.lastFrame = time.monotonic()
                return response[:-2].decode("ascii")

            except (OSError, UnicodeDecodeError, ValueError):
                self._disconnect_unlocked()
                raise

    def _send_command(self, command, timeout=-1.0):
        """Send one command and return one CRLF-terminated response."""
        with self._lock:
            if not self.isConnected or self._socket is None:
                raise ConnectionError("Not connected to PyroServer")

            try:
                message = command + self.endMessageSymbol
                self._socket.sendall(message.encode("ascii"))
                start_timestamp = time.monotonic()
                response = bytearray()

                while not response.endswith(b"\r\n"):
                    chunk = self._socket.recv(1024)

                    if not chunk:
                        raise ConnectionError("Server closed the connection")
                    if len(chunk) > 0:
                        start_timestamp = time.monotonic()

                    response.extend(chunk)

                    if len(response) > 65536:
                        raise ValueError("Response exceeds maximum size")
                    if timeout < 0:
                        continue
                    if time.monotonic() - start_timestamp > timeout:
                        return (
                            response[:-2].decode("ascii") + "ERROR: TIMEOUTED RESPONSE"
                        )

                self.lastFrame = time.monotonic()
                return response[:-2].decode("ascii")

            except (OSError, UnicodeDecodeError, ValueError):
                self._disconnect_unlocked()
                raise

    def getTemp(self):
        """Request temperature from the device."""
        return self._send_command(PyroCommands.GETTEMP.value)

    def getInt(self):
        """Request an integer value from the device."""
        return self._send_command(PyroCommands.GETINT.value)

    def _watchdog(self, max_idle_seconds=30):
        """Disconnect if no successful communication occurred recently."""
        if time.monotonic() - self.lastFrame > max_idle_seconds:
            self.disconnect()

    def _clearSocket(self):
        print("Clearing socket buffer...")
        if self._socket is None:
            raise ConnectionError("No socket created for clearing!")
        self._socket.setblocking(False)
        msg = self.read_response(0.1)
        while msg is not None:
            if msg is not None:
                print("Cleared message:", msg)
            msg = self.read_response(0.1)
        self._socket.setblocking(True)
        print("Clearing done.")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


class Results:
    def __init__(self):
        self.data = []
        self.positions = []
        self.timestamps = []
        self.measurement_ids = []
        self.raw_data = []  # Przechowuje surowe próbki ze wskazaniem na ID pomiaru
        self.start_time = time.time()
        self._current_id = 1

    def add_result(
        self,
        value: float,
        positions: Tuple[float, float, float],
        raw_samples: List[float],
    ):
        meas_id = self._current_id
        self._current_id += 1
        timestamp = time.time() - self.start_time

        self.measurement_ids.append(meas_id)
        self.data.append(value)
        self.positions.append(positions)
        self.timestamps.append(timestamp)

        # Rejestracja surowych próbek
        for sample_idx, raw_val in enumerate(raw_samples, start=1):
            self.raw_data.append(
                {
                    "measurement_id": meas_id,
                    "sample_index": sample_idx,
                    "timestamp": timestamp,
                    "position": positions,
                    "raw_value": raw_val,
                }
            )

    def get_results(self):
        results = []
        for m_id, timestamp, value, position in zip(
            self.measurement_ids, self.timestamps, self.data, self.positions
        ):
            results.append(
                {
                    "measurement_id": m_id,
                    "timestamp": timestamp,
                    "value": value,
                    "position": position,
                }
            )
        return results

    def get_raw_results(self):
        return self.raw_data


def main() -> int:
    device = PyroStage("Kinesis Stage")
    server = PyroServer(address="192.168.1.112", port=9125, timeout=10)
    step = 0.5
    xMin = 2
    xMax = 3
    yMin = 2
    yMax = 3
    zMin = 1
    zMax = 2

    try:
        device.connect()
        if device.stage is None:
            raise DeviceNotConnected()
        stage = device.stage
        server.connect()
        server._clearSocket()

    except (DeviceNotFound, DeviceNotConnected) as e:
        print("No Thorlabs devices found or connected:", e)
        return 1

    except ConnectionError as e:
        print("Failed to connect to server:", e)
        return 1

    device.home()

    results = Results()
    for z in range(int((zMax - zMin) / step) + 1):
        for y in range(int((yMax - yMin) / step) + 1):
            for x in range(int((xMax - xMin) / step) + 1):
                target_x = xMin + x * step
                target_y = yMin + y * step
                target_z = zMin + z * step
                try:
                    device.move_to(target_x, target_y, target_z)
                    time.sleep(1)  # Odczekanie 1 sekundy po zakończeniu ruchu

                    tempMeasurements = []
                    # Pobieranie 21 próbek w ciągu ~1 sekundy
                    for _ in range(21):
                        tempStr = server.getTemp()
                        temp_meas = float(tempStr.split(" ")[4])
                        tempMeasurements.append(temp_meas)
                        time.sleep(1.0 / 21)

                    # Posortuj i odrzuć 3 najmniejsze oraz 3 największe
                    tempMeasurements_sorted = sorted(tempMeasurements)
                    filtered = tempMeasurements_sorted[3:-3]

                    temp = sum(filtered) / len(filtered)
                    results.add_result(
                        temp, (target_x, target_y, target_z), tempMeasurements
                    )
                    print(
                        f"Position: ({target_x}, {target_y}, {target_z}), Filtered Avg Temp: {temp:.2f}"
                    )
                except Exception as e:
                    print(f"Error occurred while moving or reading temperature: {e}")
                    return 2

    try:
        server.disconnect()
    except Exception as e:
        print("Error during disconnect:", e)
        return 3

    # Zapis wyników do osobnych plików CSV
    print("Saving processed results...")
    with open("Test_measurement_processed.csv", "w") as file:
        file.write(
            "Measurement_ID;Timestamp;Position X;Position Y;Position Z;Temperature\n"
        )
        for result in results.get_results():
            file.write(
                f"{result['measurement_id']};{result['timestamp']};{result['position'][0]};{result['position'][1]};{result['position'][2]};{result['value']}\n"
            )

    print("Saving raw samples...")
    with open("Test_measurement_raw.csv", "w") as file:
        file.write(
            "Measurement_ID;Sample_Index;Timestamp;Position X;Position Y;Position Z;Raw_Temperature\n"
        )
        for raw in results.get_raw_results():
            file.write(
                f"{raw['measurement_id']};{raw['sample_index']};{raw['timestamp']};{raw['position'][0]};{raw['position'][1]};{raw['position'][2]};{raw['raw_value']}\n"
            )

    print("Measurement completed successfully.")
    return 0


if __name__ == "__main__":
    main()
