#!/usr/bin/env python3
"""Exercise real APT transport behavior against an isolated loopback origin.

Run inside an existing image with ``docker run --network none``; this script
never installs packages or contacts a package mirror. It is deliberately not
named test_*.py: ordinary unit discovery must not start transport experiments.
Use --require-apt-version 3.0.4 when qualifying the pinned Trixie runtime.
"""

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time


HELPER = '/usr/lib/apt/apt-helper'
PAYLOAD = bytes(range(256)) * (4 * 1024 * 1024 // 256)
SMALL_PAYLOAD = PAYLOAD[:4096]
# Public, synthetic fixture only. Its signing key was generated in an isolated
# temporary GPG home at 2024-01-01 and destroyed after signing. No expiry or
# Valid-Until is used; normal signature and date checks remain enabled.
FIXTURE_FINGERPRINT = "0B836673EBF8961AE4A86D751E8DE8A624850359"
FIXTURE_DEB = base64.b64decode(
    "ITxhcmNoPgpkZWJpYW4tYmluYXJ5LyAgMCAgICAgICAgICAgMCAgICAgMCAgICAgMTAwNjQ0ICA0"
    "ICAgICAgICAgYAoyLjAKY29udHJvbC50YXIuZ3ovIDAgICAgICAgICAgIDAgICAgIDAgICAgIDEw"
    "MDY0NCAgMjI1ICAgICAgIGAKH4sIAAAAAAAA/+3QwWoCMRAGYM95iryA21VWC4sUhV56KHiQ3sfs"
    "tA2uSZiMxcc3a8GDd7WF/4MwQ5KZCameXAwqsR/dTl3Mm+Yci+tYT6eTS/67/zwrwdY3fNPFIStJ"
    "GXmPWX/QmtyOvri1lHScJG55/OmPehA2HyzZx9DaSVWblbhvr+yGk3J5380b804+aFksrV2tN3bD"
    "We1iaKQlW/KR9qnnyocf6n33Yl45O/FJz03fcuxJubMqFHKKonaoMo/+EAAAAAAAAAAAAAAAAAAA"
    "AAAAgH/iBJtStkYAKAAACmRhdGEudGFyLmd6LyAgICAwICAgICAgICAgICAwICAgICAwICAgICAx"
    "MDA2NDQgIDQ1ICAgICAgICBgCh+LCAAAAAAAAP/twQENAAAAwqD3T20ON6AAAAAAAAAAAACANwOa"
    "3h0nACgAAAo="
)
FIXTURE_PACKAGES = b"""Package: apt-probe-fixture
Version: 1.0
Architecture: amd64
Maintainer: APT Test <apt-test@example.invalid>
Filename: pool/main/a/apt-probe-fixture/apt-probe-fixture_1.0_amd64.deb
Size: 464
SHA256: ae87e3a7f3d41791d726afeb8e469ba290bb8e2bde75217277ec7395ae9a6d51
Description: Isolated transport test

"""
FIXTURE_KEY = b"""-----BEGIN PGP PUBLIC KEY BLOCK-----

mQENBGWSAIABCADfkMtXj9HJJM4hufXDK/LhXaSfoWrvq73HqvgJcMD8EpRa04GA
Uo0RWZMp3dp86iPqn3Ttjyefn6/IroI64Y0Cqr2A7vbfI8IBFILHltk+hOLqlMez
IugrgMIjHPuiZZIDkEgCFgxmvBGLoTG6UIhFqEkFq5KFUMtMR4MJgcn8EvrDCbHy
L9wt/KxTzTO8UhOu6ho7XG0bjCLUSLdVN2OmnXb9w4/JEJU0G5v1sP2/btOrAGRU
yopwdafbjeD2sbKWnoV2wokhgkCu5ze1BBEThUOAPzJj0DNo/0bYRxYsiNK5DY4N
M6Uv9tnRnBkTuNqHkswvS2wT5OVwu9IJs+gpABEBAAG0LEFQVCBMb29wYmFjayBU
ZXN0IDxhcHQtdGVzdEBleGFtcGxlLmludmFsaWQ+iQFtBBMBCABXFiEEC4Nmc+v4
lhrkqG11Ho3opiSFA1kFAmWSAIAbFIAAAAAABAAObWFudTIsMi41KzEuMTIsMCwz
AhsDBQsJCAcCAiICBhUKCQgLAgQWAgMBAh4HAheAAAoJEB6N6KYkhQNZBSwH/0Ye
RxxupKofkdK1E2JGtVKAozetuemNkW7g0Qx47XlKvJk25KTyEVRY8fvv38Jl8bnG
xZYZtPv+QsGKm6BjrQYdBupmKeIVBb9c2rHafTYzePH5KSZyA1AxrEDveJ8Nbdoi
pcwj5K36egH/yhV89hUDKwX09AoXnJv8UFYXpsU6mf3r3baAcSKVG3FlAZtdwJHT
kAXoo0SvbK6Ki3QobLjfSotsew2UkKT1p0rhHfkyb2qoNWT57qcqSOvi8irOpo+S
gt85gQvDiPXa6n+6s7Ch7680c24hITBTQzMuvk4rcjYGo0Bmu7n7KqILB+xWds1W
ZTZSpaqP4FCBRjSsrSg=
=wV2y
-----END PGP PUBLIC KEY BLOCK-----
"""
FIXTURE_INRELEASE = b"""-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA256

Origin: APT Loopback Test
Label: APT Loopback Test
Suite: probe
Codename: probe
Date: Mon, 01 Jan 2024 00:00:00 UTC
Architectures: amd64
Components: main
Description: Isolated signed transport fixture
SHA256:
 528373ba002a6d9f8ee1889a636a7d18b924c2c926c2412055fb5049882bb784 301 main/binary-amd64/Packages
-----BEGIN PGP SIGNATURE-----

iQFPBAEBCAA5FiEEC4Nmc+v4lhrkqG11Ho3opiSFA1kFAmWSAIAbFIAAAAAABAAO
bWFudTIsMi41KzEuMTIsMCwzAAoJEB6N6KYkhQNZHzYIAN59Q34mT4RkJ82W3EEm
NVgxQkXgS7XaPEoUn84fO1pRTY2lQjLf1M6ept+eLX5DKN90dEfHzcHLrTBG7bdl
AbbqBTIu9TBNnZh37sZ4nf6OActhqjBTD8nXmGM0mh+bmhqEN2v5dec62OitPXKh
OqHSREyOmAIoI8LOZuBtPZZ/LxpmmbRvI94hK+Q4HJ7RAS5zM8nwMAwDkTxcG+ZR
Yz0Hqxtp84oeGno3I1FX9hm2mdJL3akv4u3gdIUp27vpwXA5N2uiDjXkOM6qrbw0
RjlzmMTJSQDmJD+dppkIZtWrSHdBY2R5W7f5bg9upwVEzLBIoIosPgXlCbhVcBqI
RWA=
=cDge
-----END PGP SIGNATURE-----
"""
FIXTURE_FILENAME = 'pool/main/a/apt-probe-fixture/apt-probe-fixture_1.0_amd64.deb'

CHILD_LIMITS = '''\
import os, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
os.execv(sys.argv[1], sys.argv[1:])
'''


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


REPOSITORY_CASES = ('repo_index_outage', 'repo_fallback',
                    'repo_corrupt', 'repo_tampered')


def repository_response(path):
    """The pool could serve indexes, but APT must never ask it for one."""
    parts = path.split('/', 3)
    if len(parts) != 4 or parts[1] not in REPOSITORY_CASES:
        return None
    _, case, mirror, suffix = parts
    if mirror not in ('snapshot', 'snapshot2', 'pool'):
        return 404, b'Unknown mirror\n'
    if suffix == FIXTURE_FILENAME:
        if mirror != 'pool':
            return 404, b'Snapshot payload unavailable\n'
        payload = FIXTURE_DEB
        if case == 'repo_corrupt':
            payload = bytes([payload[0] ^ 1]) + payload[1:]
        return 200, payload
    if case == 'repo_index_outage' and mirror != 'pool':
        return 404, b'Snapshot index unavailable\n'
    if suffix == 'dists/probe/InRelease':
        release = FIXTURE_INRELEASE
        if case == 'repo_tampered' and mirror != 'pool':
            release = release.replace(b'Label: APT Loopback Test',
                                      b'Label: Altered Test Data')
        return 200, release
    if suffix == 'dists/probe/main/binary-amd64/Packages':
        return 200, FIXTURE_PACKAGES
    return 404, b'Unknown fixture file\n'


class Origin(HTTPServer):
    """One bounded origin, with an independent request record for each case."""

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.requests = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        allowed = {'/baseline', '/limited', '/mismatch', '/retry0', '/retry2'}
        retry_after = None
        if self.path in allowed:
            earlier = sum(item['path'] == self.path for item in self.server.requests)
            retry_after = self.path[-1] if self.path.startswith('/retry') else None
            status = 503 if retry_after is not None and earlier == 0 else 200
            body = (b'Synthetic service unavailable\n' if status == 503 else
                    PAYLOAD if self.path in ('/baseline', '/limited') else SMALL_PAYLOAD)
        else:
            status, body = repository_response(self.path) or (404, b'Unknown fixture\n')
        self.server.requests.append({
            'path': self.path, 'at': time.monotonic(), 'status': status,
            'retry_after': retry_after if status == 503 else None,
        })
        self.send_response(status)
        if status == 503:
            self.send_header('Retry-After', retry_after)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def config_text(directory, limit):
    """Suppress system configuration and put writable APT paths in one tempdir."""
    values = {
        'Dir::Etc::main': directory / 'empty.conf',
        'Dir::Etc::parts': directory / 'parts',
        'Dir::State': directory / 'state',
        'Dir::Cache': directory / 'cache',
        'Dir::Log': directory / 'log',
        'Acquire::Retries': '2',
        'Acquire::Retries::Delay': 'true',
        'Acquire::Retries::HandleRetryAfter': 'true',
    }
    for scheme in ('http', 'https'):
        values.update({
            f'Acquire::{scheme}::Proxy': 'DIRECT',
            f'Acquire::{scheme}::Proxy-Auto-Detect': '',
            f'Acquire::{scheme}::Timeout': '5',
            f'Acquire::{scheme}::Pipeline-Depth': '0',
            f'Acquire::{scheme}::Dl-Limit': str(limit),
            f'Acquire::{scheme}::AllowRedirect': 'false',
        })
    return ''.join(f'{key} {json.dumps(str(value))};\n' for key, value in values.items())


def installed_limits():
    # Capture privately: a complete APT configuration may contain proxy secrets.
    # Only the two public policy values are returned or included in evidence.
    dump = subprocess.check_output(
        ['/usr/bin/apt-config', 'dump'], text=True, timeout=5,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    limits = {}
    for scheme in ('http', 'https'):
        key = f'Acquire::{scheme}::Dl-Limit'
        matches = re.findall(r'^' + re.escape(key) + r' "([0-9]+)";$',
                             dump, flags=re.MULTILINE | re.IGNORECASE)
        limits[scheme] = int(matches[0]) if len(matches) == 1 else None
    return limits


def private_environment(directory, config):
    return {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
            'HOME': str(directory), 'TMPDIR': str(directory), 'APT_CONFIG': str(config)}


def run_bounded(arguments, directory, env):
    command = [sys.executable, '-I', '-c', CHILD_LIMITS, *arguments]
    start = time.monotonic()
    process = subprocess.Popen(command, env=env, cwd=directory,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=20)
    finally:
        # Also terminate a transport child if its parent exits unexpectedly.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    return start, time.monotonic() - start, process.returncode, output


def run_case(origin, parent, name, limit, bad_hash=False):
    directory = parent / name
    directory.mkdir(mode=0o700)
    for child in ('parts', 'state', 'cache', 'log'):
        (directory / child).mkdir(mode=0o700)
    (directory / 'empty.conf').touch(mode=0o600)
    config = directory / 'apt.conf'
    config.write_text(config_text(directory, limit))
    body = PAYLOAD if name in ('baseline', 'limited') else SMALL_PAYLOAD
    expected = hashlib.sha256(body).hexdigest()
    destination = directory / 'download.bin'
    url = f'http://127.0.0.1:{origin.server_port}/{name}'
    env = private_environment(directory, config)
    start, elapsed, exit_code, output = run_bounded(
        [HELPER, 'download-file', url, str(destination),
         'SHA256:' + ('0' * 64 if bad_hash else expected)], directory, env)
    requests = [{**item, 'at': round(item['at'] - start, 4)}
                for item in origin.requests if item['path'] == '/' + name]
    result = {'case': name, 'limit_kib_s': limit, 'seconds': round(elapsed, 4),
              'exit_code': exit_code, 'requests': requests}
    if bad_hash:
        require(exit_code != 0 and 'Hash Sum mismatch' in output,
                'incorrect_sha256_was_not_rejected')
        result['hash_mismatch_rejected'] = True
    else:
        require(exit_code == 0, f'{name}_download_failed: {output[-2000:]}')
        actual = hashlib.sha256(destination.read_bytes()).hexdigest()
        require(actual == expected and destination.stat().st_size == len(body),
                f'{name}_payload_mismatch')
        result.update({'bytes': len(body), 'sha256': actual})
    if name == 'limited':
        # 4 MiB at 1000 KiB/s takes approximately four seconds. Allow the
        # transport's initial buffer and timer granularity, not an exact rate.
        require(elapsed >= 3.0, 'download_limit_did_not_slow_four_mib_transfer')
    if name.startswith('retry'):
        require(len(requests) == 2 and [r['status'] for r in requests] == [503, 200],
                f'{name}_unexpected_retry_sequence')
        delay = requests[1]['at'] - requests[0]['at']
        # Retry-After uses wall-clock seconds; retain tolerance for rounding.
        require(delay >= (0.8 if name == 'retry0' else 1.8),
                f'{name}_retry_delay_not_observed')
        result['retry_gap_seconds'] = round(delay, 4)
    return result


def run_repository_case(origin, parent, name):
    directory = parent / name
    directory.mkdir(mode=0o700)
    for child in ('parts', 'state/lists/partial', 'cache/archives/partial',
                  'log', 'sources.d', 'trusted.d', 'downloads'):
        (directory / child).mkdir(mode=0o700, parents=True, exist_ok=True)
    for child in ('empty.conf', 'status', 'empty.gpg'):
        (directory / child).touch(mode=0o600)
    key = directory / 'fixture.asc'
    key.write_bytes(FIXTURE_KEY)
    mirrors = directory / 'mirrors'
    base = f'http://127.0.0.1:{origin.server_port}/{name}'
    mirrors.write_text(f'{base}/snapshot/\tpriority:1\n'
                       f'{base}/snapshot2/\tpriority:2\n'
                       f'{base}/pool/\tpriority:3 type:deb\n')
    sources = directory / 'sources.list'
    sources.write_text(f'deb [arch=amd64 signed-by={key}] '
                       f'mirror+file:{mirrors} probe main\n')
    values = {
        'Dir::Etc': directory,
        'Dir::Etc::sourcelist': sources,
        'Dir::Etc::sourceparts': directory / 'sources.d',
        'Dir::Etc::trusted': directory / 'empty.gpg',
        'Dir::Etc::trustedparts': directory / 'trusted.d',
        'Dir::State::status': directory / 'status',
        'Dir::State::lists': directory / 'state/lists',
        'Dir::Cache::archives': directory / 'cache/archives',
        'Dir::Cache::pkgcache': '',
        'Dir::Cache::srcpkgcache': '',
        'APT::Architecture': 'amd64',
        'APT::Update::Error-Mode': 'any',
        'Acquire::Languages': 'none',
        'APT::Get::AllowUnauthenticated': 'false',
    }
    config = directory / 'apt.conf'
    config.write_text(config_text(directory, 1000) +
                      ''.join(f'{key} {json.dumps(str(value))};\n'
                              for key, value in values.items()) +
                      'APT::Architectures { "amd64"; };\n')
    env = private_environment(directory, config)
    start, elapsed, update_code, output = run_bounded(
        ['/usr/bin/apt-get', 'update'], directory, env)
    result = {'case': name, 'update_exit_code': update_code,
              'update_seconds': round(elapsed, 4),
              'signing_fingerprint': FIXTURE_FINGERPRINT}
    negative_index = name in ('repo_index_outage', 'repo_tampered')
    if negative_index:
        require(update_code != 0, f'{name}_untrusted_or_missing_index_was_accepted')
        if name == 'repo_tampered':
            require(re.search(r'signature|sqv|not signed', output, re.IGNORECASE),
                    f'{name}_failed_without_signature_diagnostic: {output[-1200:]}')
            result['tampered_signature_rejected'] = True
        else:
            result['missing_snapshot_index_rejected'] = True
    else:
        require(update_code == 0, f'{name}_signed_index_failed: {output[-1200:]}')
        _, elapsed, code, output = run_bounded(
            ['/usr/bin/apt-get', 'download', 'apt-probe-fixture=1.0'],
            directory / 'downloads', env)
        result.update({'download_exit_code': code, 'download_seconds': round(elapsed, 4)})
        if name == 'repo_corrupt':
            require(code != 0 and 'Hash Sum mismatch' in output,
                    'corrupt_pool_payload_was_not_rejected')
            result['corrupt_payload_rejected'] = True
        else:
            require(code == 0, f'pool_fallback_failed: {output[-1200:]}')
            downloaded = list((directory / 'downloads').glob('*.deb'))
            require(len(downloaded) == 1 and downloaded[0].read_bytes() == FIXTURE_DEB,
                    'pool_fallback_bytes_differ')
            result.update({'bytes': len(FIXTURE_DEB),
                           'sha256': hashlib.sha256(FIXTURE_DEB).hexdigest()})
    requests = [{**item, 'at': round(item['at'] - start, 4)}
                for item in origin.requests if item['path'].startswith('/' + name + '/')]
    result['requests'] = requests
    require(requests, f'{name}_made_no_fixture_requests')
    pool = [item for item in requests if item['path'].startswith(f'/{name}/pool/')]
    require(not any('/dists/' in item['path'] for item in pool),
            'payload_only_pool_was_asked_for_an_index')
    result['pool_index_requests'] = 0
    if negative_index:
        require(not pool and not any(item['path'].endswith('.deb') for item in requests),
                'payload_downloaded_without_trusted_snapshot_index')
    else:
        require(any(item['path'].endswith(FIXTURE_FILENAME) for item in pool),
                'pool_payload_fallback_not_exercised')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-apt-version', help='Fail before tests on a version mismatch')
    args = parser.parse_args()
    result = {'status': 'FAIL', 'network': 'loopback_only', 'package_install': False,
              'uid': os.geteuid(), 'cases': []}
    try:
        require(os.geteuid() != 0, 'probe_requires_nonroot')
        version_text = subprocess.check_output(
            ['/usr/bin/apt', '--version'], text=True, timeout=5).strip()
        result['apt_version_text'] = version_text
        match = re.fullmatch(r'apt (\S+) \([^)]+\)', version_text)
        require(match is not None, 'unrecognized_apt_version')
        result['apt_version'] = match.group(1)
        if args.require_apt_version:
            require(match.group(1) == args.require_apt_version, 'apt_version_mismatch')
        require(Path(HELPER).is_file(), 'apt_helper_missing')
        result['installed_limit_kib_s'] = installed_limits()
        require(result['installed_limit_kib_s'] == {'http': 1000, 'https': 1000},
                'installed_http_https_limits_must_both_equal_1000')
        with tempfile.TemporaryDirectory(prefix='apt-loopback-probe-') as scratch:
            origin = Origin()
            thread = threading.Thread(target=origin.serve_forever,
                                      kwargs={'poll_interval': 0.1}, daemon=True)
            thread.start()
            try:
                for name, limit, bad_hash in (
                    ('baseline', 0, False), ('limited', 1000, False),
                    ('mismatch', 1000, True), ('retry0', 1000, False),
                    ('retry2', 1000, False),
                ):
                    result['cases'].append(run_case(origin, Path(scratch), name, limit, bad_hash))
                    if name == 'limited':
                        baseline, limited = result['cases'][:2]
                        require(limited['seconds'] >= baseline['seconds'] + 1.5,
                                'limited_transfer_not_demonstrably_slower_than_baseline')
                for name in REPOSITORY_CASES:
                    result['cases'].append(run_repository_case(origin, Path(scratch), name))
            finally:
                origin.shutdown()
                origin.server_close()
                thread.join(timeout=5)
                require(not thread.is_alive(), 'origin_thread_did_not_stop')
        result['status'] = 'PASS'
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result['error'] = str(exc)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
