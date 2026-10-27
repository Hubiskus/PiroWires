"""
stage_gui.py - tkinter front-end for stage_control.py (keep both files in the same folder).

Run:  python stage_gui.py
Close Kinesis completely before starting. Moves run in worker threads, so the window stays
responsive and several axes can move at the same time. STOP buttons work during a move.
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import stage_control as sc

POLL_MS = 300                                  # how often positions/states are refreshed
DEFAULT_STEP_MM = {"nano": "0.01", "max": "0.1"}


class App:
    def __init__(self, root):
        self.root = root
        root.title("Stage control")
        self.stages = None
        self.busy = {a: False for a in sc.AXES}
        self.threads = []
        self.events = queue.Queue()          # worker threads -> GUI thread
        self.rows = {}                       # axis -> dict of vars/widgets
        self.scale_vars = {}                 # controller key -> StringVar
        self.status_var = tk.StringVar()
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_connect()
        self.root.after(POLL_MS, self._tick)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.grid(sticky="nsew")

        for key in sc.CONTROLLERS:
            frame = ttk.LabelFrame(main, text=sc.CONTROLLER_LABELS.get(key, key), padding=8)
            frame.grid(sticky="ew", pady=(0, 8))

            self.scale_vars[key] = tk.StringVar(value="not connected")
            ttk.Label(frame, textvariable=self.scale_vars[key], foreground="gray").grid(
                row=0, column=0, columnspan=11, sticky="w", pady=(0, 4))

            heads = ["Axis", "Position", "State", "Target (mm)", "", "", "Step (mm)", "", "", "", ""]
            for c, h in enumerate(heads):
                ttk.Label(frame, text=h, font=("TkDefaultFont", 9, "bold")).grid(row=1, column=c, padx=3)

            r = 2
            for axis, (k, _) in sc.AXES.items():
                if k == key:
                    self._build_row(frame, axis, key, r)
                    r += 1

        bottom = ttk.Frame(main)
        bottom.grid(sticky="ew")
        bottom.columnconfigure(1, weight=1)
        self.stop_all_btn = tk.Button(bottom, text="STOP ALL", bg="#c62828", fg="white",
                                      activebackground="#8e0000", activeforeground="white",
                                      font=("TkDefaultFont", 11, "bold"), width=12,
                                      command=self.stop_all, state="disabled")
        self.stop_all_btn.grid(row=0, column=0, padx=(0, 10))
        self.status_label = ttk.Label(bottom, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=0, column=1, sticky="ew")
        self.connect_btn = ttk.Button(bottom, text="Reconnect", command=self.start_connect, state="disabled")
        self.connect_btn.grid(row=0, column=2, padx=(10, 0))

    def _build_row(self, frame, axis, key, r):
        lo, hi = sc.LIMITS_MM[axis]
        row = {
            "pos": tk.StringVar(value="--"),
            "state": tk.StringVar(value="--"),
            "target": tk.StringVar(value=f"{lo:g}"),
            "step": tk.StringVar(value=DEFAULT_STEP_MM.get(key, "0.1")),
        }
        ttk.Label(frame, text=f"{axis}\n({lo:g}..{hi:g} mm)").grid(row=r, column=0, padx=3, sticky="w")
        ttk.Label(frame, textvariable=row["pos"], width=13, font=("Consolas", 11)).grid(row=r, column=1, padx=3)
        ttk.Label(frame, textvariable=row["state"], width=18).grid(row=r, column=2, padx=3)

        target = ttk.Entry(frame, textvariable=row["target"], width=9)
        target.grid(row=r, column=3, padx=3)
        target.bind("<Return>", lambda e, a=axis: self.go(a))
        go = ttk.Button(frame, text="Go", width=5, command=lambda a=axis: self.go(a))
        go.grid(row=r, column=4, padx=3)

        minus = ttk.Button(frame, text="- step", width=7, command=lambda a=axis: self.jog(a, -1))
        minus.grid(row=r, column=5, padx=(10, 1))
        ttk.Entry(frame, textvariable=row["step"], width=7).grid(row=r, column=6, padx=1)
        plus = ttk.Button(frame, text="+ step", width=7, command=lambda a=axis: self.jog(a, +1))
        plus.grid(row=r, column=7, padx=(1, 10))

        zero = ttk.Button(frame, text="Set 0", width=6, command=lambda a=axis: self.zero(a))
        zero.grid(row=r, column=8, padx=3)
        home = ttk.Button(frame, text="Home", width=6, command=lambda a=axis: self.home(a))
        home.grid(row=r, column=9, padx=3)
        stop = ttk.Button(frame, text="Stop", width=6, command=lambda a=axis: self.stop(a))
        stop.grid(row=r, column=10, padx=3)

        row["action_buttons"] = [go, minus, plus, zero, home]
        row["stop_button"] = stop
        self.rows[axis] = row
        self._update_buttons(axis)

    def _update_buttons(self, axis):
        connected = self.stages is not None
        row = self.rows[axis]
        action = "normal" if connected and not self.busy[axis] else "disabled"
        for b in row["action_buttons"]:
            b.config(state=action)
        row["stop_button"].config(state="normal" if connected else "disabled")

    def _update_all_buttons(self):
        for a in sc.AXES:
            self._update_buttons(a)
        self.stop_all_btn.config(state="normal" if self.stages else "disabled")
        self.connect_btn.config(state="disabled" if self.stages else "normal")

    def set_status(self, msg, error=False):
        self.status_var.set(msg)
        self.status_label.config(foreground="#b00020" if error else "black")

    # ---------------------------------------------------------- connection
    def start_connect(self):
        self.connect_btn.config(state="disabled")
        self.set_status("Connecting to controllers (takes a few seconds)...")

        def worker():
            try:
                self.events.put(("connected", sc.Stages()))
            except Exception as e:
                self.events.put(("connect_failed", e))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ commands
    @staticmethod
    def _parse(var, what):
        try:
            return float(var.get().replace(",", "."))
        except ValueError:
            raise ValueError(f"{what}: '{var.get()}' is not a number")

    def run(self, axis, label, fn):
        """Run fn() in a worker thread; GUI is updated through the events queue."""
        if self.busy[axis] or self.stages is None:
            return
        self.busy[axis] = True
        self._update_buttons(axis)
        self.set_status(f"{axis}: {label}...")

        def worker():
            try:
                fn()
                self.events.put(("done", axis, f"{axis}: {label} - done", None))
            except sc.MoveAborted:
                self.events.put(("done", axis, f"{axis}: stopped", None))
            except Exception as e:
                self.events.put(("done", axis, None, e))

        t = threading.Thread(target=worker, daemon=True)
        self.threads = [x for x in self.threads if x.is_alive()] + [t]
        t.start()

    def go(self, axis):
        try:
            mm = self._parse(self.rows[axis]["target"], "Target")
        except ValueError as e:
            return self.set_status(str(e), error=True)
        self.run(axis, f"moving to {mm:g} mm", lambda: self.stages.move_to(axis, mm))

    def jog(self, axis, direction):
        try:
            step = self._parse(self.rows[axis]["step"], "Step")
        except ValueError as e:
            return self.set_status(str(e), error=True)
        d = direction * step
        self.run(axis, f"moving by {d:+g} mm", lambda: self.stages.move_by(axis, d))

    def home(self, axis):
        if not messagebox.askyesno(
                "Home axis",
                f"Home {axis}?\n\nOnly works if the stage has a home/limit switch wired to this "
                f"channel. Make sure the path is clear."):
            return
        self.run(axis, "homing", lambda: self.stages.home(axis))

    def zero(self, axis):
        if self.stages and not self.busy[axis]:
            self.stages.zero(axis)
            self.set_status(f"{axis}: current position set as 0 mm")

    def stop(self, axis):
        if self.stages:
            try:
                self.stages.stop(axis)
                self.set_status(f"{axis}: stop sent")
            except Exception as e:
                self.set_status(str(e), error=True)

    def stop_all(self):
        if self.stages:
            self.stages.stop_all()
            self.set_status("All axes stopped")

    # ----------------------------------------------------------- main loop
    def _handle(self, ev):
        kind = ev[0]
        if kind == "connected":
            self.stages = ev[1]
            for key in sc.CONTROLLERS:
                axis = next(a for a, (k, _) in sc.AXES.items() if k == key)
                note = " (OVERRIDE)" if self.stages.k_overridden[axis] else " (from Kinesis settings)"
                self.scale_vars[key].set(
                    f"Controller {sc.CONTROLLERS[key].decode()}   |   {self.stages.k[axis]:.0f} steps/mm{note}")
            self._update_all_buttons()
            self.set_status("Connected. Channels enabled.")
        elif kind == "connect_failed":
            self._update_all_buttons()
            self.set_status(f"Connection failed: {ev[1]}", error=True)
            messagebox.showerror("Connection failed", str(ev[1]))
        elif kind == "done":
            _, axis, msg, err = ev
            self.busy[axis] = False
            self._update_buttons(axis)
            if err is not None:
                self.set_status(str(err), error=True)
            else:
                self.set_status(msg)

    def _tick(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.stages is not None:
            try:
                self.stages.request_updates()
                for axis, st in self.stages.snapshot().items():
                    row = self.rows[axis]
                    row["pos"].set(f"{st.position_mm:9.4f} mm")
                    parts = ["homing" if st.homing else "moving" if st.moving else "idle"]
                    if st.homed:
                        parts.append("homed")
                    if not st.enabled:
                        parts.append("DISABLED")
                    row["state"].set(" | ".join(parts))
            except sc.StageError:
                pass
            except Exception as e:
                self.set_status(f"Update error: {e}", error=True)
        self.root.after(POLL_MS, self._tick)

    def on_close(self):
        if self.stages is not None:
            self.stages.stop_all()
            for t in self.threads:
                t.join(timeout=1.5)
            self.stages.close()
            self.stages = None
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()