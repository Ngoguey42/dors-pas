#!/usr/bin/env python3
r"""Right/Left control awake mode. Up/Down control charge inhibition. Ctrl-C quits.

Run with sudo: sudo python3 dors_pas.py

From a checkout of the notes branch, view the spec with:
git --no-pager show HEAD:specs/dors-pas.md

Spec commit: eafa9f083fde6909fc6ef17f46fa79ee7ab2121b
"""

import argparse
import ctypes
import ctypes.util
import errno
import os
import plistlib
import pwd
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Optional
from xml.parsers.expat import ExpatError

POLL_SECONDS = 2
NANOSECONDS_PER_SECOND = 1_000_000_000
THERMAL_STATES = ("NOMINAL", "FAIR", "SERIOUS", "CRITICAL")
SLEEP_STATES = {False: "CAN_SLEEP", True: "NO_SLEEP"}
LID_STATES = {False: "OPEN", True: "CLOSED"}
POWER_STATES = {False: "UNPLUGGED", True: "PLUGGED"}
SESSION_STATES = ("LOCKED", "UNLOCKED")
MODE_STATES = {False: "FALSE", True: "TRUE"}
LID_LOCK_STATES = {False: "DISABLED", True: "ACTIVE"}
MIN_INHIBIT_CHARGE = 10
IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"


class SystemPower(Enum):
    FROM_BAT = "FROM_BAT"
    FROM_PLUG = "FROM_PLUG"


class BatteryStatus(Enum):
    CHARGING = "CHARGING"
    DISCHARGING = "DISCHARGING"
    IDLE = "IDLE"


class Arrow(Enum):
    UP = b"A"
    DOWN = b"B"
    RIGHT = b"C"
    LEFT = b"D"


@dataclass(frozen=True)
class PowerStatus:
    plugged: bool
    source: SystemPower
    battery_status: BatteryStatus
    battery_level: int

    @property
    def can_inhibit(self) -> bool:
        return self.plugged and self.battery_level >= MIN_INHIBIT_CHARGE

# Reserve space for the system integer ranges from the first row onward.
MAX_ELAPSED = (2**63 - 1) // NANOSECONDS_PER_SECOND
MAX_PID = 2**31 - 1
COLUMN_GROUPS = (
    (
        ("time", ("00:00:00",)),
        ("elapsed", (f"{MAX_ELAPSED:_d}s",)),
    ),
    (
        ("plugged", POWER_STATES.values()),
        ("sys-power", tuple(source.value for source in SystemPower)),
        ("bat-status", tuple(status.value for status in BatteryStatus)),
        ("bat-level", ("100%",)),
        ("lid", LID_STATES.values()),
        ("session", SESSION_STATES),
        ("thermal", THERMAL_STATES),
    ),
    (
        ("dors-pas", MODE_STATES.values()),
        ("caffeinate", (f"pid:{MAX_PID}",)),
        ("disablesleep", SLEEP_STATES.values()),
        ("repair-lid-lock", LID_LOCK_STATES.values()),
    ),
    (("charging-inhibition", MODE_STATES.values()),),
)
COLUMNS = tuple(column for group in COLUMN_GROUPS for column in group)
ARROW = re.compile(rb"\x1b[\[O]([ABCD])")


def run(*args):
    try:
        return subprocess.run(args, capture_output=True, timeout=10, check=True).stdout
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"{args[0]} exited with status {exc.returncode}: {detail or 'no error output'}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{args[0]} timed out after {exc.timeout} seconds") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run {args[0]}: {exc}") from exc


_thermal_call = None


def read_thermal():
    """Read NSProcessInfo.thermalState through the Objective-C runtime."""
    global _thermal_call
    if _thermal_call is None:
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            raise RuntimeError("Could not locate the Objective-C runtime")
        objc = ctypes.cdll.LoadLibrary(objc_path)
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        send = objc.objc_msgSend
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        send.restype = ctypes.c_void_p
        info = send(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
        if not info:
            raise RuntimeError("Could not access NSProcessInfo")
        send.restype = ctypes.c_long
        selector = objc.sel_registerName(b"thermalState")
        _thermal_call = lambda: send(info, selector)
    level = _thermal_call()
    if not 0 <= level < len(THERMAL_STATES):
        raise RuntimeError(f"Unexpected macOS thermal state: {level}")
    return THERMAL_STATES[level]


def read_sleep_state():
    setting = re.search(rb"^\s*SleepDisabled[ \t]+([01])[ \t]*$", run("pmset", "-g"), re.MULTILINE)
    if setting is None:
        raise RuntimeError("pmset -g did not report a valid SleepDisabled value")
    return SLEEP_STATES[setting[1] == b"1"]


@lru_cache(maxsize=1)
def console_user_apis():
    iokit = ctypes.CDLL(IOKIT_PATH)
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ptr, uint = ctypes.c_void_p, ctypes.c_uint
    for library, name, result, arguments in (
        (iokit, "IORegistryGetRootEntry", uint, (uint,)),
        (iokit, "IOObjectRelease", ctypes.c_int, (uint,)),
        (iokit, "IORegistryEntryCreateCFProperty", ptr, (uint, ptr, ptr, uint)),
        (cf, "CFStringCreateWithCString", ptr, (ptr, ctypes.c_char_p, uint)),
        (cf, "CFPropertyListCreateData", ptr, (ptr, ptr, ctypes.c_long, ctypes.c_long, ptr)),
        (cf, "CFDataGetBytePtr", ptr, (ptr,)),
        (cf, "CFDataGetLength", ctypes.c_long, (ptr,)),
        (cf, "CFRelease", None, (ptr,)),
    ):
        function = getattr(library, name)
        function.restype = result
        function.argtypes = arguments
    return iokit, cf


def read_console_users():
    """Read the root's IOConsoleUsers property without archiving the registry."""
    iokit, cf = console_user_apis()
    root = iokit.IORegistryGetRootEntry(0)
    if not root:
        raise RuntimeError("Could not open the I/O Registry root")
    key = users = data = None
    try:
        utf8 = 0x08000100
        xml_plist = 100
        key = cf.CFStringCreateWithCString(None, b"IOConsoleUsers", utf8)
        if not key:
            raise RuntimeError("Could not create the IOConsoleUsers property key")
        users = iokit.IORegistryEntryCreateCFProperty(root, key, None, 0)
        if not users:
            raise RuntimeError("Could not read IOConsoleUsers from the I/O Registry")
        data = cf.CFPropertyListCreateData(None, users, xml_plist, 0, None)
        if not data:
            raise RuntimeError("Could not serialize IOConsoleUsers")
        buffer = cf.CFDataGetBytePtr(data)
        length = cf.CFDataGetLength(data)
        if not buffer or length <= 0:
            raise RuntimeError("IOConsoleUsers serialized to an empty property list")
        return plistlib.loads(ctypes.string_at(buffer, length))
    except (ValueError, ExpatError) as exc:
        raise RuntimeError("Could not decode the IOConsoleUsers property list") from exc
    finally:
        for ref in (data, users, key):
            if ref:
                cf.CFRelease(ref)
        iokit.IOObjectRelease(root)


def read_session():
    console_users = read_console_users()
    if not isinstance(console_users, list) or any(not isinstance(user, dict) for user in console_users):
        raise RuntimeError("IOConsoleUsers did not contain a list of user records")
    for user in console_users:
        for key in ("kCGSSessionOnConsoleKey", "CGSSessionScreenIsLocked"):
            flag = user.get(key, False)
            if type(flag) not in (bool, int) or flag not in (0, 1):
                raise RuntimeError(f"IOConsoleUsers contained an invalid {key} flag")
    users = [user for user in console_users if user.get("kCGSSessionOnConsoleKey")]
    locked = not users or any(user.get("CGSSessionScreenIsLocked") for user in users)
    return "LOCKED" if locked else "UNLOCKED"


def read_power_status() -> PowerStatus:
    registry = run("ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1", "-w", "0")
    properties = dict(re.findall(
        rb'^\s*"(ExternalConnected|CurrentCapacity|Amperage)" = ([^\r\n]+)$',
        registry, re.MULTILINE,
    ))
    external = properties.get(b"ExternalConnected")
    if external not in (b"Yes", b"No"):
        raise RuntimeError(f"AppleSmartBattery ExternalConnected must be Yes or No, got {external!r}")
    plugged = external == b"Yes"

    level = properties.get(b"CurrentCapacity", b"")
    if not re.fullmatch(rb"(100|[0-9]{1,2})", level):
        raise RuntimeError(f"AppleSmartBattery CurrentCapacity must be a percentage, got {level!r}")
    raw_current = properties.get(b"Amperage", b"")
    if not re.fullmatch(rb"-?[0-9]{1,20}", raw_current):
        raise RuntimeError(f"AppleSmartBattery Amperage must be an integer, got {raw_current!r}")
    current = int(raw_current)
    if not -(2**63) <= current < 2**64:
        raise RuntimeError(f"AppleSmartBattery Amperage is out of range: {current}")
    if current >= 2**63:
        current -= 2**64  # ioreg can print a signed current as an unsigned integer.

    source = SystemPower.FROM_BAT
    if plugged:
        telemetry = re.search(rb'^\s*"PowerTelemetryData" = (\{[^\r\n]*\})$', registry, re.MULTILINE)
        input_power = re.search(rb'"SystemPowerIn"\s*=\s*([0-9]+)(?=[,}])', telemetry[1]) if telemetry else None
        if input_power is None:
            raise RuntimeError("AppleSmartBattery PowerTelemetryData is missing a valid SystemPowerIn reading")
        if int(input_power[1]) > 0:
            source = SystemPower.FROM_PLUG

    status = BatteryStatus.IDLE
    if current < 0:
        status = BatteryStatus.DISCHARGING
    elif plugged and current > 0:
        status = BatteryStatus.CHARGING
    return PowerStatus(plugged, source, status, int(level))


def read_lid() -> bool:
    lid_info = run("ioreg", "-r", "-k", "AppleClamshellState", "-d", "1", "-w", "0")
    lid_values = set(re.findall(rb'^\s*"AppleClamshellState" = (Yes|No)[ \t]*$', lid_info, re.MULTILINE))
    if len(lid_values) != 1:
        raise RuntimeError("ioreg did not report a single valid AppleClamshellState value")
    return lid_values.pop() == b"Yes"


def lock_session():
    """Lock the console user's GUI session when running through sudo."""
    uid = os.stat("/dev/console").st_uid
    if uid == 0:
        return  # The user logged out after the session was sampled.
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError as exc:
        raise RuntimeError(f"No account found for console UID {uid}") from exc
    code = (
        "import ctypes; "
        "lib = ctypes.CDLL('/System/Library/PrivateFrameworks/"
        "login.framework/Versions/Current/login'); "
        "lib.SACLockScreenImmediate()"
    )
    run("launchctl", "asuser", str(uid), "sudo", "-u", user, sys.executable, "-c", code)


class AwakeMode:
    def __init__(self):
        self.enabled = False
        self.child = None
        self.sleep_needs_reset = False

    def caffeinate_pid(self):
        if self.child is not None and self.child.poll() is None:
            return self.child.pid
        return None

    def sync(self, sleep_state):
        """Synchronize awake mode and return the confirmed sleep setting."""
        if not self.enabled:
            if self.sleep_needs_reset:
                self.disable()
                return SLEEP_STATES[False]
            return sleep_state

        self.sleep_needs_reset = True
        if sleep_state != SLEEP_STATES[True]:
            self.set_disablesleep(True)
        if self.caffeinate_pid() is None:
            if self.child is not None:
                print(f"caffeinate exited with status {self.child.returncode}; restarting", file=sys.stderr)
            self.child = subprocess.Popen(
                ["caffeinate", "-ims", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return SLEEP_STATES[True]

    @staticmethod
    def set_disablesleep(enabled):
        run("pmset", "-a", "disablesleep", str(int(enabled)))
        actual = read_sleep_state()
        if actual != SLEEP_STATES[enabled]:
            raise RuntimeError(f"pmset did not apply disablesleep {int(enabled)} (reported {actual})")

    def disable(self):
        try:
            if self.sleep_needs_reset:
                self.set_disablesleep(False)
                self.sleep_needs_reset = False
        finally:
            if self.caffeinate_pid() is not None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=2)
            self.child = None


class SMCKeyInfo(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("attributes", ctypes.c_uint8),
    ]


class SMCMessage(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("unused_version_and_power_limits", ctypes.c_uint8 * 24),
        ("info", SMCKeyInfo),
        ("result", ctypes.c_uint8),
        ("status", ctypes.c_uint8),
        ("operation", ctypes.c_uint8),
        ("index", ctypes.c_uint32),
        ("data", ctypes.c_uint8 * 32),
    ]


class ChargeControl:
    """The AppleSMCKeysEndpoint CHTE control verified on this M5."""

    ALLOWED = bytes(4)
    INHIBITED = bytes.fromhex("01000000")

    def __init__(self) -> None:
        if (ctypes.sizeof(SMCMessage), SMCMessage.info.offset, SMCMessage.operation.offset,
                SMCMessage.data.offset) != (80, 28, 42, 48):
            raise RuntimeError("Unexpected SMC message layout for charge inhibition")

        self.iokit = ctypes.CDLL(IOKIT_PATH)
        uint, ptr, size = ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t
        for name, result, arguments in (
            ("IOServiceMatching", ptr, (ctypes.c_char_p,)),
            ("IOServiceGetMatchingService", uint, (uint, ptr)),
            ("IOServiceOpen", uint, (uint, uint, uint, ctypes.POINTER(uint))),
            ("IOServiceClose", uint, (uint,)),
            ("IOObjectRelease", uint, (uint,)),
            ("IOConnectCallStructMethod", uint, (uint, uint, ptr, size, ptr, ctypes.POINTER(size))),
        ):
            function = getattr(self.iokit, name)
            function.argtypes = arguments
            function.restype = result

        task = ctypes.c_uint.in_dll(ctypes.CDLL(None), "mach_task_self_").value
        match = self.iokit.IOServiceMatching(b"AppleSMCKeysEndpoint")
        service = self.iokit.IOServiceGetMatchingService(0, match) if match else 0
        if not service:
            raise RuntimeError("AppleSMCKeysEndpoint is unavailable for charge inhibition")
        self.connection = uint()
        try:
            error = self.iokit.IOServiceOpen(service, task, 0, ctypes.byref(self.connection))
        finally:
            self.iokit.IOObjectRelease(service)
        if error:
            raise RuntimeError(f"Could not open AppleSMCKeysEndpoint: IOKit error 0x{error:08x}")

    def close(self) -> None:
        error = self.iokit.IOServiceClose(self.connection)
        if error:
            raise RuntimeError(f"Could not close AppleSMCKeysEndpoint: IOKit error 0x{error:08x}")

    def _request(self, operation: int, byte_count: int = 0, payload: bytes = b"") -> SMCMessage:
        request = SMCMessage()
        request.key = int.from_bytes(b"CHTE", "big")
        request.operation = operation
        request.info.size = byte_count
        request.data[:len(payload)] = payload
        result = SMCMessage()
        result_size = ctypes.c_size_t(ctypes.sizeof(result))
        error = self.iokit.IOConnectCallStructMethod(
            self.connection, 2, ctypes.byref(request), ctypes.sizeof(request),
            ctypes.byref(result), ctypes.byref(result_size),
        )
        if error:
            raise RuntimeError(f"CHTE operation {operation} failed: IOKit error 0x{error:08x}")
        if result_size.value != ctypes.sizeof(result):
            raise RuntimeError(f"CHTE operation {operation} returned {result_size.value} bytes, expected 80")
        if result.result:
            raise RuntimeError(
                f"CHTE operation {operation} failed: SMC result 0x{result.result:02x}, status 0x{result.status:02x}"
            )
        return result

    def check_key(self) -> None:
        info = self._request(9).info
        kind = info.kind.to_bytes(4, "big")
        if info.size != 4 or kind != b"ui32":
            raise RuntimeError(f"CHTE must be a four-byte ui32, got size={info.size}, type={kind!r}")

    def read(self) -> bytes:
        return bytes(self._request(5, 4).data[:4])

    def write(self, value: bytes) -> None:
        self._request(6, 4, value)
        actual = self.read()
        if actual != value:
            raise RuntimeError(f"CHTE write verification failed: expected 0x{value.hex()}, got 0x{actual.hex()}")


class ChargeInhibition:
    """Own only the inhibit value set by this invocation."""

    def __init__(self) -> None:
        self.control: Optional[ChargeControl] = None

    @property
    def enabled(self) -> bool:
        return self.control is not None

    def sync(self, power: PowerStatus, arrow: Optional[Arrow]) -> None:
        if not power.can_inhibit or arrow is Arrow.DOWN:
            self.disable()
        elif self.control is not None:
            actual = self.control.read()
            if actual != ChargeControl.INHIBITED:
                raise RuntimeError(f"CHTE changed while charge inhibition was active: 0x{actual.hex()}")
        elif arrow is Arrow.UP:
            self.enable()

    def enable(self) -> None:
        control = ChargeControl()
        try:
            control.check_key()
            original = control.read()
            if original != ChargeControl.ALLOWED:
                raise RuntimeError(f"CHTE is already 0x{original.hex()}, expected 0x00000000 before enabling")
        except BaseException:
            control.close()
            raise
        # Register cleanup before issuing the first write.
        self.control = control
        control.write(ChargeControl.INHIBITED)

    def disable(self) -> None:
        control = self.control
        if control is None:
            return
        try:
            actual = control.read()
        except RuntimeError:
            actual = None
        if actual not in (None, ChargeControl.ALLOWED, ChargeControl.INHIBITED):
            raise RuntimeError(f"CHTE has an unexpected value 0x{actual.hex()} during restoration")
        if actual != ChargeControl.ALLOWED:
            control.write(ChargeControl.ALLOWED)
        self.control = None
        control.close()


class Table:
    def __init__(self, output):
        self.output = output
        self.color = output.isatty()
        self.tab_title: Optional[str] = None
        self.widths = [max(len(title), *(len(value) for value in values)) for title, values in COLUMNS]
        self.previous = None
        self.last_print = None
        self.rows = 0
        self.header = self.format_row([title for title, _ in COLUMNS])
        self.print_line(self.header)

    def format_row(self, values, changed=()):
        if len(values) != len(self.widths):
            raise RuntimeError("Table row does not match the number of columns")
        cells = []
        for index, (value, width) in enumerate(zip(values, self.widths)):
            padding = width - len(value)
            if padding < 0:
                raise RuntimeError(f"Value exceeds the width of the {COLUMNS[index][0]} column")
            style = self.color and index in changed
            text = f"\x1b[1;33m{value}\x1b[0m" if style else value
            cells.append(text + " " * padding)
        groups = []
        start = 0
        for columns in COLUMN_GROUPS:
            end = start + len(columns)
            groups.append(" | ".join(cells[start:end]))
            start = end
        return " || ".join(groups).rstrip()

    def print_line(self, line):
        print(line, file=self.output, flush=True)

    def update_tab_title(self, awake: bool, inhibited: bool) -> None:
        if not self.color:
            return
        awake_dot = "🔴" if awake else "🟢"
        inhibit_dot = "🔴" if inhibited else "🟢"
        title = f"{awake_dot}WAKE {inhibit_dot}HOLD"
        if title == self.tab_title:
            return
        # Save the icon title once, then update iTerm's tab with OSC 1.
        save = "\x1b[22;1t" if self.tab_title is None else ""
        self.tab_title = title
        self.output.write(f"{save}\x1b]1;{title}\x07")
        self.output.flush()

    def restore_tab_title(self) -> None:
        if self.tab_title is None:
            return
        try:
            self.output.write("\x1b[23;1t")
            self.output.flush()
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.ENXIO, errno.ENOTTY, errno.EPIPE):
                raise
        self.tab_title = None

    def log(self, status):
        if status == self.previous:
            return
        now = time.time_ns()
        elapsed = "-"
        changed = ()
        if self.previous is not None:
            seconds = max(0, (now - self.last_print) // NANOSECONDS_PER_SECOND)
            elapsed = f"{seconds:_d}s"
            changed = {index for index, (old, new) in enumerate(zip(self.previous, status), 2) if old != new}
        local_time = time.strftime("%H:%M:%S", time.localtime(now // NANOSECONDS_PER_SECOND))
        self.print_line(self.format_row((local_time, elapsed, *status), changed))
        self.previous = status
        self.last_print = now
        self.rows += 1
        if self.rows % 10 == 0:
            self.print_line(self.header)


def refresh(mode: AwakeMode, inhibition: ChargeInhibition, table: Table, arrow: Optional[Arrow] = None) -> None:
    power = read_power_status()
    inhibition.sync(power, arrow)
    if arrow in (Arrow.LEFT, Arrow.RIGHT):
        mode.enabled = arrow is Arrow.RIGHT
    sleep_state = mode.sync(read_sleep_state())
    lid_closed = read_lid()
    session = read_session()
    if mode.enabled and lid_closed and session == "UNLOCKED":
        lock_session()
        session = read_session()
    pid = mode.caffeinate_pid()
    table.log((
        POWER_STATES[power.plugged],
        power.source.value,
        power.battery_status.value,
        f"{power.battery_level}%",
        LID_STATES[lid_closed],
        session,
        read_thermal(),
        MODE_STATES[mode.enabled],
        f"pid:{pid}" if pid is not None else "-",
        sleep_state,
        LID_LOCK_STATES[mode.enabled],
        MODE_STATES[inhibition.enabled],
    ))
    table.update_tab_title(mode.enabled, inhibition.enabled)


def monitor(fd: int, mode: AwakeMode, inhibition: ChargeInhibition, table: Table) -> None:
    pending = b""
    refresh(mode, inhibition, table)
    next_poll = time.monotonic() + POLL_SECONDS
    while True:
        timeout = max(0, next_poll - time.monotonic())
        if select.select([fd], [], [], timeout)[0]:
            keys = os.read(fd, 64)
            if not keys:
                return
            pending += keys
            for match in ARROW.finditer(pending):
                next_poll = time.monotonic() + POLL_SECONDS
                refresh(mode, inhibition, table, Arrow(match[1]))
            tail = pending.rfind(b"\x1b")
            pending = pending[tail:] if tail >= 0 and len(pending) - tail < 3 else b""
        if time.monotonic() >= next_poll:
            next_poll = time.monotonic() + POLL_SECONDS
            refresh(mode, inhibition, table)


def restore_settings(mode: AwakeMode, inhibition: ChargeInhibition) -> None:
    errors = []
    for name, restore in (("charge inhibition", inhibition.disable), ("normal sleep", mode.disable)):
        try:
            restore()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise RuntimeError("Could not restore settings: " + " | ".join(errors))


def main():
    argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0]).parse_args()
    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 dors_pas.py")
    if not sys.stdin.isatty():
        sys.exit("An interactive terminal is required.")

    def stop(signum, frame):
        raise SystemExit(0)

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    mode = AwakeMode()
    inhibition = ChargeInhibition()
    table: Optional[Table] = None
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        tty.setcbreak(fd)
        table = Table(sys.stdout)
        monitor(fd, mode, inhibition, table)
    except KeyboardInterrupt:
        pass
    finally:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
        try:
            try:
                restore_settings(mode, inhibition)
            finally:
                try:
                    if table is not None:
                        table.restore_tab_title()
                finally:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                    except termios.error as exc:
                        if exc.args[0] not in (errno.EIO, errno.ENXIO, errno.ENOTTY):
                            raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, termios.error, subprocess.TimeoutExpired) as exc:
        sys.exit(f"dors-pas: {exc}")
