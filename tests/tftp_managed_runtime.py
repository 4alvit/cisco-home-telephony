#!/usr/bin/env python3
"""Exercise managed TFTP in disposable Docker resources, never a live phone.

Run explicitly with an existing local image: TFTP_TEST_IMAGE=image python3
tests/tftp_managed_runtime.py. A positional image is also accepted. The filename
deliberately excludes this Docker test from ordinary unittest discovery.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid


PROFILE_NAME = "SEP020000000001.cnf.xml"
PROFILE_XML = ("<device><loadInformation>SIP45.synthetic</loadInformation>"
               "<sipProfile><dialTemplate>dialplan.xml</dialTemplate></sipProfile>"
               "<vendorConfig><g722CodecSupport>2</g722CodecSupport></vendorConfig>"
               "<!--" + "synthetic-profile-only " * 400 + "--></device>\n")
FILES = {PROFILE_NAME: PROFILE_XML,
         "dialplan.xml": '<DIALTEMPLATE><TEMPLATE MATCH="2.." Timeout="0"/></DIALTEMPLATE>\n'}
LOCK = "/run/cisco-tftp/owner.lock"
READY = "/run/cisco-tftp/held.json"
RESUME = "/run/cisco-tftp/resume"

INITIALIZE = r"""
import json, os, pathlib, sys
files = json.load(sys.stdin)
root = pathlib.Path('/srv/tftp')
assert not any(root.iterdir()), 'Synthetic served volume was not empty'
for name, content in files.items():
    target = root / name
    target.write_text(content, encoding='utf-8')
    target.chmod(0o444)
root.chmod(0o555)
os.chmod('/run/cisco-tftp', 0o700)
"""

CLIENT = r"""
import errno, hashlib, json, os, pathlib, socket, struct, sys, time
mode, name = sys.argv[1:]
host = '127.0.0.1'

def request(sock, opcode, filename):
    sock.sendto(struct.pack('!H', opcode) + filename.encode('ascii') + b'\0octet\0', (host, 69))

def receive(sock):
    packet, peer = sock.recvfrom(2048)
    assert peer[0] == host and len(packet) >= 4, 'Malformed or foreign TFTP response'
    return packet, peer

def download(hold=False):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((host, 0))
        sock.settimeout(5)
        request(sock, 1, name)
        body, expected, server = bytearray(), 1, None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            packet, peer = receive(sock)
            if server is None:
                assert 20000 <= peer[1] <= 20031, 'Transfer outside configured port range'
                server = peer
            assert peer == server, 'TFTP transfer identity changed'
            opcode, block = struct.unpack('!HH', packet[:4])
            assert opcode == 3, 'RRQ did not return DATA'
            if block == expected - 1:
                sock.sendto(struct.pack('!HH', 4, block), peer)
                continue
            assert block == expected, 'Unexpected TFTP block'
            if hold and expected == 1:
                marker = pathlib.Path('/run/cisco-tftp/held.tmp')
                marker.write_text(json.dumps({'port': peer[1]}))
                marker.replace('/run/cisco-tftp/held.json')
                paused_until = time.monotonic() + 45
                while not pathlib.Path('/run/cisco-tftp/resume').exists():
                    assert time.monotonic() < paused_until, 'Held-transfer release timeout'
                    time.sleep(.05)
                deadline = time.monotonic() + 15
            body.extend(packet[4:])
            assert len(body) <= 128 * 1024, 'Unexpected oversized TFTP response'
            sock.sendto(struct.pack('!HH', 4, block), peer)
            if len(packet) < 516:
                print(json.dumps({'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}), flush=True)
                return
            expected += 1
        raise AssertionError('TFTP download deadline exceeded')

if mode in ('read', 'hold'):
    download(mode == 'hold')
elif mode == 'deny':
    for opcode, filename in ((1, 'SIP45.synthetic.loads'), (1, 'apps45.synthetic.sbn'),
                             (1, '../' + name), (1, '/etc/passwd'),
                             (2, 'new-upload.xml'), (2, name)):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((host, 0))
            sock.settimeout(5)
            request(sock, opcode, filename)
            packet, _ = receive(sock)
            operation, code = struct.unpack('!HH', packet[:4])
            assert operation == 5 and code in (1, 2), 'Forbidden RRQ/WRQ was not denied'
    root = pathlib.Path('/srv/tftp')
    assert {p.name for p in root.iterdir()} == {name, 'dialplan.xml'}, 'WRQ created a file'
    assert os.statvfs(root).f_flag & os.ST_RDONLY, 'Served root mount is writable'
    try:
        fd = os.open(root / name, os.O_WRONLY)
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES), 'Unexpected write refusal'
    else:
        os.close(fd)
        raise AssertionError('Served profile is writable')
    print(json.dumps({'denied': 6}), flush=True)
else:
    raise AssertionError('Unknown synthetic client mode')
"""

FIND_TRANSFER = r"""
import json, os, pathlib
record = json.loads(pathlib.Path('/run/cisco-tftp/owner.lock').read_text())
port = json.loads(pathlib.Path('/run/cisco-tftp/held.json').read_text())['port']
inodes = {line.split()[9] for line in pathlib.Path('/proc/net/udp').read_text().splitlines()[1:]
          if int(line.split()[1].split(':')[1], 16) == port}
assert len(inodes) == 1, 'Held transfer socket is ambiguous'
matches = []
for process in pathlib.Path('/proc').iterdir():
    if not process.name.isdigit() or int(process.name) <= 1:
        continue
    try:
        descriptors = list((process / 'fd').iterdir())
        owns_socket = any(os.readlink(fd) in {'socket:[' + inode + ']' for inode in inodes}
                          for fd in descriptors)
        if owns_socket:
            fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
            assert int(fields[1]) in (record['owner'], record['listener']), 'Foreign transfer process'
            matches.append({'pid': int(process.name), 'start': int(fields[19])})
    except FileNotFoundError:
        continue
assert len(matches) == 1 and matches[0]['pid'] != record['listener'], 'Transfer child not identified'
print(json.dumps({'transfer': matches[0], 'listener': {'pid': record['listener'],
                                                     'start': record['listener_start']}}))
"""

SIGNAL_EXACT = r"""
import json, os, pathlib, signal, sys
identity = json.loads(sys.argv[1])
assert identity['pid'] > 1, 'Refusing to signal supervisor'
fields = pathlib.Path('/proc', str(identity['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
assert int(fields[19]) == identity['start'] and fields[0] not in ('Z', 'X'), 'Process identity changed'
os.kill(identity['pid'], getattr(signal, sys.argv[2]))
"""

PROCESS_STATE = r"""
import json, pathlib, sys
identity = json.loads(sys.argv[1])
try:
    fields = pathlib.Path('/proc', str(identity['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
    state = fields[0] if int(fields[19]) == identity['start'] else 'replaced'
except FileNotFoundError:
    state = 'absent'
print(json.dumps(state))
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(predicate, message, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise RuntimeError(message)


class Fixture:
    def __init__(self, image):
        self.image = image
        self.prefix = "cisco-tftp-runtime-" + uuid.uuid4().hex[:12]
        self.network = self.prefix + "-net"
        self.served = self.prefix + "-profiles"
        self.lock = self.prefix + "-lock"
        self.containers, self.volumes = [], []
        self.network_created = False
        self.held = None

    @staticmethod
    def docker(*arguments, input=None, check=True, timeout=30):
        result = subprocess.run(["docker", *arguments], input=input, capture_output=True,
                                text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("Docker " + arguments[0] + " failed: " + result.stderr[-2000:].strip())
        return result

    def execute(self, container, code, *arguments, check=True, timeout=15):
        return self.docker("exec", container, "python3", "-c", code, *arguments,
                           check=check, timeout=timeout)

    def setup(self):
        require(self.docker("info", "--format", "{{.OSType}}").stdout.strip() == "linux",
                "Managed TFTP test requires a Linux Docker daemon")
        self.docker("image", "inspect", self.image)  # Never pull or build implicitly.
        for volume in (self.served, self.lock):
            self.volumes.append(volume)
            self.docker("volume", "create", "--label", "cisco.test=" + self.prefix, volume)
        self.network_created = True
        self.docker("network", "create", "--internal", "--label", "cisco.test=" + self.prefix, self.network)
        initialize = self.prefix + "-initialize"
        self.containers.append(initialize)
        self.docker("run", "--rm", "--pull=never", "-i", "--name", initialize,
                    "--network", "none", "--read-only", "--user", "0:0",
                    "--mount", "type=volume,src=" + self.served + ",dst=/srv/tftp",
                    "--mount", "type=volume,src=" + self.lock + ",dst=/run/cisco-tftp",
                    "--entrypoint", "python3", self.image, "-c", INITIALIZE,
                    input=json.dumps(FILES))

    def start(self, suffix):
        name = self.prefix + "-" + suffix
        self.containers.append(name)
        self.docker("run", "-d", "--pull=never", "--name", name,
                    "--label", "cisco.test=" + self.prefix, "--network", self.network,
                    "--read-only", "--user", "0:0", "--cap-drop", "ALL",
                    "--cap-add", "NET_BIND_SERVICE", "--cap-add", "SYS_CHROOT",
                    "--cap-add", "SETUID", "--cap-add", "SETGID",
                    "--cap-add", "KILL", "--cap-add", "SYS_PTRACE",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777",
                    "--mount", "type=volume,src=" + self.served + ",dst=/srv/tftp,readonly",
                    "--mount", "type=volume,src=" + self.lock + ",dst=/run/cisco-tftp",
                    "--env", "CISCO_TFTP_LOCK=" + LOCK,
                    self.image, "--foreground", "--ipv4", "--user", "tftp", "--secure",
                    "--address", "127.0.0.1:69", "--port-range", "20000:20031",
                    "--blocksize", "1468", "/srv/tftp")
        return name

    def state(self, container):
        return json.loads(self.docker("inspect", "--format", "{{json .State}}", container).stdout)

    def ready(self, container):
        return self.docker("exec", container, "/usr/local/bin/cisco-tftp", "--check",
                           check=False, timeout=5).returncode == 0

    def denied_owner(self, suffix):
        contender = self.start(suffix)
        wait_for(lambda: not self.state(contender)["Running"], "Second owner did not refuse the held lock")
        require(self.state(contender)["ExitCode"] == 75, "Contender did not exit with ownership refusal")
        logs = self.docker("logs", contender)
        require("TFTP owner already active" in logs.stdout + logs.stderr,
                "Second owner failed for a reason other than the held lock")

    def download(self, container):
        result = json.loads(self.execute(container, CLIENT, "read", PROFILE_NAME).stdout)
        require(result == {"bytes": len(PROFILE_XML.encode()),
                           "sha256": hashlib.sha256(PROFILE_XML.encode()).hexdigest()},
                "Downloaded profile did not match the synthetic fixture")

    def process_state(self, container, identity):
        return json.loads(self.execute(container, PROCESS_STATE, json.dumps(identity)).stdout)

    def signal(self, container, identity, name):
        self.execute(container, SIGNAL_EXACT, json.dumps(identity), name)

    def run(self):
        self.setup()
        first = self.start("owner")
        wait_for(lambda: self.ready(first), "Managed TFTP readiness did not pass")
        self.download(first)
        self.execute(first, CLIENT, "deny", PROFILE_NAME, timeout=35)
        self.download(first)  # An existing-profile WRQ must not change its bytes.
        self.denied_owner("contender")
        require(self.ready(first), "Rejected contender affected the active listener")
        print("Managed TFTP: RRQ, read-only root, forbidden paths/firmware/WRQ and exclusive owner passed.", flush=True)

        # Leave a real DATA block unacknowledged, then stop only its exact child.
        # Its independent PID namespace lets the client finish and report after
        # the owner's PID 1 exits; only the isolated network namespace is shared.
        client = self.prefix + "-held-client"
        self.containers.append(client)
        self.held = subprocess.Popen(["docker", "run", "--rm", "--pull=never", "--name", client,
                                      "--label", "cisco.test=" + self.prefix,
                                      "--network", "container:" + first, "--read-only",
                                      "--user", "0:0", "--cap-drop", "ALL",
                                      "--mount", "type=volume,src=" + self.lock + ",dst=/run/cisco-tftp",
                                      "--entrypoint", "python3", self.image, "-u", "-c",
                                      CLIENT, "hold", PROFILE_NAME],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wait_for(lambda: self.execute(first, "from pathlib import Path; import sys; "
                                      "sys.exit(not Path(" + repr(READY) + ").is_file())",
                                      check=False).returncode == 0,
                 "Real transfer did not reach its first DATA block")
        identities = json.loads(self.execute(first, FIND_TRANSFER).stdout)
        transfer, listener = identities["transfer"], identities["listener"]
        self.signal(first, transfer, "SIGSTOP")
        wait_for(lambda: self.process_state(first, transfer) in ("T", "t"),
                 "Transfer child did not stop")
        self.signal(first, listener, "SIGTERM")
        wait_for(lambda: self.process_state(first, listener) in ("absent", "Z"),
                 "Listener did not exit")
        require(self.state(first)["Running"], "Supervisor exited while a transfer was still alive")
        require(not self.ready(first), "Readiness passed after its exact listener exited")
        self.denied_owner("drain-contender")
        require(self.process_state(first, transfer) in ("T", "t"),
                "Transfer child disappeared before release")
        self.execute(first, "from pathlib import Path; Path(" + repr(RESUME) + ").touch()")
        self.signal(first, transfer, "SIGCONT")
        output, error = self.held.communicate(timeout=20)
        require(self.held.returncode == 0, "Held real transfer failed to finish: " + error[-1000:])
        require(json.loads(output) == {"bytes": len(PROFILE_XML.encode()),
                                      "sha256": hashlib.sha256(PROFILE_XML.encode()).hexdigest()},
                "Held transfer did not finish with its original profile bytes")
        wait_for(lambda: not self.state(first)["Running"], "Supervisor did not exit after transfer drain")
        require(self.state(first)["ExitCode"] in (0, 143),
                "Drained supervisor exited abnormally")

        replacement = self.start("replacement")
        wait_for(lambda: self.ready(replacement), "Replacement owner could not acquire released lock")
        self.download(replacement)
        self.docker("stop", "--time", "10", replacement, timeout=15)
        stopped = self.state(replacement)
        require(not stopped["Running"] and stopped["ExitCode"] in (0, 143),
                "Replacement did not shut down gracefully")
        print("Managed TFTP: listener-loss readiness, transfer drain, retained lock and replacement passed.", flush=True)

    def cleanup(self):
        failures = []
        if self.held is not None and self.held.poll() is None:
            try:
                self.held.terminate()
                try:
                    self.held.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    self.held.kill()
                    self.held.communicate(timeout=3)
            except (OSError, subprocess.SubprocessError):
                failures.append("held-client CLI process")
        resources = [("container", name) for name in reversed(self.containers)]
        if self.network_created:
            resources.append(("network", self.network))
        resources.extend(("volume", name) for name in reversed(self.volumes))
        for kind, name in resources:
            try:
                command = ("rm", "-f", name) if kind == "container" else (kind, "rm", name)
                result = self.docker(*command, check=False, timeout=15)
                if result.returncode and "No such " + kind not in result.stderr:
                    failures.append(name)
            except (OSError, subprocess.SubprocessError):
                failures.append(name)
        return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", default=os.environ.get("TFTP_TEST_IMAGE"))
    args = parser.parse_args()
    if not args.image:
        parser.error("Supply an existing image argument or TFTP_TEST_IMAGE")
    fixture, failed = Fixture(args.image), False
    try:
        fixture.run()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        failed = True
        print("Managed TFTP runtime FAILED: " + str(error), file=sys.stderr)
        for container in fixture.containers:
            try:
                result = fixture.docker("logs", "--tail", "40", container, check=False, timeout=5)
                if result.returncode == 0:
                    print(container + ":\n" + (result.stdout + result.stderr)[-6000:], file=sys.stderr)
            except (OSError, subprocess.SubprocessError):
                pass
    finally:
        try:
            leftovers = fixture.cleanup()
            if leftovers:
                failed = True
                print("Managed TFTP cleanup incomplete: " + ", ".join(leftovers), file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as error:
            failed = True
            print("Managed TFTP cleanup failed: " + str(error), file=sys.stderr)
    if not failed:
        print("Managed TFTP runtime PASS; disposable containers, volumes and network removed.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
