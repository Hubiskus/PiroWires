"""
Menu-driven control of two Thorlabs BSC103 controllers through the Kinesis C API (ctypes):
  - fine stage   (NanoMax-TS, controller 70821739): X, Y, Z
  - coarse stage (MAX201,     controller 70817296): X, Y (no Z)

Positions are in mm. The device-unit <-> mm conversion is read from the Kinesis settings
(it is printed at startup). Close Kinesis completely before running.
"""
import ctypes
import os
import sys
import time
from ctypes import POINTER, byref, c_bool, c_char_p, c_double, c_int, c_short, c_ubyte, c_uint

# ---------------- CONFIG ----------------
KINESIS_DIR = r"C:\Thorlabs\Kinesis"
CONTROLLERS = {"nano": b"70821739", "max": b"70817296"}

# axis -> (controller, channel). Channel mapping is still UNVERIFIED (1=X, 2=Y, 3=Z is a guess).
AXES = {
    "nano_x": ("nano", 1),
    "nano_y": ("nano", 2),
    "nano_z": ("nano", 3),
    "max_x":  ("max", 1),
    "max_y":  ("max", 2),
}

# Soft limits in mm, relative to each axis's software zero
LIMITS_MM = {
    "nano_x": (0.0, 4.0),
    "nano_y": (0.0, 4.0),
    "nano_z": (0.0, 4.0),
    "max_x":  (0.0, 50.0),
    "max_y":  (0.0, 50.0),
}

# Flip to -1 if an axis moves opposite to what you want (positions and limits follow the flip)
SIGN = {a: +1 for a in AXES}

# Device units (steps) per mm for each controller.
# None = use the value from the Kinesis settings (printed at startup).
# Put a number here (e.g. "nano": 512_000) to override it for all channels of that controller.
STEPS_PER_MM_OVERRIDE = {"nano": None, "max": None}

DISABLE_ON_EXIT = False   # True: remove motor power on quit (the stage then no longer holds position)
MOVE_TIMEOUT_S = 120
HOME_TIMEOUT_S = 180
POS_TOL_UNITS = 5         # how close to the target counts as "arrived"
# ----------------------------------------

S, C = c_char_p, c_ubyte
PROTOTYPES = {
    "TLI_BuildDeviceList":            ("dm",  c_short, []),
    "SBC_Open":                       ("sbc", c_short, [S]),
    "SBC_Close":                      ("sbc", None,    [S]),
    "SBC_LoadSettings":               ("sbc", c_bool,  [S, C]),
    "SBC_StartPolling":               ("sbc", c_bool,  [S, C, c_int]),
    "SBC_StopPolling":                ("sbc", None,    [S, C]),
    "SBC_EnableChannel":              ("sbc", c_short, [S, C]),
    "SBC_DisableChannel":             ("sbc", c_short, [S, C]),
    "SBC_RequestStatusBits":          ("sbc", c_short, [S, C]),
    "SBC_GetStatusBits":              ("sbc", c_uint,  [S, C]),
    "SBC_RequestPosition":            ("sbc", c_short, [S, C]),
    "SBC_GetPosition":                ("sbc", c_int,   [S, C]),
    "SBC_GetDeviceUnitFromRealValue": ("sbc", c_short, [S, C, c_double, POINTER(c_int), c_int]),
    "SBC_MoveToPosition":             ("sbc", c_short, [S, C, c_int]),
    "SBC_Home":                       ("sbc", c_short, [S, C]),
    "SBC_StopImmediate":              ("sbc", c_short, [S, C]),
}

BIT_MOVING = 0x00F0       # moving + jogging bits
BIT_HOMING = 0x0200
BIT_HOMED = 0x0400
BIT_ENABLED = 0x80000000


def load_api():
    if sys.maxsize <= 2 ** 32:
        raise RuntimeError("32-bit Python: the 64-bit Kinesis DLLs cannot be loaded.")
    if not os.path.isdir(KINESIS_DIR):
        raise FileNotFoundError(f"Kinesis folder not found: {KINESIS_DIR}")
    os.add_dll_directory(KINESIS_DIR)
    os.environ["PATH"] = KINESIS_DIR + os.pathsep + os.environ["PATH"]
    libs = {
        "dm":  ctypes.CDLL(os.path.join(KINESIS_DIR, "Thorlabs.MotionControl.DeviceManager.dll")),
        "sbc": ctypes.CDLL(os.path.join(KINESIS_DIR, "Thorlabs.MotionControl.Benchtop.StepperMotor.dll")),
    }
    api = {}
    for name, (lib, restype, argtypes) in PROTOTYPES.items():
        f = getattr(libs[lib], name)
        f.restype, f.argtypes = restype, argtypes
        api[name] = f
    return api


class Stages:
    def __init__(self):
        self.api = load_api()
        self.api["TLI_BuildDeviceList"]()
        self.opened = []     # controller serials that were opened
        self.polling = []    # (serial, channel) with polling started
        self.k = {}          # axis -> device units per mm (from Kinesis settings)
        self.offset = {a: 0 for a in AXES}   # raw device units that correspond to 0 mm
        try:
            for key, serial in CONTROLLERS.items():
                rc = self.api["SBC_Open"](serial)
                if rc != 0:
                    raise RuntimeError(f"Could not open controller '{key}' ({serial.decode()}), "
                                       f"error code {rc}. Is Kinesis fully closed? Try power-cycling.")
                self.opened.append(serial)
            for axis, (key, ch) in AXES.items():
                serial = CONTROLLERS[key]
                if not self.api["SBC_LoadSettings"](serial, ch):
                    print(f"Warning: could not load settings for {axis}")
                self.api["SBC_StartPolling"](serial, ch, 200)
                self.polling.append((serial, ch))
            time.sleep(1.0)
            for axis, (key, ch) in AXES.items():
                rc = self.api["SBC_EnableChannel"](CONTROLLERS[key], ch)
                if rc != 0:
                    raise RuntimeError(f"Could not enable {axis} (error code {rc})")
            time.sleep(1.0)
            for axis, (key, ch) in AXES.items():
                du = self._du_from_mm(CONTROLLERS[key], ch, 1.0)
                override = STEPS_PER_MM_OVERRIDE.get(key)
                if not (override or du):
                    raise RuntimeError(f"No unit conversion available for {axis}")
                self.k[axis] = float(override or du)
        except Exception:
            self.close()
            raise

    # ---- low-level helpers ----
    def _du_from_mm(self, serial, ch, mm):
        v = c_int()
        rc = self.api["SBC_GetDeviceUnitFromRealValue"](serial, ch, float(mm), byref(v), 0)
        return v.value if rc == 0 else None

    def _sc(self, axis):
        key, ch = AXES[axis]
        return CONTROLLERS[key], ch

    def _raw(self, axis):
        serial, ch = self._sc(axis)
        return self.api["SBC_GetPosition"](serial, ch)

    def _status(self, axis):
        serial, ch = self._sc(axis)
        return self.api["SBC_GetStatusBits"](serial, ch)

    def refresh(self, axes=None):
        """Ask the controllers for fresh position/status and give the replies time to arrive."""
        for a in (axes or AXES):
            serial, ch = self._sc(a)
            self.api["SBC_RequestPosition"](serial, ch)
            self.api["SBC_RequestStatusBits"](serial, ch)
        time.sleep(0.3)

    # ---- public API ----
    def position(self, axis):
        return SIGN[axis] * (self._raw(axis) - self.offset[axis]) / self.k[axis]

    def stop(self, axis):
        serial, ch = self._sc(axis)
        self.api["SBC_StopImmediate"](serial, ch)

    def zero(self, axis):
        self.refresh([axis])
        self.offset[axis] = self._raw(axis)

    def move_to(self, axis, mm):
        lo, hi = LIMITS_MM[axis]
        if not lo <= mm <= hi:
            raise ValueError(f"{axis}: {mm} mm is outside limits {lo}..{hi} mm")
        serial, ch = self._sc(axis)
        target = self.offset[axis] + round(SIGN[axis] * mm * self.k[axis])
        rc = self.api["SBC_MoveToPosition"](serial, ch, int(target))
        if rc != 0:
            raise RuntimeError(f"{axis}: move command rejected (error code {rc})")
        self._wait_move(axis, target)

    def move_by(self, axis, mm):
        self.refresh([axis])
        self.move_to(axis, self.position(axis) + mm)

    def _wait_move(self, axis, target):
        serial, ch = self._sc(axis)
        t0, ok_count, idle = time.time(), 0, 0
        time.sleep(0.2)
        try:
            while time.time() - t0 < MOVE_TIMEOUT_S:
                self.refresh([axis])
                pos, st = self._raw(axis), self._status(axis)
                moving = bool(st & BIT_MOVING)
                idle = 0 if moving else idle + 1
                if not moving and abs(pos - target) <= POS_TOL_UNITS:
                    ok_count += 1
                    if ok_count >= 2:
                        return
                else:
                    ok_count = 0
                if idle >= 6:
                    raise RuntimeError(f"{axis}: stopped at {self.position(axis):.4f} mm before reaching "
                                       f"the target (status 0x{st:08X}). Check limit switches/status.")
            raise TimeoutError(f"{axis}: move did not finish within {MOVE_TIMEOUT_S} s")
        except KeyboardInterrupt:
            self.stop(axis)
            raise

    def home(self, axis):
        serial, ch = self._sc(axis)
        rc = self.api["SBC_Home"](serial, ch)
        if rc != 0:
            raise RuntimeError(f"{axis}: home command rejected (error code {rc})")
        t0, seen_busy, idle = time.time(), False, 0
        time.sleep(0.5)
        try:
            while time.time() - t0 < HOME_TIMEOUT_S:
                self.refresh([axis])
                st = self._status(axis)
                if st & (BIT_HOMING | BIT_MOVING):
                    seen_busy, idle = True, 0
                else:
                    idle += 1
                if idle >= 3 and (st & BIT_HOMED) and seen_busy:
                    break
                if idle >= 8 and not seen_busy:
                    raise RuntimeError(f"{axis}: homing never started (status 0x{st:08X})")
            else:
                raise TimeoutError(f"{axis}: homing did not finish within {HOME_TIMEOUT_S} s")
        except KeyboardInterrupt:
            self.stop(axis)
            raise
        self.refresh([axis])
        self.offset[axis] = self._raw(axis)

    def close(self):
        if not self.opened:
            return
        for axis, (key, ch) in AXES.items():
            serial = CONTROLLERS[key]
            try:
                self.api["SBC_StopImmediate"](serial, ch)
                if DISABLE_ON_EXIT:
                    self.api["SBC_DisableChannel"](serial, ch)
            except Exception:
                pass
        for serial, ch in self.polling:
            try:
                self.api["SBC_StopPolling"](serial, ch)
            except Exception:
                pass
        for serial in self.opened:
            try:
                self.api["SBC_Close"](serial)
            except Exception:
                pass
        self.opened, self.polling = [], []


# ---------------- MENU ----------------
MENU = """
==== Stage control ====
 1. Home
 2. Move
 3. Show positions
 4. Set zero here
 5. Stop all
 6. Quit
"""


def choose_axis(allow_all=False):
    names = list(AXES)
    for i, a in enumerate(names, 1):
        lo, hi = LIMITS_MM[a]
        print(f"  {i}. {a}  (limits {lo}..{hi} mm)")
    if allow_all:
        print(f"  {len(names) + 1}. all")
    raw = input("Axis number (Enter to cancel): ").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        print("Not a number.")
        return None
    if 1 <= n <= len(names):
        return [names[n - 1]]
    if allow_all and n == len(names) + 1:
        return names
    print("Invalid choice.")
    return None


def show_positions(s):
    s.refresh()
    for a in AXES:
        lo, hi = LIMITS_MM[a]
        print(f"  {a:8s} {s.position(a):10.4f} mm   (limits {lo}..{hi}; {s.k[a]:.0f} units/mm)")


def do_home(s):
    axes = choose_axis(allow_all=True)
    if not axes:
        return
    print("Homing only works if the stage has a home/limit switch wired to that channel.")
    if input(f"Home {', '.join(axes)}? Make sure the path is clear (y/n): ").lower() != "y":
        return
    for a in axes:
        print(f"Homing {a}...")
        s.home(a)
        print(f"{a}: homed, position {s.position(a):.4f} mm")


def do_move(s):
    axes = choose_axis()
    if not axes:
        return
    a = axes[0]
    s.refresh([a])
    print(f"{a} is at {s.position(a):.4f} mm")
    mode = input("1. Absolute   2. Relative   (Enter to cancel): ").strip()
    if mode not in ("1", "2"):
        return
    try:
        mm = float(input("Distance/position in mm: ").replace(",", "."))
    except ValueError:
        print("Not a valid number.")
        return
    if mode == "1":
        s.move_to(a, mm)
    else:
        s.move_by(a, mm)
    s.refresh([a])
    print(f"{a}: {s.position(a):.4f} mm")


def do_zero(s):
    axes = choose_axis(allow_all=True)
    if not axes:
        return
    for a in axes:
        s.zero(a)
        print(f"{a}: current position set as 0 mm")


def main():
    try:
        s = Stages()
    except Exception as e:
        print("Startup failed:", e)
        return
    print("\nAxis mapping and scale (device units per mm, from Kinesis settings):")
    for a, (key, ch) in AXES.items():
        note = "  (OVERRIDE)" if STEPS_PER_MM_OVERRIDE.get(key) else ""
        print(f"  {a:8s} controller {key} ch{ch}   {s.k[a]:.0f} units/mm{note}")
    try:
        while True:
            print(MENU)
            choice = input("Choice: ").strip()
            try:
                if choice == "1":
                    do_home(s)
                elif choice == "2":
                    do_move(s)
                elif choice == "3":
                    show_positions(s)
                elif choice == "4":
                    do_zero(s)
                elif choice == "5":
                    for a in AXES:
                        s.stop(a)
                    print("All axes stopped.")
                elif choice == "6":
                    break
                else:
                    print("Invalid choice.")
            except KeyboardInterrupt:
                print("\nInterrupted, axis stopped.")
            except Exception as e:
                print("Error:", e)
    except KeyboardInterrupt:
        pass
    finally:
        s.close()


if __name__ == "__main__":
    main()