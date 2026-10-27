"""
Test of the official Kinesis C API for the BSC103 (Thorlabs.MotionControl.Benchtop.StepperMotor.dll)
through ctypes. This is the same driver stack the Kinesis app uses, so EnableChannel here
should do what the Enable button does.

What it does:
  1. Loads the DLLs from the Kinesis folder (64-bit Python needs the 64-bit Kinesis install)
  2. Opens both controllers, loads their settings, starts polling
  3. Per channel: status before -> EnableChannel -> status after (decoded, 'enabled' bit),
     and the device units per mm that Kinesis has for that channel (compare with your
     512000 / 25600 values)
  4. Asks whether the front-panel diodes lit up
  5. OPTIONAL (asks first): tiny positive test move per axis, and back
  6. Everything is also written to kinesis_dll_log.txt (paste that file back to me)

No homing is ever done here. Close Kinesis completely before running.
"""
import ctypes
import os
import sys
import time
import traceback
from ctypes import POINTER, byref, c_bool, c_char_p, c_double, c_int, c_short, c_ubyte, c_uint

# ---------------- CONFIG ----------------
KINESIS_DIR = r"C:\Thorlabs\Kinesis"
CONTROLLERS = {"nano": b"70821739", "max": b"70817296"}
CHANNELS = {"nano": (1, 2, 3), "max": (1, 2)}
EXPECTED_STEPS_PER_MM = {"nano": 512_000, "max": 25_600}
TEST_MOVE_MM = 0.05
LOG_FILE = "kinesis_dll_log.txt"
# ----------------------------------------

STATUS_BITS = [
    (0x00000001, "hw_fw_lim"), (0x00000002, "hw_bk_lim"),
    (0x00000004, "sw_fw_lim"), (0x00000008, "sw_bk_lim"),
    (0x00000010, "moving_fw"), (0x00000020, "moving_bk"),
    (0x00000040, "jogging_fw"), (0x00000080, "jogging_bk"),
    (0x00000100, "connected"), (0x00000200, "homing"), (0x00000400, "homed"),
    (0x00000800, "initializing"), (0x00001000, "tracking"), (0x00002000, "settled"),
    (0x00004000, "motion_error"), (0x00008000, "instr_error"),
    (0x80000000, "ENABLED"),
]
ENABLED_BIT = 0x80000000

S, C = c_char_p, c_ubyte
# name: (library key, restype, argtypes)
PROTOTYPES = {
    "TLI_BuildDeviceList":            ("dm",  c_short, []),
    "SBC_Open":                       ("sbc", c_short, [S]),
    "SBC_Close":                      ("sbc", None,    [S]),
    "SBC_GetNumChannels":             ("sbc", c_short, [S]),
    "SBC_LoadSettings":               ("sbc", c_bool,  [S, C]),
    "SBC_StartPolling":               ("sbc", c_bool,  [S, C, c_int]),
    "SBC_StopPolling":                ("sbc", None,    [S, C]),
    "SBC_ClearMessageQueue":          ("sbc", None,    [S, C]),
    "SBC_EnableChannel":              ("sbc", c_short, [S, C]),
    "SBC_RequestStatusBits":          ("sbc", c_short, [S, C]),
    "SBC_GetStatusBits":              ("sbc", c_uint,  [S, C]),
    "SBC_RequestPosition":            ("sbc", c_short, [S, C]),
    "SBC_GetPosition":                ("sbc", c_int,   [S, C]),
    "SBC_GetRealValueFromDeviceUnit": ("sbc", c_short, [S, C, c_int, POINTER(c_double), c_int]),
    "SBC_GetDeviceUnitFromRealValue": ("sbc", c_short, [S, C, c_double, POINTER(c_int), c_int]),
    "SBC_MoveToPosition":             ("sbc", c_short, [S, C, c_int]),
    "SBC_StopImmediate":              ("sbc", c_short, [S, C]),
    "SBC_CanHome":                    ("sbc", c_bool,  [S, C]),
    "SBC_CanMoveWithoutHomingFirst":  ("sbc", c_bool,  [S, C]),
}


class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.stdout = sys.stdout

    def write(self, s):
        self.stdout.write(s)
        self.f.write(s)

    def flush(self):
        self.stdout.flush()
        self.f.flush()

    def close(self):
        self.f.close()


def section(title):
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def decode(bits):
    names = [n for m, n in STATUS_BITS if bits & m]
    return f"0x{bits:08X} {names}"


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
    api, missing = {}, []
    for name, (lib, restype, argtypes) in PROTOTYPES.items():
        try:
            f = getattr(libs[lib], name)
            f.restype, f.argtypes = restype, argtypes
            api[name] = f
        except AttributeError:
            missing.append(name)
    print("DLLs loaded. Missing functions:", missing or "none")
    return api


def status(api, serial, ch):
    api["SBC_RequestStatusBits"](serial, ch)
    time.sleep(0.4)
    return api["SBC_GetStatusBits"](serial, ch)


def real_from_du(api, serial, ch, du):
    v = c_double()
    rc = api["SBC_GetRealValueFromDeviceUnit"](serial, ch, int(du), byref(v), 0)
    return v.value if rc == 0 else None


def du_from_real(api, serial, ch, mm):
    v = c_int()
    rc = api["SBC_GetDeviceUnitFromRealValue"](serial, ch, float(mm), byref(v), 0)
    return v.value if rc == 0 else None


def test_move(api, serial, ch, name, key):
    du = du_from_real(api, serial, ch, TEST_MOVE_MM)
    if not du:
        print(f"  could not convert {TEST_MOVE_MM} mm to device units, skipping")
        return
    api["SBC_RequestPosition"](serial, ch)
    time.sleep(0.3)
    p0 = api["SBC_GetPosition"](serial, ch)
    print(f"\n  Test move on {name}: +{du} device units (= {TEST_MOVE_MM} mm), then back. start={p0}")
    try:
        rc = api["SBC_MoveToPosition"](serial, ch, p0 + du)
        print(f"  MoveToPosition returned {rc} (0 = accepted)")
        t0, last = time.time(), p0
        while time.time() - t0 < 6.0:
            time.sleep(0.4)
            api["SBC_RequestPosition"](serial, ch)
            p = api["SBC_GetPosition"](serial, ch)
            print(f"    t={time.time() - t0:4.1f}s  delta={p - p0:>9d}  "
                  f"status={decode(api['SBC_GetStatusBits'](serial, ch))}")
            if p == last and p != p0:
                break
            last = p
        p1 = api["SBC_GetPosition"](serial, ch)
        print(f"  RESULT: moved {p1 - p0} units (expected {du}) -> "
              f"{'OK' if abs((p1 - p0) - du) <= max(2, du * 0.02) else 'MISMATCH / NO MOTION'}")
    except KeyboardInterrupt:
        print("  Interrupted -> stopping")
    finally:
        api["SBC_StopImmediate"](serial, ch)
    try:
        rc = api["SBC_MoveToPosition"](serial, ch, p0)
        print(f"  return move accepted: {rc == 0}")
        time.sleep(3.0)
        api["SBC_RequestPosition"](serial, ch)
        print(f"  final delta from start: {api['SBC_GetPosition'](serial, ch) - p0} units")
    except KeyboardInterrupt:
        api["SBC_StopImmediate"](serial, ch)


def main():
    tee = Tee(LOG_FILE)
    sys.stdout = tee
    api, opened, started = None, [], []
    try:
        section("0. ENVIRONMENT")
        print("python:", sys.version.replace("\n", " "))
        api = load_api()
        print("TLI_BuildDeviceList ->", api["TLI_BuildDeviceList"]())

        section("1. OPEN CONTROLLERS, LOAD SETTINGS, START POLLING")
        for key, serial in CONTROLLERS.items():
            rc = api["SBC_Open"](serial)
            print(f"{key} {serial.decode()}: SBC_Open -> {rc} (0 = success)")
            if rc != 0:
                continue
            opened.append((key, serial))
            print("   channels reported:", api["SBC_GetNumChannels"](serial))
            for ch in CHANNELS[key]:
                ok = api["SBC_LoadSettings"](serial, ch)
                pol = api["SBC_StartPolling"](serial, ch, 200)
                started.append((serial, ch))
                print(f"   ch{ch}: LoadSettings={ok}  StartPolling={pol}")
        if not opened:
            print("\nNo controller opened. Close Kinesis, power-cycle, retry.")
            return
        time.sleep(1.0)

        section("2. ENABLE CHANNELS AND READ BACK")
        summary = []
        for key, serial in opened:
            for ch in CHANNELS[key]:
                name = f"{key} ch{ch}"
                print(f"\n>> {name}")
                print("   CanHome:", api["SBC_CanHome"](serial, ch),
                      "| CanMoveWithoutHomingFirst:", api["SBC_CanMoveWithoutHomingFirst"](serial, ch))
                before = status(api, serial, ch)
                print("   status before:", decode(before))
                rc = api["SBC_EnableChannel"](serial, ch)
                print("   SBC_EnableChannel ->", rc, "(0 = success)")
                time.sleep(1.0)
                after = status(api, serial, ch)
                print("   status after :", decode(after))
                summary.append((name, bool(after & ENABLED_BIT)))
                one = du_from_real(api, serial, ch, 1.0)
                exp = EXPECTED_STEPS_PER_MM[key]
                print(f"   device units per mm (from Kinesis settings): {one}   "
                      f"(your value: {exp}) -> {'MATCH' if one == exp else 'DIFFERENT'}")
                api["SBC_RequestPosition"](serial, ch)
                time.sleep(0.3)
                p = api["SBC_GetPosition"](serial, ch)
                print(f"   position: {p} units = {real_from_du(api, serial, ch, p)} mm")

        print("\nEnabled bit after EnableChannel:")
        for name, en in summary:
            print(f"   {name:10s} {'ENABLED' if en else 'not enabled'}")
        ans = input("\nDid the front-panel diodes light up on the channels in use? (y/n): ").strip()
        print("diodes lit:", ans)

        section("3. OPTIONAL TINY MOVE TEST")
        print("Each test moves one axis by a few tens of microns in the + direction and back.")
        print("Ctrl+C aborts and stops the axis.")
        for key, serial in opened:
            stop_all = False
            for ch in CHANNELS[key]:
                ans = input(f"\nMove test on {key} ch{ch}? (y/n/q): ").strip().lower()
                if ans == "q":
                    stop_all = True
                    break
                if ans == "y":
                    test_move(api, serial, ch, f"{key} ch{ch}", key)
            if stop_all:
                break

        section("4. DONE")
        print(f"Log saved to {LOG_FILE}. Paste it back for analysis.")

    except KeyboardInterrupt:
        print("\nAborted by user.")
    except Exception:
        traceback.print_exc()
    finally:
        if api:
            for serial, ch in started:
                try:
                    api["SBC_StopPolling"](serial, ch)
                except Exception:
                    pass
            for _, serial in opened:
                try:
                    api["SBC_Close"](serial)
                except Exception:
                    pass
        sys.stdout = tee.stdout
        tee.close()


if __name__ == "__main__":
    main()