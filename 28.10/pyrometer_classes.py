"""
pyrometer_classes.py - Klasa połączenia TCP/IP z serwerem pirometru.
"""
import socket


class ConnectionError(Exception):
    """Błąd połączenia z serwerem pirometru."""
    pass


class PyroServer:
    def __init__(self, address: str = "192.168.2.112", port: int = 9125, timeout: float = 5.0):
        self.address = address
        self.port = int(port)
        self.timeout = timeout
        self.sock = None
        self.isConnected = False

    def connect(self) -> None:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.address, self.port))
            self.isConnected = True
        except Exception as e:
            self.isConnected = False
            raise ConnectionError(f"Błąd połączenia z pirometrem ({self.address}:{self.port}): {e}")

    def disconnect(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.isConnected = False

    def clear_buffer(self) -> None:
        """Czyszczenie starych, zalegających danych z bufora odbiorczego socketu."""
        if not self.sock or not self.isConnected:
            return
        try:
            self.sock.setblocking(False)
            while True:
                data = self.sock.recv(4096)
                if not data:
                    break
        except (BlockingIOError, socket.error):
            pass
        finally:
            if self.sock:
                self.sock.settimeout(self.timeout)

    def send_command(self, cmd: str) -> str:
        """Wysyła polecenie tekstowe do pirometru i zwraca odpowiedź."""
        if not self.isConnected or not self.sock:
            self.connect()
        try:
            # 1. Wyczyść stare zaległości z bufora przed wysłaniem komendy
            self.clear_buffer()

            # 2. Wysyłanie właściwego polecenia
            full_cmd = (cmd.strip() + "\n").encode("utf-8")
            self.sock.sendall(full_cmd)

            # 3. Odczyt właściwej odpowiedzi
            data = self.sock.recv(1024).decode("utf-8", errors="ignore").strip()
            if not data:
                raise ConnectionError("Pusta odpowiedź z serwera pirometru")
            return data
        except Exception as e:
            self.disconnect()
            raise ConnectionError(f"Błąd komunikacji z pirometrem ({cmd.strip()}): {e}")

    def getTemp(self) -> str:
        return self.send_command("GetTemp")

    def getPyroType(self) -> str:
        """
        Odpytuje pirometr o tryb pracy (GetPyroType 1).
        Odpowiedź: GetPyroType <idx> <mode> <val1> <val2>
        np. 'GetPyroType 0 0 ...' -> dual
            'GetPyroType 0 1 ...' -> ch1
            'GetPyroType 0 2 ...' -> ch2
        """
        try:
            resp = self.send_command("GetPyroType 1")
            tokens = resp.strip().split()
            if len(tokens) >= 3 and not tokens[0].upper().startswith("ERR"):
                code = tokens[2]  # Trzeci element to kod trybu (0, 1, 2)
                modes = {"0": "dual", "1": "ch1", "2": "ch2"}
                return modes.get(code, f"mode_{code}")
            return "unknown"
        except Exception:
            return "unknown"

    def setPyroType(self, mode: str) -> str:
        """
        Ustawia tryb pracy pirometru (SetPyroType 1 <0/1/2>).
        mode: 'dual' -> 0, 'ch1' -> 1, 'ch2' -> 2
        """
        mode_map = {"dual": "0", "ch1": "1", "ch2": "2"}
        code = mode_map.get(mode.lower().strip(), mode)
        return self.send_command(f"SetPyroType 1 {code}")