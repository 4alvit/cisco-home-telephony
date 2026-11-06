"""Exercise emitted APT configuration without downloading or building images."""
from pathlib import Path
import re
import shlex
import subprocess
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
IMAGES = ('asterisk', 'voice')
SNAPSHOT = '20261101T000000Z'


def run_commands(image, contains):
    text = (ROOT / 'containers' / image / 'Dockerfile').read_text()
    logical_lines = text.replace('\\\n', ' ').splitlines()
    instruction = next(line for line in logical_lines
                       if line.startswith('RUN ') and contains in line)
    commands = [[]]
    for token in shlex.split(instruction[4:]):
        if token == '&&':
            commands.append([])
        else:
            commands[-1].append(token)
    return commands


def emitted_files(commands):
    writes = []
    for index, command in enumerate(commands):
        if command[0] != 'printf' or '>' not in command:
            continue
        redirect = command.index('>')
        # Run the actual printf format through the shell, capturing its output.
        # No APT command, image build or privileged filesystem write is run.
        content = subprocess.check_output(
            ['/bin/sh', '-c', shlex.join(command[:redirect])], text=True)
        writes.append((index, command[redirect + 1], content))
    return writes


class AptSnapshotConfigurationTests(unittest.TestCase):
    def test_each_archive_has_two_ordered_https_mirrors_for_the_same_snapshot(self):
        expected_archives = {
            '/etc/apt/cisco-debian-snapshot.mirrors': 'debian',
            '/etc/apt/cisco-security-snapshot.mirrors': 'debian-security',
        }
        for image in IMAGES:
            with self.subTest(image=image):
                writes = emitted_files(run_commands(image, 'VERSION_CODENAME'))
                mirrors = {path: content for _, path, content in writes
                           if path.endswith('.mirrors')}
                self.assertEqual(set(mirrors), set(expected_archives))
                for path, archive in expected_archives.items():
                    lines = mirrors[path].splitlines()
                    self.assertEqual(len(lines), 2)
                    for priority, line in enumerate(lines, 1):
                        uri, metadata = line.split('\t')
                        parsed = urlsplit(uri)
                        self.assertEqual(metadata, f'priority:{priority}')
                        self.assertEqual(parsed.scheme, 'https')
                        self.assertEqual(parsed.netloc, (
                            'snapshot-cloudflare.debian.org',
                            'snapshot.debian.org')[priority - 1])
                        self.assertEqual(parsed.path, f'/archive/{archive}/{SNAPSHOT}/')
                        self.assertFalse(parsed.query or parsed.fragment)

    def test_final_sources_resolve_the_correct_archive_without_trust_bypasses(self):
        expected = {
            'trixie': '/etc/apt/cisco-debian-snapshot.mirrors',
            'trixie-updates': '/etc/apt/cisco-debian-snapshot.mirrors',
            'trixie-security': '/etc/apt/cisco-security-snapshot.mirrors',
        }
        for image in IMAGES:
            with self.subTest(image=image):
                writes = emitted_files(run_commands(image, 'VERSION_CODENAME'))
                final = {path: content for _, path, content in writes}
                entries = final['/etc/apt/sources.list'].splitlines()
                self.assertEqual(len(entries), 3)
                suites = set()
                for entry in entries:
                    kind, options, uri, suite, component = shlex.split(entry)
                    self.assertEqual((kind, options, component),
                                     ('deb', '[check-valid-until=no]', 'main'))
                    self.assertEqual(uri, 'mirror+file:' + expected[suite])
                    self.assertIn(expected[suite], final)
                    suites.add(suite)
                self.assertEqual(suites, set(expected))

    def test_pbx_bootstraps_ca_before_https_mirror_sources(self):
        commands = run_commands('asterisk', 'VERSION_CODENAME')
        writes = emitted_files(commands)
        source_writes = [(index, content) for index, path, content in writes
                         if path == '/etc/apt/sources.list']
        self.assertEqual(len(source_writes), 2)
        ca_install = commands.index(
            ['apt-get', 'install', '-y', '--no-install-recommends', 'ca-certificates'])
        self.assertLess(source_writes[0][0], commands.index(['apt-get', 'update']))
        self.assertLess(commands.index(['apt-get', 'update']), ca_install)
        self.assertLess(ca_install, source_writes[1][0])
        for entry in source_writes[0][1].splitlines():
            self.assertIn('http://snapshot.debian.org/archive/', entry)
            self.assertIn(f'/{SNAPSHOT} ', entry)
        for index, path, _ in writes:
            if path.endswith('.mirrors'):
                self.assertGreater(index, ca_install)

    def test_pbx_reduces_retries_after_bootstrap_and_mirror_setup(self):
        commands = run_commands('asterisk', 'VERSION_CODENAME')
        writes = emitted_files(commands)
        policy = [(index, content) for index, path, content in writes
                  if path == '/etc/apt/apt.conf.d/zz-cisco-network']
        self.assertEqual(len(policy), 1)
        policy_index, content = policy[0]
        self.assertEqual(content, 'Acquire::Retries "2";\nAcquire::http::Timeout "30";\n'
                                 'Acquire::https::Timeout "30";\n')
        ca_install = commands.index(
            ['apt-get', 'install', '-y', '--no-install-recommends', 'ca-certificates'])
        final_sources = max(index for index, path, _ in writes if path == '/etc/apt/sources.list')
        self.assertGreater(policy_index, ca_install)
        self.assertGreater(policy_index, final_sources)

    def test_all_apt_calls_start_with_bounded_network_policy(self):
        for image in (*IMAGES, 'tftp'):
            with self.subTest(image=image):
                writes = emitted_files(run_commands(image, 'Acquire::Retries'))
                retries = 2 if image == 'voice' else 6
                self.assertEqual([(path, content) for _, path, content in writes], [
                    ('/etc/apt/apt.conf.d/zz-cisco-network',
                     f'Acquire::Retries "{retries}";\nAcquire::http::Timeout "30";\n'
                     'Acquire::https::Timeout "30";\n')])
                text = (ROOT / 'containers' / image / 'Dockerfile').read_text()
                self.assertLess(text.index('/etc/apt/apt.conf.d/zz-cisco-network'),
                                text.index('apt-get'))
                self.assertIsNone(re.search(r'apt-get[^\n]*Acquire::', text))


if __name__ == '__main__':
    unittest.main()
