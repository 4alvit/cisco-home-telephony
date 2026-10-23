"""Exclusive, read-only managed TFTP with exact local readiness.

The supervisor owns the flock, not tftpd (which may close inherited fds).
Linux subreaping keeps ownership until the listener AND transfer children exit.
"""
import ctypes
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time

DAEMON = "/usr/sbin/in.tftpd"
PROC = Path("/proc")


class Refused(Exception):
    pass


def checked_arguments(arguments):
    flags = {"--foreground", "--ipv4", "--secure", "--verbose"}
    values = {"--user", "--address", "--port-range", "--blocksize"}
    seen, roots = {}, []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option in seen:
            raise Refused("Repeated managed TFTP option")
        if option in flags:
            seen[option] = True
        elif option in values and index + 1 < len(arguments):
            index += 1
            seen[option] = arguments[index]
        elif option.startswith("-"):
            raise Refused("Unsupported managed TFTP option")
        else:
            roots.append(option)
        index += 1
    if not (seen.get("--foreground") and seen.get("--secure")
            and seen.get("--user") == "tftp" and len(roots) == 1
            and os.path.isabs(roots[0]) and os.path.normpath(roots[0]) == roots[0]):
        raise Refused("Managed TFTP requires foreground, secure root and tftp user")
    if seen.get("--port-range") != "20000:20031" or seen.get("--blocksize") != "1468":
        raise Refused("Managed TFTP requires the fixed transfer range and block size")
    host, separator, port = seen.get("--address", "").rpartition(":")
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        raise Refused("Managed TFTP requires an explicit IPv4 address") from None
    if (not separator or port != "69" or str(address) != host
            or address.is_unspecified or address.is_multicast
            or host == "255.255.255.255"):
        raise Refused("Managed TFTP requires an explicit IPv4 address")
    return (arguments if "--ipv4" in seen else ["--ipv4", *arguments]), host, roots[0]


def check_root(root):
    # Configuration/firmware files are flat. Only the existing, reviewed
    # wallpaper layout may add directories; the renderer owns file allowlists.
    allowed_directories = {"Desktops", "Desktops/320x212x16"}
    wallpaper_files = {"List.xml", "home.png", "home-thumb.png"}

    def visit(descriptor, relative=""):
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o555:
            raise Refused("Managed TFTP directories must have mode 0555")
        for name in os.listdir(descriptor):
            path = f"{relative}/{name}" if relative else name
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and path in allowed_directories:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                try:
                    visit(child, path)
                finally:
                    os.close(child)
            elif (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o444
                    or (relative and (relative != "Desktops/320x212x16"
                                      or name not in wallpaper_files))):
                raise Refused("Managed TFTP serves only approved read-only regular files")

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor)
    finally:
        os.close(descriptor)


def open_lock(path, *, create=False):
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise Refused("Unsafe TFTP ownership path")
    directory, name = os.path.split(path)
    if not name:
        raise Refused("Unsafe TFTP ownership path")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise Refused("Unsafe TFTP ownership directory")
        if create:
            os.fchmod(directory_fd, 0o700)
        elif stat.S_IMODE(info.st_mode) != 0o700:
            raise Refused("Unsafe TFTP ownership directory")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        descriptor = os.open(name, flags | (os.O_CREAT if create else 0),
                             0o600, dir_fd=directory_fd)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise Refused("Unsafe TFTP ownership file")
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("TFTP ownership file changed")
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def acquire(path):
    descriptor = open_lock(path, create=True)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("TFTP owner already active") from None
        os.ftruncate(descriptor, 0)  # stale metadata is never readiness
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def enable_subreaper():
    if not sys.platform.startswith("linux"):
        raise Refused("Managed TFTP requires Linux child subreaping")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Could not enable TFTP child subreaping")


def read_bounded(path, maximum=8192):
    with open(path, "rb") as source:
        result = source.read(maximum + 1)
    if len(result) > maximum:
        raise Refused("Oversized TFTP readiness data")
    return result.decode("ascii")


def process_identity(pid):
    fields = read_bounded(PROC / str(pid) / "stat").rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X", "x"}:
        raise Refused("TFTP process is not alive")
    return int(fields[1]), int(fields[19])


def write_metadata(descriptor, child, host):
    owner = os.getpid()
    _, owner_start = process_identity(owner)
    parent, child_start = process_identity(child.pid)
    if parent != owner:
        raise Refused("TFTP listener ownership changed")
    record = {"version": 1, "owner": owner, "owner_start": owner_start,
              "listener": child.pid, "listener_start": child_start,
              "namespace": os.readlink(PROC / "self/ns/pid"), "address": host}
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
    os.lseek(descriptor, 0, os.SEEK_SET)
    while encoded:
        count = os.write(descriptor, encoded)
        if not count:
            raise OSError("Could not save TFTP readiness")
        encoded = encoded[count:]
    os.fsync(descriptor)


def owner_holds_lock(record, info):
    for line in read_bounded(PROC / "locks", 1024 * 1024).splitlines():
        fields = line.split()
        if len(fields) != 8 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
            continue
        device = fields[5].split(":")
        if (len(device) == 3 and int(fields[4]) == record["owner"]
                and int(device[0], 16) == os.major(info.st_dev)
                and int(device[1], 16) == os.minor(info.st_dev)
                and int(device[2]) == info.st_ino):
            return True
    return False


def listener_has_socket(record):
    inodes = set()
    with os.scandir(PROC / str(record["listener"]) / "fd") as descriptors:
        for index, descriptor in enumerate(descriptors):
            if index >= 256:
                raise Refused("Unexpected TFTP listener descriptor count")
            try:
                target = os.readlink(descriptor.path)
            except FileNotFoundError:  # a concurrently closed, unrelated fd
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    # procfs represents IPv4 addresses as native-endian 32-bit integers.
    address = f"{int.from_bytes(socket.inet_aton(record['address']), sys.byteorder):08X}:0045"
    for line in read_bounded(PROC / "net/udp", 1024 * 1024).splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 10 and fields[1] == address and fields[9] in inodes:
            return True
    return False


def check_ready(path):
    descriptor = open_lock(path)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise Refused("No managed TFTP owner")
        data = os.read(descriptor, 2049)
        if len(data) > 2048:
            raise Refused("Oversized TFTP readiness record")
        record = json.loads(data)
        keys = {"version", "owner", "owner_start", "listener", "listener_start", "namespace", "address"}
        if (not isinstance(record, dict) or set(record) != keys
                or type(record["version"]) is not int or record["version"] != 1
                or any(type(record[key]) is not int or record[key] <= 0
                       for key in ("owner", "owner_start", "listener", "listener_start"))
                or not isinstance(record["namespace"], str)
                or not isinstance(record["address"], str)):
            raise Refused("Invalid TFTP readiness record")
        if str(ipaddress.IPv4Address(record["address"])) != record["address"]:
            raise Refused("Invalid TFTP readiness address")
        namespace = os.readlink(PROC / "self/ns/pid")
        if any(os.readlink(PROC / str(pid) / "ns/pid") != namespace
               for pid in (record["owner"], record["listener"])) or record["namespace"] != namespace:
            raise Refused("TFTP readiness belongs to another PID namespace")
        _, owner_start = process_identity(record["owner"])
        parent, listener_start = process_identity(record["listener"])
        if (owner_start != record["owner_start"] or parent != record["owner"]
                or listener_start != record["listener_start"]
                or not owner_holds_lock(record, os.fstat(descriptor))
                or not listener_has_socket(record)):
            raise Refused("Managed TFTP listener is not ready")
        # Detect exit/replacement while inspecting the kernel socket table.
        if (process_identity(record["owner"])[1] != owner_start
                or process_identity(record["listener"]) != (parent, listener_start)):
            raise Refused("TFTP listener changed during readiness")
        return 0
    finally:
        os.close(descriptor)


def drain_children(child):
    result = None
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            if result is None:
                raise Refused("TFTP listener exit was not observed")
            return result
        if pid == child.pid:
            result = os.waitstatus_to_exitcode(status)
            child.returncode = result
        if pid == 0:
            time.sleep(.05)


def main():
    if sys.argv[1:] == ["--check"]:
        return check_ready(os.environ["CISCO_TFTP_LOCK"])
    arguments, host, root = checked_arguments(sys.argv[1:])
    check_root(root)
    enable_subreaper()
    descriptor = acquire(os.environ["CISCO_TFTP_LOCK"])
    child, pending = None, None

    def forward(number, _frame):
        nonlocal pending
        pending = number
        if child is not None:
            try:
                os.killpg(child.pid, number)
            except ProcessLookupError:
                pass
            except OSError:
                print("Could not signal owned TFTP daemon; retaining ownership", file=sys.stderr)

    try:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, forward)
        if pending is not None:
            return 128 + pending
        child = subprocess.Popen([DAEMON, *arguments], stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        try:
            if pending is not None:
                forward(pending, None)
            try:
                write_metadata(descriptor, child, host)
            except (FileNotFoundError, ProcessLookupError, Refused):
                pass  # listener already exited; never publish readiness
        except BaseException:
            forward(signal.SIGTERM, None)
            drain_children(child)
            raise
        result = drain_children(child)
        return result if result >= 0 else 128 - result
    finally:
        # Metadata is cleared only after all owned descendants have drained.
        # Never unlink the inode: every contender must lock the same file.
        try:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as error:
        print(str(error), file=sys.stderr)
        sys.exit(75)
    except (ValueError, KeyError, IndexError):
        print("Managed TFTP ownership, arguments or readiness refused", file=sys.stderr)
        sys.exit(75)
    except OSError:
        print("Could not establish TFTP ownership, readiness or start daemon", file=sys.stderr)
        sys.exit(73)
