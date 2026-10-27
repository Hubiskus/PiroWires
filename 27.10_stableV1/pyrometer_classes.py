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

    def getTemp(self) -> str:
        if not self.isConnected or not self.sock:
            self.connect()
        try:
            self.sock.sendall(b"GetTemp\n")
            data = self.sock.recv(1024).decode("utf-8", errors="ignore").strip()
            if not data:
                raise ConnectionError("Pusta odpowiedź z serwera pirometru")
            return data
        except Exception as e:
            self.disconnect()
            raise ConnectionError(f"Błąd komunikacji z pirometrem: {e}")