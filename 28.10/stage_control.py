"""
stage_control.py - logic layer for two Thorlabs BSC103 controllers (Kinesis C API via ctypes):
  - fine stage   (NanoMax-TS, controller 70821739): X, Y, Z
  - coarse stage (MAX201,     controller 70817296): X, Y (no Z)

No UI code in here. All positions are in mm, relative to each axis's software zero.
Every DLL call is serialised with a lock, so the Stages object can be used from several
threads (e.g. a GUI thread plus one worker thread per moving axis).
Close Kinesis completely before using this module.
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_bool, c_char_p, c_double, c_int, c_short, c_ubyte, c_uint
from dataclasses import dataclass

# ---------------- CONFIG ----------------
KINESIS_DIR = r"c:\Thorlabs\Kinesis"
CONTROLLERS = {"nano": b"70821739", "max": b"70817296"}
CONTROLLER_LABELS = {"nano": "Fine stage (NanoMax-TS)", "max": "Coarse stage (MAX201)"}

# axis -> (controller, channel). Channel mapping: verify with small moves if you change cabling.
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
# None = use the value from the Kinesis settings. A number overrides it for all channels
# of that controller, e.g. {"nano": 512_000, "max": None}.
STEPS_PER_MM_OVERRIDE = {"nano": None, "max": None}

DISABLE_ON_EXIT = False   # True: remove motor power on close (stage then no longer holds position)
MOVE_TIMEOUT_S = 120
HOME_TIMEOUT_S = 180
POS_TOL_UNITS = 5         # how close to the target counts as "arrived"
# ----------------------------------------

BIT_MOVING = 0x00F0       # moving + jogging bits
BIT_HOMING = 0x0200
BIT_HOMED = 0x0400
BIT_ENABLED = 0x80000000

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


class StageError(Exception):
    """Any stage/controller problem (message is meant to be shown to the user)."""


class MoveAborted(StageError):
    """A move or homing run was stopped by the user."""


@dataclass
class AxisState:
    position_mm: float
    moving: bool
    homing: bool
    homed: bool
    enabled: bool


def _load_api():
    if sys.maxsize <= 2 ** 32:
        raise StageError("32-bit Python: the 64-bit Kinesis DLLs cannot be loaded.")
    if not os.path.isdir(KINESIS_DIR):
        raise StageError(f"Kinesis folder not found: {KINESIS_DIR}")
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
    """Connects to both controllers, enables the channels and exposes mm-based moves."""

    def __init__(self):
        self.api = _load_api()
        self._lock = threading.RLock()
        self._closed = False
        self._abort = {a: threading.Event() for a in AXES}
        self.opened = []     # controller serials that were opened
        self.polling = []    # (serial, channel) with polling started
        self.k = {}          # axis -> device units per mm
        self.k_overridden = {}   # axis -> bool
        self.offset = {a: 0 for a in AXES}   # raw device units that correspond to 0 mm
        try:
            self._call("TLI_BuildDeviceList")
            for key, serial in CONTROLLERS.items():
                rc = self._call("SBC_Open", serial)
                if rc != 0:
                    raise StageError(f"Could not open controller '{key}' ({serial.decode()}), error "
                                     f"code {rc}. Is Kinesis fully closed? Try power-cycling.")
                self.opened.append(serial)
            for axis in AXES:
                serial, ch = self._sc(axis)
                self._call("SBC_LoadSettings", serial, ch)
                self._call("SBC_StartPolling", serial, ch, 200)
                self.polling.append((serial, ch))
            time.sleep(1.0)
            for axis in AXES:
                serial, ch = self._sc(axis)
                rc = self._call("SBC_EnableChannel", serial, ch)
                if rc != 0:
                    raise StageError(f"Could not enable {axis} (error code {rc})")
            time.sleep(1.0)
            for axis, (key, _) in AXES.items():
                serial, ch = self._sc(axis)
                du = self._du_from_mm(serial, ch, 1.0)
                override = STEPS_PER_MM_OVERRIDE.get(key)
                if not (override or du):
                    raise StageError(f"No unit conversion available for {axis}")
                self.k[axis] = float(override or du)
                self.k_overridden[axis] = bool(override)
        except Exception:
            self.close()
            raise

    # ---- low-level helpers ----
    def _call(self, name, *args):
        with self._lock:
            if self._closed:
                raise StageError("Controllers are closed.")
            return self.api[name](*args)

    def _du_from_mm(self, serial, ch, mm):
        v = c_int()
        rc = self._call("SBC_GetDeviceUnitFromRealValue", serial, ch, float(mm), byref(v), 0)
        return v.value if rc == 0 else None

    @staticmethod
    def _sc(axis):
        key, ch = AXES[axis]
        return CONTROLLERS[key], ch

    def _raw(self, axis):
        serial, ch = self._sc(axis)
        return self._call("SBC_GetPosition", serial, ch)

    def _status(self, axis):
        serial, ch = self._sc(axis)
        return self._call("SBC_GetStatusBits", serial, ch)

    # ---- state ----
    def request_updates(self, axes=None):
        """Non-blocking: ask the controllers for fresh position/status (replies arrive shortly)."""
        for a in (axes or AXES):
            serial, ch = self._sc(a)
            self._call("SBC_RequestPosition", serial, ch)
            self._call("SBC_RequestStatusBits", serial, ch)

    def refresh(self, axes=None):
        """Blocking (0.3 s): request fresh values and wait for the replies."""
        self.request_updates(axes)
        time.sleep(0.3)

    def position(self, axis):
        """Last known position in mm (non-blocking)."""
        return SIGN[axis] * (self._raw(axis) - self.offset[axis]) / self.k[axis]

    def snapshot(self):
        """Last known state of every axis (non-blocking): {axis: AxisState}."""
        out = {}
        for axis in AXES:
            st = self._status(axis)
            out[axis] = AxisState(
                position_mm=self.position(axis),
                moving=bool(st & BIT_MOVING),
                homing=bool(st & BIT_HOMING),
                homed=bool(st & BIT_HOMED),
                enabled=bool(st & BIT_ENABLED),
            )
        return out

    # ---- commands ----
    def zero(self, axis):
        """Define the current position as 0 mm (software only)."""
        self.offset[axis] = self._raw(axis)

    def stop(self, axis):
        self._abort[axis].set()
        serial, ch = self._sc(axis)
        self._call("SBC_StopImmediate", serial, ch)

    def stop_all(self):
        for axis in AXES:
            try:
                self.stop(axis)
            except StageError:
                pass

    def move_to(self, axis, mm):
        """Absolute move (mm, relative to the software zero). Blocks until arrival."""
        lo, hi = LIMITS_MM[axis]
        if not lo <= mm <= hi:
            raise ValueError(f"{axis}: {mm:g} mm is outside limits {lo:g}..{hi:g} mm")
        self._abort[axis].clear()
        serial, ch = self._sc(axis)
        target = self.offset[axis] + round(SIGN[axis] * mm * self.k[axis])
        rc = self._call("SBC_MoveToPosition", serial, ch, int(target))
        if rc != 0:
            raise StageError(f"{axis}: move command rejected (error code {rc})")
        self._wait_move(axis, target)

    def move_by(self, axis, mm):
        """Relative move (mm). Blocks until arrival."""
        self.refresh([axis])
        self.move_to(axis, self.position(axis) + mm)

    def _wait_move(self, axis, target):
        t0, ok_count, idle = time.time(), 0, 0
        time.sleep(0.2)
        while time.time() - t0 < MOVE_TIMEOUT_S:
            if self._abort[axis].is_set():
                raise MoveAborted(f"{axis}: stopped")
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
                raise StageError(f"{axis}: stopped at {self.position(axis):.4f} mm before reaching "
                                 f"the target (status 0x{st:08X}). Check limit switches.")
        raise StageError(f"{axis}: move did not finish within {MOVE_TIMEOUT_S} s")

    def home(self, axis):
        """Run the homing routine (needs a home/limit switch on that channel). Blocks until done."""
        self._abort[axis].clear()
        serial, ch = self._sc(axis)
        rc = self._call("SBC_Home", serial, ch)
        if rc != 0:
            raise StageError(f"{axis}: home command rejected (error code {rc})")
        t0, seen_busy, idle = time.time(), False, 0
        time.sleep(0.5)
        while True:
            if time.time() - t0 > HOME_TIMEOUT_S:
                raise StageError(f"{axis}: homing did not finish within {HOME_TIMEOUT_S} s")
            if self._abort[axis].is_set():
                raise MoveAborted(f"{axis}: stopped")
            self.refresh([axis])
            st = self._status(axis)
            if st & (BIT_HOMING | BIT_MOVING):
                seen_busy, idle = True, 0
            else:
                idle += 1
            if idle >= 3 and (st & BIT_HOMED) and seen_busy:
                break
            if idle >= 8 and not seen_busy:
                raise StageError(f"{axis}: homing never started (status 0x{st:08X})")
        self.refresh([axis])
        self.offset[axis] = self._raw(axis)

    # ---- shutdown ----
    def close(self):
        """Stop all axes, stop polling and close the controllers. Safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            for event in self._abort.values():
                event.set()
            for axis in AXES:
                serial, ch = self._sc(axis)
                try:
                    if serial in self.opened:
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
            self._closed = True