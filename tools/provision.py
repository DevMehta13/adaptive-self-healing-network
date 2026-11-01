"""Guided provisioning of four fresh ESP32 (esp32dev) boards as A/B/C/D (Windows first).

For each role the tool detects the board's COM port (the one that newly appears), reads its
MAC with esptool, erases the flash and uploads the role image.  When the MACs are known it
sends each board its neighbour `peer` lines over serial, then resets every board and checks
the `F3_READY` line.  No MAC is typed by hand and none is committed: they are written to
firmware/lab_boards.local.json (git-ignored).

    python tools/provision.py                  # all four roles, with the DHT11 image on A
    python tools/provision.py --only B         # replace one board, re-point its neighbours
    python tools/provision.py --skip-flash     # peers + verify only, from the saved file
    python tools/provision.py --dry-run        # print the plan, touch nothing

Needs pyserial and the PlatformIO environment from firmware/README.md.  Firmware, protocol,
serial format and dashboard API are not touched.  Run it with the backend stopped: the backend
holds C's port.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROLES = ("A", "B", "C", "D")
NODE_ID = {"A": 1, "B": 2, "C": 3, "D": 4}
ROLE_OF_ID = {v: k for k, v in NODE_ID.items()}

# The configured diamond (FOUR_BOARD_LAB_PACKAGE.md, "MAC-address setup"): role -> neighbour roles
# in the order the lab package lists them.
NEIGHBOURS = {"A": ("B", "D"), "B": ("A", "C"), "C": ("B", "D"), "D": ("A", "C")}

REPO = Path(__file__).resolve().parent.parent
FIRMWARE = REPO / "firmware"
LAB_FILE = FIRMWARE / "lab_boards.local.json"

PORT_APPEAR_TIMEOUT_S = 30.0
PORT_SETTLE_S = 1.5
READY_TIMEOUT_S = 8.0
PEER_REPLY_TIMEOUT_S = 3.0

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
READY_RE = re.compile(
    r"# F3_READY role=(?P<role>\d) mac=(?P<mac>[0-9A-Fa-f:]{17}) channel=(?P<channel>\d+)"
    r" peers_restored=(?P<peers>\d+) free_heap=(?P<heap>\d+)"
)
ESPTOOL_MAC_RE = re.compile(r"^MAC:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", re.M)


class ProvisionError(Exception):
    """A step failed in a way the operator must fix; main() prints it and exits non-zero."""


# --------------------------------------------------------------------------------------
# Pure logic (unit-tested without hardware)
# --------------------------------------------------------------------------------------

def normalize_mac(text):
    mac = text.strip().upper()
    if not MAC_RE.match(mac):
        raise ProvisionError(f"not a MAC address: {text!r}")
    return mac


def new_ports(before, after):
    """Ports present in `after` but not in `before`, sorted."""
    return sorted(set(after) - set(before))


def single_new_port(before, after):
    found = new_ports(before, after)
    if not found:
        raise ProvisionError("no new serial port appeared; check the cable (it must carry data) "
                             "and the CP210x/CH340 driver")
    if len(found) > 1:
        raise ProvisionError(f"more than one new serial port appeared ({', '.join(found)}); "
                             "unplug everything except the board being set up and retry")
    return found[0]


def wait_for_new_port(list_ports, before, timeout=PORT_APPEAR_TIMEOUT_S, settle=PORT_SETTLE_S,
                      clock=time.monotonic, sleep=time.sleep):
    """Wait for a port to appear, let a second one show up if it is going to, then require one."""
    deadline = clock() + timeout
    while clock() < deadline:
        if new_ports(before, list_ports()):
            break
        sleep(0.2)
    sleep(settle)
    return single_new_port(before, list_ports())


def parse_ready(line):
    """Parse an F3_READY line; None when the line is something else."""
    m = READY_RE.search(line)
    if not m:
        return None
    return {
        "role": ROLE_OF_ID.get(int(m["role"])),
        "mac": normalize_mac(m["mac"]),
        "channel": int(m["channel"]),
        "peers_restored": int(m["peers"]),
        "free_heap": int(m["heap"]),
    }


def parse_esptool_mac(output):
    m = ESPTOOL_MAC_RE.search(output)
    if not m:
        raise ProvisionError("esptool read_mac printed no MAC line")
    return normalize_mac(m.group(1))


def peer_lines(role, macs):
    """The `peer N MAC` lines for `role`, from the lab package table."""
    return [f"peer {NODE_ID[n]} {macs[n]}" for n in NEIGHBOURS[role]]


def env_for(role, no_sensor):
    if role == "A":
        return "f3c_a" if no_sensor else "f3c_a_dht11"
    return f"f3c_{role.lower()}"


def peer_plan(only, macs):
    """Lines to send per role.  Whole run: every board gets both of its lines.

    With --only, the replaced boards get both of their lines, and every other board gets just
    the lines that point at a replaced board (the rest are already stored in its flash).
    """
    plan = {}
    for role in ROLES:
        if only is None or role in only:
            plan[role] = peer_lines(role, macs)
        else:
            stale = [f"peer {NODE_ID[n]} {macs[n]}" for n in NEIGHBOURS[role] if n in only]
            if stale:
                plan[role] = stale
    return plan


def merge_macs(only, saved, fresh):
    """MACs for all four roles: new ones for the roles just provisioned, saved for the rest."""
    macs = {}
    for role in ROLES:
        if only is None or role in only:
            if role not in fresh:
                raise ProvisionError(f"no MAC known for role {role}")
            macs[role] = fresh[role]
        elif role in saved:
            macs[role] = saved[role]
        else:
            raise ProvisionError(f"--only needs the saved MAC of role {role}, but {LAB_FILE.name} "
                                 "has none; run a full provisioning first")
    return macs


def match_roles(observed, macs):
    """observed: port -> MAC read from F3_READY.  Returns role -> port for known MACs."""
    by_mac = {mac: role for role, mac in macs.items()}
    found = {}
    for port, mac in observed.items():
        role = by_mac.get(mac)
        if role is not None:
            found[role] = port
    return found


def check_ready(info, role):
    """Verification rule: right role and both stored peers restored."""
    if info is None:
        raise ProvisionError(f"role {role}: no F3_READY line after reset")
    if info["role"] != role:
        raise ProvisionError(f"role {role}: board reports role={info['role']}")
    if info["peers_restored"] != 2:
        raise ProvisionError(f"role {role}: peers_restored={info['peers_restored']}, expected 2")


def send_peer_line(ser, line, retries=1, timeout=PEER_REPLY_TIMEOUT_S, clock=time.monotonic):
    """Send one peer line; require '# peer_set'.  One retry, then a clear error."""
    for _ in range(retries + 1):
        ser.reset_input_buffer()
        ser.write((line + "\n").encode("ascii"))
        deadline = clock() + timeout
        while clock() < deadline:
            text = ser.readline().decode("ascii", errors="replace")
            if "# peer_set" in text:
                return
            if "# peer_failed" in text:
                break
    raise ProvisionError(f"board did not confirm '{line}' with '# peer_set' "
                         f"after {retries + 1} attempts")


def merge_ports(saved, current):
    """Saved ports for roles not seen this run, minus any port a located board now holds."""
    taken = set(current.values())
    kept = {r: p for r, p in saved.items() if r not in current and p not in taken}
    return {**kept, **current}


def lab_record(macs, ports, now=None):
    now = now or datetime.now(UTC)
    return {
        "updated": now.isoformat(timespec="seconds"),
        "boards": {r: {"mac": macs[r], "port": ports.get(r)} for r in ROLES if r in macs},
    }


def format_table(macs, ports):
    rows = [("Role", "MAC", "COM")]
    rows += [(r, macs.get(r, "-"), ports.get(r) or "-") for r in ROLES]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in rows)


# --------------------------------------------------------------------------------------
# Hardware and subprocess layer
# --------------------------------------------------------------------------------------

def list_serial_ports():
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports()]


def pio_command():
    venv = FIRMWARE / ".platformio-venv" / "Scripts" / "pio.exe"
    return [str(venv)] if venv.exists() else ["pio"]


def esptool_command():
    if (FIRMWARE / ".platformio-venv" / "Scripts" / "python.exe").exists():
        python = str(FIRMWARE / ".platformio-venv" / "Scripts" / "python.exe")
    else:
        python = sys.executable
    core = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    script = core / "packages" / "tool-esptoolpy" / "esptool.py"
    if not script.exists():
        raise ProvisionError(f"PlatformIO's esptool not found at {script}; "
                             "build any firmware env once so PlatformIO installs it")
    return [python, str(script)]


def run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-15:])
        raise ProvisionError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{tail}")
    return result.stdout + result.stderr


def retry_once(action, *args):
    """Run a flashing step; one retry, because serial transfers occasionally drop mid-way."""
    try:
        return action(*args)
    except ProvisionError as err:
        print(f"  step failed, retrying once: {str(err).splitlines()[-1]}")
        return action(*args)


def read_mac(port):
    return parse_esptool_mac(run(esptool_command() + ["--port", port, "read_mac"]))


def erase_flash(port):
    run(esptool_command() + ["--port", port, "erase_flash"])


def upload(env, port):
    run(pio_command() + ["run", "-e", env, "--target", "upload", "--upload-port", port],
        cwd=FIRMWARE)


def open_port(port):
    """Open without letting DTR/RTS reset the board (same approach as the backend)."""
    import serial
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, 115200, 0.3
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def pulse_reset(ser):
    """EN low via RTS (DTR high keeps GPIO0 high so the board runs rather than boots to flash)."""
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False


def wait_ready(ser, timeout=READY_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = parse_ready(ser.readline().decode("ascii", errors="replace"))
        if info:
            return info
    return None


def reset_and_read_ready(port):
    ser = open_port(port)
    try:
        ser.reset_input_buffer()
        pulse_reset(ser)
        return wait_ready(ser)
    finally:
        ser.close()


def identify_ports(candidates):
    """port -> MAC for every candidate that answers with an F3_READY line."""
    seen = {}
    for port in candidates:
        try:
            info = reset_and_read_ready(port)
        except Exception:  # noqa: BLE001 - a busy or non-board port just isn't a candidate
            continue
        if info:
            seen[port] = info["mac"]
    return seen


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------

def prompt(text):
    input(text)


def provision_board(role, args, used_ports):
    prompt(f"\nPlug in the board for role {role} and press Enter "
           "(unplug other unflashed boards)... ")
    before = list_serial_ports()
    port = wait_for_new_port(list_serial_ports, before)
    print(f"  role {role}: new port {port}")
    mac = read_mac(port)
    print(f"  role {role}: MAC {mac}")
    env = env_for(role, args.no_sensor)
    print(f"  role {role}: erasing flash, uploading {env} ...")
    retry_once(erase_flash, port)
    retry_once(upload, env, port)
    used_ports[role] = port
    return mac


def locate(macs, roles, ports):
    """Find each role's port by its MAC; ask the operator to plug in any that are missing."""
    for _ in range(3):
        missing = [r for r in roles if r not in ports]
        if not missing:
            return
        found = match_roles(identify_ports(list_serial_ports()), {r: macs[r] for r in missing})
        ports.update(found)
        missing = [r for r in roles if r not in ports]
        if not missing:
            return
        prompt(f"\nBoard(s) {', '.join(missing)} not found on any port. Plug them in "
               "(data cable) and press Enter... ")
    raise ProvisionError(f"could not find board(s) {', '.join(r for r in roles if r not in ports)} "
                         "by MAC")


def send_peers(plan, ports):
    for role, lines in plan.items():
        ser = open_port(ports[role])
        try:
            ser.reset_input_buffer()
            pulse_reset(ser)
            info = wait_ready(ser)
            if info is None:
                raise ProvisionError(f"role {role}: no F3_READY before sending peers")
            for line in lines:
                send_peer_line(ser, line)
                print(f"  role {role}: {line} -> peer_set")
        finally:
            ser.close()


def verify(roles, macs, ports):
    for role in roles:
        info = reset_and_read_ready(ports[role])
        check_ready(info, role)
        if info["mac"] != macs[role]:
            raise ProvisionError(f"role {role}: MAC changed ({info['mac']} vs {macs[role]})")
        print(f"  role {role}: F3_READY role={NODE_ID[role]} peers_restored="
              f"{info['peers_restored']} free_heap={info['free_heap']}")


def load_saved():
    if not LAB_FILE.exists():
        return {}, {}
    data = json.loads(LAB_FILE.read_text())
    boards = data.get("boards", {})
    return ({r: b["mac"] for r, b in boards.items()},
            {r: b.get("port") for r, b in boards.items() if b.get("port")})


def print_plan(roles, only, args):
    print("DRY RUN: nothing is executed.\n")
    macs = {r: f"<MAC_{r}>" for r in ROLES}
    for role in roles:
        if args.skip_flash:
            print(f"role {role}: (skip flash) locate by saved MAC")
            continue
        print(f"role {role}: prompt, detect new port, esptool read_mac, erase_flash, "
              f"pio run -e {env_for(role, args.no_sensor)} --target upload")
    print("\nPeer lines to send:")
    for role, lines in peer_plan(only, macs).items():
        print(f"  {role}: " + "; ".join(lines))
    print("\nVerify: reset each board, require F3_READY with the right role and peers_restored=2")
    print(f"Record: {LAB_FILE}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="comma-separated roles to provision, e.g. B or A,D")
    ap.add_argument("--no-sensor", action="store_true", help="flash f3c_a instead of f3c_a_dht11")
    ap.add_argument("--skip-flash", action="store_true",
                    help="peers + verify only, using the MACs in lab_boards.local.json")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    args = ap.parse_args(argv)

    only = None
    if args.only:
        only = [r.strip().upper() for r in args.only.split(",")]
        bad = [r for r in only if r not in ROLES]
        if bad:
            ap.error(f"unknown role(s): {', '.join(bad)}")
        only = sorted(set(only), key=ROLES.index)
    roles = only or list(ROLES)

    if args.dry_run:
        print_plan(roles, only, args)
        return 0

    try:
        saved_macs, saved_ports = load_saved()
        ports, fresh = {}, {}
        if args.skip_flash:
            known = {r: saved_macs[r] for r in roles if r in saved_macs}
            macs = merge_macs(only, saved_macs, known)
        else:
            for role in roles:
                fresh[role] = provision_board(role, args, ports)
            macs = merge_macs(only, saved_macs, fresh)
            if len(set(macs.values())) != len(macs):
                raise ProvisionError("two roles ended up with the same MAC; "
                                     "was the same board plugged in twice?")

        plan = peer_plan(only, macs)
        print("\nSending peer entries ...")
        # Boards are re-identified by MAC, never trusted by remembered port name.
        ports = {}
        locate(macs, list(plan), ports)
        send_peers(plan, ports)

        print("\nVerifying (reset, F3_READY, peers_restored=2) ...")
        verify(list(plan), macs, ports)

        all_ports = merge_ports(saved_ports, ports)
        record = lab_record(macs, all_ports)
        LAB_FILE.write_text(json.dumps(record, indent=1) + "\n")
        print("\n" + format_table(macs, all_ports))
        print(f"\nSaved {LAB_FILE}")
        return 0
    except ProvisionError as err:
        print(f"\nERROR: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
