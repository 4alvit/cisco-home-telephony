"""Synthetic process/ownership tests; no TFTP listener or network traffic."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import sys
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "containers/tftp/entrypoint.sh"
RUNTIME = ROOT / "containers/tftp/runtime.py"
spec = importlib.util.spec_from_file_location("tftp_runtime", RUNTIME)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
ARGS = ["--foreground", "--user", "tftp", "--secure", "--address",
        "127.0.0.1:69", "--port-range", "20000:20031", "--blocksize", "1468", "/srv/tftp"]


@unittest.skipUnless(os.name == "posix", "POSIX descriptor locks and signals")
class TFTPManagedOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o755)
        self.lock = self.control / "owner.lock"
        self.served = self.root / "served"
        self.served.mkdir(mode=0o555)
        self.served.chmod(0o555)  # Match staged permissions regardless of process umask.
        self.addCleanup(self.make_writable)
        daemon = self.root / "fake-tftpd"
        daemon.write_text("""#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
os.closerange(3, 256)  # model a daemon closing all inherited descriptors
ready = Path(os.environ['TFTP_TEST_READY'])
release = Path(os.environ['TFTP_TEST_RELEASE'])
stopping = Path(os.environ['TFTP_TEST_STOPPING'])
def stop(number, frame):
    stopping.write_text(str(number))
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGHUP, stop)
if os.environ.get('TFTP_TEST_FORK') == '1':
    child = os.fork()
    if child:
        ready.write_text(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:], 'child': child}))
        sys.exit(17)
    Path(os.environ['TFTP_TEST_TRANSFER']).write_text(str(os.getpid()))
else:
    ready.write_text(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:]}))
if os.environ.get('TFTP_TEST_HOLD') == '1':
    while not release.exists():
        time.sleep(.01)
sys.exit(int(os.environ.get('TFTP_TEST_EXIT', '0')))
""")
        daemon.chmod(0o700)
        self.script = self.root / "entrypoint.sh"
        source = RUNTIME.read_text().replace('DAEMON = "/usr/sbin/in.tftpd"', f'DAEMON = {str(daemon)!r}')
        if not sys.platform.startswith("linux"):
            # Process-group tests work on POSIX; genuine subreaping is tested
            # separately on Linux (the only supported production platform).
            source = source.replace("    enable_subreaper()\n", "    pass  # synthetic non-Linux process test\n")
        local_runtime = self.root / "runtime.py"
        local_runtime.write_text(source)
        self.script.write_text(ENTRYPOINT.read_text().replace("/usr/sbin/in.tftpd", str(daemon))
                               .replace("/usr/local/lib/cisco-tftp/runtime.py", str(local_runtime)))
        self.processes = []
        self.addCleanup(self.finish_children)

    def make_writable(self):
        for directory, _, _ in os.walk(self.served):
            os.chmod(directory, 0o755)

    def finish_children(self):
        for process, files in self.processes:
            files["release"].touch()
        for process, files in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                # Only this test's synthetic child and supervisor are targeted.
                if files["ready"].exists():
                    pid = json.loads(files["ready"].read_text())["pid"]
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.kill()
                process.communicate(timeout=3)

    def start(self, *, managed=True, hold=False, args=None, lock=None, exit_code=0, fork=False):
        index = len(self.processes)
        files = {name: self.root / f"{index}-{name}" for name in ("ready", "release", "stopping", "transfer")}
        env = dict(os.environ, TFTP_TEST_HOLD="1" if hold else "0", TFTP_TEST_EXIT=str(exit_code),
                   **{f"TFTP_TEST_{key.upper()}": str(path) for key, path in files.items()})
        env.pop("CISCO_TFTP_LOCK", None)
        env["TFTP_TEST_FORK"] = "1" if fork else "0"
        if managed:
            env["CISCO_TFTP_LOCK"] = str(lock or self.lock)
        arguments = [str(self.served) if arg == "/srv/tftp" else arg
                     for arg in (ARGS if args is None else args)]
        process = subprocess.Popen(["/bin/sh", str(self.script), *arguments],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append((process, files))
        return process, files

    def ready(self, process, path):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text()) if path.name.endswith("ready") else path.read_text()
            if process.poll() is not None:
                self.fail(f"Child exited before readiness: {process.communicate()}")
            time.sleep(.01)
        self.fail("Synthetic child readiness deadline exceeded")

    def refused(self, *, lock=None, args=None):
        process, files = self.start(lock=lock, args=args)
        _, error = process.communicate(timeout=3)
        self.assertNotEqual(process.returncode, 0, error)
        self.assertFalse(files["ready"].exists())
        return error

    def test_unmanaged_keeps_arguments_and_exit_status(self):
        process, files = self.start(managed=False, args=["--version"], exit_code=23)
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 23)
        self.assertEqual(json.loads(files["ready"].read_text())["args"], ["--version"])
        self.assertFalse(self.lock.exists())

    def test_managed_private_inode_is_reused_and_bind_preserved(self):
        process, files = self.start()
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0)
        arguments = json.loads(files["ready"].read_text())["args"]
        self.assertEqual(arguments, ["--ipv4", *ARGS[:-1], str(self.served)])
        self.assertEqual(self.control.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)
        inode = self.lock.stat().st_ino
        second, _ = self.start()
        second.communicate(timeout=3)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_exclusive_even_when_daemon_closes_inherited_descriptors(self):
        first, files = self.start(hold=True)
        self.ready(first, files["ready"])
        self.assertIn("owner already active", self.refused())
        self.assertIsNone(first.poll())
        self.assertFalse(files["stopping"].exists())

    def test_signal_holds_lock_until_child_cleanup_finishes(self):
        first, files = self.start(hold=True)
        self.ready(first, files["ready"])
        first.terminate()
        self.assertEqual(self.ready(first, files["stopping"]), str(signal.SIGTERM))
        self.assertIsNone(first.poll())
        self.assertIn("owner already active", self.refused())
        files["release"].touch()
        first.communicate(timeout=3)
        self.assertEqual(first.returncode, 0)
        replacement, _ = self.start()
        replacement.communicate(timeout=3)
        self.assertEqual(replacement.returncode, 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper integration")
    def test_listener_exit_retains_lock_until_orphaned_transfer_exits(self):
        first, files = self.start(hold=True, fork=True)
        record = self.ready(first, files["ready"])
        self.ready(first, files["transfer"])
        time.sleep(.15)  # synthetic listener has exited; transfer remains alive
        self.assertIsNone(first.poll())
        self.assertIn("owner already active", self.refused())
        files["release"].touch()
        first.communicate(timeout=3)
        self.assertEqual(first.returncode, 17)
        self.assertEqual(self.lock.read_bytes(), b"")
        with self.assertRaises(ProcessLookupError):
            os.kill(record["child"], 0)
        replacement, _ = self.start()
        replacement.communicate(timeout=3)
        self.assertEqual(replacement.returncode, 0)

    def test_read_only_root_rejects_writable_files_symlinks_and_unknown_directories(self):
        for kind in ("writable", "symlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                self.served.chmod(0o755)
                target = self.served / kind
                if kind == "writable":
                    target.write_text("profile")
                    target.chmod(0o644)
                elif kind == "symlink":
                    target.symlink_to(self.root / "outside")
                elif kind == "directory":
                    target.mkdir(mode=0o555)
                else:
                    os.mkfifo(target, 0o444)
                self.served.chmod(0o555)
                self.refused()
                self.assertFalse(self.lock.exists())
                self.served.chmod(0o755)
                target.rmdir() if kind == "directory" else target.unlink()
        self.served.chmod(0o555)

    def test_reviewed_wallpaper_layout_and_0444_profile_are_accepted(self):
        self.served.chmod(0o755)
        profile = self.served / "SEP001122334455.cnf.xml"
        profile.write_text("profile")
        profile.chmod(0o444)
        wallpaper = self.served / "Desktops/320x212x16"
        wallpaper.mkdir(parents=True)
        for name in ("List.xml", "home.png", "home-thumb.png"):
            target = wallpaper / name
            target.write_text("wallpaper")
            target.chmod(0o444)
        for directory in (wallpaper, wallpaper.parent, self.served):
            directory.chmod(0o555)
        child, _ = self.start()
        child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0)

    def test_lock_symlink_is_rejected_without_touching_target(self):
        target = self.root / "target"
        target.write_text("untouched")
        target.chmod(0o600)
        self.lock.symlink_to(target)
        self.refused()
        self.assertEqual(target.read_text(), "untouched")
        self.assertTrue(self.lock.is_symlink())

    def test_directory_symlink_and_writable_directory_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.control, target_is_directory=True)
        self.refused(lock=alias / "owner.lock")
        self.assertFalse(self.lock.exists())
        self.control.chmod(0o777)
        self.refused()
        self.assertFalse(self.lock.exists())

    def test_hardlink_and_world_accessible_lock_are_rejected(self):
        self.lock.write_text("")
        self.lock.chmod(0o600)
        other = self.root / "other"
        os.link(self.lock, other)
        self.refused()
        other.unlink()
        self.lock.chmod(0o644)
        self.refused()
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o644)

    def test_nonregular_lock_does_not_block_open(self):
        os.mkfifo(self.lock, 0o600)
        self.refused()

    def test_upload_daemonization_and_ambiguous_bind_are_rejected(self):
        invalid = [ARGS + ["--create"], ARGS + ["-c"], ARGS + ["--listen"],
                   [arg for arg in ARGS if arg != "--foreground"],
                   ["::1:69" if arg == "127.0.0.1:69" else arg for arg in ARGS],
                   ["0.0.0.0:69" if arg == "127.0.0.1:69" else arg for arg in ARGS],
                   ARGS + ["--address", "127.0.0.2:69"],
                   [arg for arg in ARGS if arg not in {"--port-range", "20000:20031"}],
                   ["20000:30000" if arg == "20000:20031" else arg for arg in ARGS],
                   ["30000:30031" if arg == "20000:20031" else arg for arg in ARGS],
                   ["8192" if arg == "1468" else arg for arg in ARGS]]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.refused(args=arguments)
        self.assertFalse(self.lock.exists())


class TFTPReadinessTests(unittest.TestCase):
    """Kernel snapshots are synthetic; the smoke harness covers real procfs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        control = self.root / "control"
        control.mkdir(mode=0o700)
        self.lock = control / "owner.lock"
        self.descriptor = runtime.acquire(str(self.lock))
        self.addCleanup(os.close, self.descriptor)
        self.record = {"version": 1, "owner": 41, "owner_start": 100,
                       "listener": 42, "listener_start": 200,
                       "namespace": "pid:[77]", "address": "127.0.0.1"}
        self.write_record()
        for name in ("self", "41", "42"):
            folder = self.proc / name / "ns"
            folder.mkdir(parents=True)
            (folder / "pid").symlink_to("pid:[77]")
        self.process(41, parent=1, start=100)
        self.process(42, parent=41, start=200)
        descriptors = self.proc / "42/fd"
        descriptors.mkdir()
        (descriptors / "3").symlink_to("socket:[12345]")
        (self.proc / "net").mkdir()
        self.udp()
        info = os.fstat(self.descriptor)
        self.lock_line = (f"1: FLOCK ADVISORY WRITE 41 {os.major(info.st_dev):02x}:"
                          f"{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n")
        (self.proc / "locks").write_text(self.lock_line)
        patcher = mock.patch.object(runtime, "PROC", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_record(self):
        os.ftruncate(self.descriptor, 0)
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        os.write(self.descriptor, json.dumps(self.record).encode())

    def process(self, pid, *, parent, start, state="S"):
        fields = [state, str(parent), *(["0"] * 17), str(start)]
        (self.proc / str(pid) / "stat").write_text(f"{pid} (tftp daemon) {' '.join(fields)}")

    def udp(self, *, address=None, inode="12345"):
        if address is None:
            address = f"{int.from_bytes(bytes((127, 0, 0, 1)), sys.byteorder):08X}:0045"
        (self.proc / "net/udp").write_text(
            "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            f"0: {address} 00000000:0000 07 00000000:00000000 00:00000000 00000000 100 0 {inode}\n")

    def check(self):
        return runtime.check_ready(str(self.lock))

    def test_ready_requires_exact_owned_socket_and_live_identities(self):
        self.assertEqual(self.check(), 0)

    def test_another_daemons_socket_wrong_address_or_port_is_not_ready(self):
        for address, inode in ((None, "99999"), ("00000000:0045", "12345"),
                               ("0100007F:0046", "12345")):
            with self.subTest(address=address, inode=inode):
                self.udp(address=address, inode=inode)
                with self.assertRaises(runtime.Refused):
                    self.check()

    def test_dead_reused_or_reparented_listener_and_owner_are_not_ready(self):
        for parent, start, state in ((41, 200, "Z"), (41, 201, "S"), (1, 200, "S")):
            with self.subTest(parent=parent, start=start, state=state):
                self.process(42, parent=parent, start=start, state=state)
                with self.assertRaises(runtime.Refused):
                    self.check()
        self.process(42, parent=41, start=200)
        self.process(41, parent=1, start=101)
        with self.assertRaises(runtime.Refused):
            self.check()

    def test_unlocked_or_foreign_owned_lock_never_qualifies(self):
        (self.proc / "locks").write_text(self.lock_line.replace("WRITE 41", "WRITE 99"))
        with self.assertRaises(runtime.Refused):
            self.check()
        (self.proc / "locks").write_text(self.lock_line)
        runtime.fcntl.flock(self.descriptor, runtime.fcntl.LOCK_UN)
        with self.assertRaises(runtime.Refused):
            self.check()

    def test_foreign_namespace_and_unreadable_child_fds_fail_closed(self):
        namespace = self.proc / "42/ns/pid"
        namespace.unlink()
        namespace.symlink_to("pid:[88]")
        with self.assertRaises(runtime.Refused):
            self.check()
        namespace.unlink()
        namespace.symlink_to("pid:[77]")
        with mock.patch.object(runtime.os, "scandir", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.check()

    def test_malformed_oversized_and_boolean_pid_records_are_not_ready(self):
        for data in (b"{", b"x" * 2049,
                     json.dumps({**self.record, "listener": True}).encode()):
            with self.subTest(size=len(data)):
                os.ftruncate(self.descriptor, 0)
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                os.write(self.descriptor, data)
                with self.assertRaises((runtime.Refused, ValueError)):
                    self.check()


if __name__ == "__main__":
    unittest.main()
