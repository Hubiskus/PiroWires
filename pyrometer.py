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

class PyroStage():
    def __init__(self, device_name:str):
        self.stage:Thorlabs.KinesisMotor|None = None 
        self.device_name = device_name
        self._steps_per_mm = 51200 #MAGIC NUMBER BEING LITERAL STEPS PER MM FOR THE STAGE BSC103 WITH STEPPERS.
        pass

    def connect(self):
        thor_devices = Thorlabs.list_kinesis_devices()
        print("Found devices",thor_devices)
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
            raise ThorlabsError("Not indended device connected. Please connect the correct device.")
        
        self.stage.set_supported_channels(3)
        

    def _mm_to_steps(self, mm:float)->int:
        return int(mm * self._steps_per_mm)
    
    def _steps_to_mm(self, steps:int)->float:
        return steps / self._steps_per_mm
    
    def home(self):
        if self.stage is None:
            raise DeviceNotConnected("Can't proceed with homing, stage not connected.")
        for i in range(3):
            channel=i+1
            if self.stage.is_homed(channel):
                print(f"Axis {channel} is homed.")
                continue
            else:
                self.stage.home(channel=channel, sync=False, force=True)
                print("Axis {channel} homing started; status:", self.stage.get_status(channel=channel))
                try:
                    self.stage.wait_for_home(channel=channel, timeout=10)
                except Thorlabs.ThorlabsTimeoutError as e:
                    raise RuntimeError(f"Homing timeout for axis {channel}.") from e
                print("Axis {channel} homing complete:", self.stage.is_homed(channel=channel))

    def move_to(self, x:float|None = None, y:float|None = None, z:float|None = None):
        if self.stage is None:
            raise DeviceNotConnected()
        target = [x,y,z]
        for i,t in enumerate(target):
            if t is not None:
                if t<0 and t>4:
                    raise ValueError("Invalid position. Please provide a value between 0 and 4.")
                self.stage.move_to(self._mm_to_steps(t),i+1,scale=False)
                self.stage.wait_move(i+1,timeout=10)

    def move_relative(self, dx:float|None = None, dy:float|None = None, dz:float|None = None):
        if self.stage is None:
            raise DeviceNotConnected()
        target = [dx,dy,dz]
        for i,t in enumerate(target):
            if t is not None:
                current_pos = self.stage.get_position(i+1,scale=False)
                new_pos = current_pos + self._mm_to_steps(t)
                target[i] = self._steps_to_mm(new_pos)
        self.move_to(*target)

    def get_position(self)->tuple[float,float,float]:
        if self.stage is None:
            raise DeviceNotConnected()
        positions = []
        for i in range(3):
            positions.append(self._steps_to_mm(self.stage.get_position(i+1,scale=False)))
        return tuple(positions)
    
    # def __del__(self):
    #     if self.stage:
    #         self.stage.close()



import time as tm
from enum import Enum

import socket
import threading
import time
from enum import Enum


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
                #source_address=("192.168.1.110", 0)
            )
            print("Socket created.")
            sock.settimeout(self.timeout)

            self._socket = sock
            self.isConnected = True
            self.lastFrame = time.monotonic()
            #self._read_socket()


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
    def read_response(self,timeout = -1.0):
        """Read one CRLF-terminated response from the server."""
        return self._read_socket(timeout)
    def send_command(self, command,timeout =-1.0):
        """Send one command and return one CRLF-terminated response."""
        return self._send_command(command, timeout)
    def _read_socket(self,timeout=-1.0):
        """Return one CRLF-terminated string."""
        with self._lock:
            if not self.isConnected or self._socket is None:
                raise ConnectionError("Not connected to PyroServer")
            timestamp = time.monotonic()
            try:
                response = bytearray()
                while not response.endswith(b"\r\n"):
                    try:
                        chunk = self._socket.recv(1024,)
                    except socket.error as e:
                        if e.errno == socket.EWOULDBLOCK:
                            chunk = b""
                        else:
                            raise
                    if len(chunk)>0:
                        timestamp = time.monotonic()
                    if chunk is None:
                        raise ConnectionError(
                            "Server closed the connection"
                        )

                    response.extend(chunk)

                    if len(response) > 65536:
                        raise ValueError("Response exceeds maximum size")
                    if timeout < 0:
                        continue
                    if (time.monotonic() - timestamp >timeout):
                        return None

                self.lastFrame = time.monotonic()
                return response[:-2].decode("ascii")

            except (OSError, UnicodeDecodeError, ValueError):
                self._disconnect_unlocked()
                raise

    def _send_command(self, command,timeout=-1.0):
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
                        raise ConnectionError(
                            "Server closed the connection"
                        )
                    if len(chunk)>0:
                        start_timestamp = time.monotonic()

                    response.extend(chunk)

                    # Avoid accumulating an unexpectedly large response.
                    if len(response) > 65536:
                        raise ValueError("Response exceeds maximum size")
                    if timeout < 0:
                        continue
                    if (time.monotonic() - start_timestamp >timeout):
                        return response[:-2].decode("ascii") + "ERROR: TIMEOUTED RESPONSE"

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

from typing import List, Tuple
class Results:
    def __init__(self):
        self.data = []
        self.positions = []
        self.timestamps = []
        self.start_time = time.time()

    def add_result(self, value:float,positions:Tuple[float,float,float]):
        self.data.append(value)
        self.positions.append(positions)
        self.timestamps.append(time.time() - self.start_time)

    def get_results(self):
        results = []
        for timestamp, value, position in zip(self.timestamps, self.data, self.positions):
            results.append({"timestamp": timestamp, "value": value, "position": position})
        return results

def main() ->int:
    device = PyroStage("Kinesis Stage")
    server = PyroServer(address="192.168.1.112",port=9125,timeout = 10)
    step =  0.5
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

    except DeviceNotFound|DeviceNotConnected as e:
        print("No Thorlabs devices found.")
        return 1
    
    except ConnectionError as e:
        print("Failed to connect to server:", e)
        return 1
    
    device.home()

    results = Results()
    for z in range(int((zMax-zMin)/step)+1):
        for y in range(int((yMax-yMin)/step)+1):
            for x in range(int((xMax-xMin)/step)+1):
                target_x = xMin + x * step
                target_y = yMin + y * step
                target_z = zMin + z * step
                try:
                    #print("Moving to position:", (target_x, target_y, target_z))
                    device.move_to(target_x, target_y, target_z)
                    time.sleep(2)  # Wait for the stage to stabilize
                    tempMeasurements = []
                    for _ in range(14):
                        tempStr = server.getTemp()
                        temp_meas = float(tempStr.split(" ")[4])
                        tempMeasurements.append(temp_meas)
                        time.sleep(0.1)  
                    tempMeasurements.sort()
                    filtered = tempMeasurements[2:-2]
                    print(len(filtered)," ",len(tempMeasurements))
                    temp = sum(filtered) / len(filtered)
                    results.add_result(temp, (target_x, target_y, target_z))
                    print(f"Position: ({target_x}, {target_y}, {target_z}), Temperature: {temp}")
                except Exception as e:
                    print(f"Error occurred while moving or reading temperature: {e}")
                    return 2
    
    try:
        server.disconnect()
    except Exception as e:
        print("Error during disconnect:", e)
        return 3
    
    print("Results:")
    for result in results.get_results():
        print(f"Position: ({result['position'][0]}, {result['position'][1]}, {result['position'][2]}), Temperature: {result['value']}")
    with open("Test measurement.csv","w") as file:
        file.write("Timestamp;Position X; Position Y;Position Z;Temperature\n")
        for result in results.get_results():
            file.write(f"{result['timestamp']};{result['position'][0]};{result['position'][1]};{result['position'][2]};{result['value']}\n")
    return 0

if __name__ == "__main__":
    main()
    # device = PyroStage("Kinesis Stage")
    # device.connect()
    # device.home()
    # device.move_to(2,2,2)
    # 90821744
