"""Installer checks use disposable Git checkouts and synthetic launchers only."""
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / 'scripts' / 'install-voice-cli.sh'


class VoiceCLIInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'checkout'
        self.source = self.repo / 'scripts' / 'voice_session.py'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(
            'import json, os, sys\n'
            'print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), '
            '"marker": os.environ.get("VOICE_TEST_MARKER"), "python": sys.executable}))\n'
            'raise SystemExit(int(os.environ.get("VOICE_TEST_EXIT", "0")))\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        self.home = self.root / 'synthetic-home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.env = {**os.environ, 'HOME': str(self.home)}
        self.env.pop('VOICE_TEST_EXIT', None)
        self.env.pop('VOICE_TEST_MARKER', None)

    def install(self, *arguments, default_args=True, installer=INSTALLER, **options):
        defaults = ['--repo', str(self.repo), '--python', sys.executable] if default_args else []
        return subprocess.run(['bash', str(installer), *defaults, *map(str, arguments)],
                              cwd=options.pop('cwd', self.root),
                              env=options.pop('env', self.env), capture_output=True, text=True,
                              timeout=10, **options)

    def launch(self, target=None, *arguments, **options):
        return subprocess.run([str(target or self.bin / 'cisco-voice'), *arguments],
                              cwd=options.pop('cwd', self.root),
                              env=options.pop('env', self.env), capture_output=True, text=True,
                              timeout=5, **options)

    def fake_python(self, path, *, marker='selected fake interpreter'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nVOICE_TEST_MARKER=' + shlex.quote(marker) + '\n'
                        'export VOICE_TEST_MARKER\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        path.chmod(0o755)
        return path

    def reported_version_python(self, version):
        """Simulate only the version probe; run checks with the local Python."""
        path = self.root / ('python-reporting-' + version)
        path.write_text(
            '#!/bin/sh\nif [ "$1" = "-c" ]; then\n'
            '    case "$2" in\n'
            '        *cisco-voice-python*) printf "%s\\n" ' +
            shlex.quote('cisco-voice-python ' + version) + '; exit 0 ;;\n'
            '    esac\nfi\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        path.chmod(0o755)
        return path

    def timeout_dependency(self, label, *, version=None, available=True, callable_timeout=True):
        directory = self.root / ('dependency-' + label)
        directory.mkdir()
        marker = directory / 'imported'
        code = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("imported")\n'
        if not available:
            code += 'raise ImportError("synthetic missing async_timeout")\n'
        elif callable_timeout:
            code += 'def timeout(*args, **kwargs):\n    return None\n'
        else:
            code += 'timeout = None\n'
        (directory / 'async_timeout.py').write_text(code)
        if version is not None:
            metadata = directory / ('async_timeout-' + version + '.dist-info')
            metadata.mkdir()
            (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: async-timeout\n'
                                             'Version: ' + version + '\n')
        return {**self.env, 'PYTHONPATH': str(directory)}, marker

    def assert_installed(self, result, target=None):
        self.assertEqual(result.returncode, 0, result.stderr)
        target = target or self.bin / 'cisco-voice'
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertTrue(target.stat().st_mode & stat.S_IXUSR)
        self.assertIn('Managed by cisco-home-telephony install-voice-cli.sh', target.read_text())
        return target

    def test_help_is_side_effect_free_without_valid_python_or_checkout(self):
        result = self.install('--repo', '/missing', '--python', '/missing', '--help', default_args=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--prefix', result.stdout)
        self.assertIn('--bin-dir', result.stdout)
        self.assertIn('no overwrite', result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_default_destination_is_under_synthetic_home(self):
        result = self.install()
        target = self.assert_installed(result, self.home / '.local' / 'bin' / 'cisco-voice')
        self.assertIn('Installed cisco-voice:', result.stdout)
        self.assertEqual(json.loads(self.launch(target, '--help').stdout)['argv'], ['--help'])

    def test_default_repository_is_resolved_from_installer_location(self):
        copied = self.repo / 'scripts' / 'install-voice-cli.sh'
        shutil.copyfile(INSTALLER, copied)
        result = self.install('--python', sys.executable, '--bin-dir', self.bin,
                              default_args=False, installer=copied, cwd=self.home)
        self.assert_installed(result)
        self.assertIn(str(self.source), (self.bin / 'cisco-voice').read_text())

    def test_prefix_and_relative_bindir_destinations(self):
        prefix = self.root / 'prefix with spaces'
        self.assert_installed(self.install('--prefix', prefix), prefix / 'bin' / 'cisco-voice')
        result = self.install('--bin-dir', 'relative bin', cwd=self.home)
        self.assert_installed(result, self.home / 'relative bin' / 'cisco-voice')

    def test_special_paths_arguments_and_caller_directory_are_literal(self):
        special = "checkout ' space $(touch SHOULD_NOT_EXIST) `touch ALSO_NOT_EXIST`"
        moved = self.root / special
        self.repo.rename(moved)
        self.repo, self.source = moved, moved / 'scripts' / 'voice_session.py'
        python = self.fake_python(self.root / "venv ' $() `literal`" / 'bin' / "python's command")
        target_dir = self.root / "bin ' $() `literal`"
        result = self.install('--python', python, '--bin-dir', target_dir)
        target = self.assert_installed(result, target_dir / 'cisco-voice')
        caller = self.root / 'caller working directory'
        caller.mkdir()
        arguments = ['', 'argument with spaces', "single'quote", '$(touch SHOULD_NOT_EXIST)',
                     '`touch ALSO_NOT_EXIST`', '--flag=value', 'line one\nline two', '*', '$HOME']
        launched = self.launch(target, *arguments, cwd=caller)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        output = json.loads(launched.stdout)
        self.assertEqual(output['argv'], arguments)
        self.assertEqual(output['cwd'], str(caller))
        self.assertEqual(output['marker'], 'selected fake interpreter')
        self.assertFalse((self.root / 'SHOULD_NOT_EXIST').exists())
        self.assertFalse((caller / 'SHOULD_NOT_EXIST').exists())
        self.assertFalse((self.root / 'ALSO_NOT_EXIST').exists())
        self.assertFalse((caller / 'ALSO_NOT_EXIST').exists())

    def test_python_command_uses_path_and_preserves_virtual_environment_symlink(self):
        python = self.root / 'synthetic venv' / 'bin' / 'python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        env = {**self.env, 'PATH': str(python.parent) + os.pathsep + self.env.get('PATH', '')}
        result = self.install('--python', 'python', '--bin-dir', self.bin, env=env)
        target = self.assert_installed(result)
        self.assertIn(shlex.quote(str(python)), target.read_text())
        launched = self.launch(target)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        python.unlink()
        missing = self.launch(target)
        self.assertEqual(missing.returncode, 127)
        self.assertIn('Python unavailable', missing.stderr)

    def test_wrapper_propagates_exit_status(self):
        self.assert_installed(self.install('--bin-dir', self.bin))
        result = self.launch(env={**self.env, 'VOICE_TEST_EXIT': '37'})
        self.assertEqual(result.returncode, 37)

    def test_identical_install_is_idempotent_without_replacing_inode(self):
        target = self.assert_installed(self.install('--bin-dir', self.bin))
        before = target.stat()
        content = target.read_bytes()
        result = self.install('--bin-dir', self.bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Already installed cisco-voice:', result.stdout)
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual((target.stat().st_ino, target.stat().st_mtime_ns),
                         (before.st_ino, before.st_mtime_ns))
        self.assertEqual(list(self.bin.iterdir()), [target])

    def test_different_existing_command_and_modified_managed_wrapper_are_not_overwritten(self):
        self.bin.mkdir()
        target = self.bin / 'cisco-voice'
        for content in ('#!/bin/sh\necho unrelated\n',
                        '#!/bin/sh\n# Managed by cisco-home-telephony install-voice-cli.sh v1.\necho edited\n'):
            target.write_text(content)
            target.chmod(0o755)
            result = self.install('--bin-dir', self.bin)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Refusing', result.stderr)
            self.assertEqual(target.read_text(), content)
            self.assertEqual(list(self.bin.iterdir()), [target])

    def test_target_symlinks_dangling_links_and_directories_are_refused(self):
        self.bin.mkdir()
        victim = self.root / 'unrelated command'
        victim.write_text('do not change')
        target = self.bin / 'cisco-voice'
        for destination in (victim, self.root / 'missing target'):
            target.symlink_to(destination)
            result = self.install('--bin-dir', self.bin)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(target.is_symlink())
            self.assertEqual(victim.read_text(), 'do not change')
            self.assertEqual(list(self.bin.iterdir()), [target])
            target.unlink()
        target.mkdir()
        self.assertEqual(self.install('--bin-dir', self.bin).returncode, 1)
        self.assertEqual(list(target.iterdir()), [])

    def test_destination_directory_symlink_is_refused(self):
        actual = self.root / 'actual bin'
        actual.mkdir()
        self.bin.symlink_to(actual, target_is_directory=True)
        result = self.install('--bin-dir', self.bin)
        self.assertEqual(result.returncode, 1)
        self.assertIn('symlink', result.stderr)
        self.assertEqual(list(actual.iterdir()), [])

    def test_changed_interpreter_is_refused_and_original_wrapper_remains(self):
        target = self.assert_installed(self.install('--bin-dir', self.bin))
        original = target.read_bytes()
        other = self.fake_python(self.root / 'other-python')
        result = self.install('--python', other, '--bin-dir', self.bin)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_bytes(), original)

    def test_unknown_missing_and_mutually_exclusive_options_fail_without_installing(self):
        for arguments in [('--unknown',), ('--repo',), ('--python', ''),
                          ('--prefix', self.root / 'prefix', '--bin-dir', self.bin),
                          ('--bin-dir', self.bin, '--bin-dir', self.bin),
                          ('--bin-dir', '--help')]:
            with self.subTest(arguments=arguments):
                result = self.install(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn('install-voice-cli:', result.stderr)
                self.assertFalse(self.bin.exists())
                self.assertEqual(list(self.home.iterdir()), [])

    def test_invalid_python_and_version_fail_before_creating_destination(self):
        old = self.root / 'old-python'
        old.write_text('#!/bin/sh\nprintf "%s\\n" "cisco-voice-python 3.9"\n')
        old.chmod(0o755)
        not_executable = self.root / 'not executable'
        not_executable.write_text('not executable')
        for python in (old, '/bin/true', self.root / 'missing-python', not_executable):
            with self.subTest(python=python):
                result = self.install('--python', python, '--bin-dir', self.bin)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Python', result.stderr)
                self.assertFalse(self.bin.exists())

    def test_reported_python310_missing_timeout_fails_before_destination_creation(self):
        python = self.reported_version_python('3.10')
        env, imported = self.timeout_dependency('missing', available=False)
        result = self.install('--python', python, '--bin-dir', self.bin, env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('async', result.stderr.lower())
        self.assertTrue(imported.exists(), 'The Python 3.10 dependency probe did not import the module')
        self.assertFalse(self.bin.exists())

    def test_reported_python310_refuses_unsupported_timeout_versions(self):
        python = self.reported_version_python('3.10')
        for version in ('3.0.0', '6.0.0', 'invalid'):
            with self.subTest(version=version):
                env, imported = self.timeout_dependency(version, version=version)
                if version == '6.0.0':
                    env['PYTHONOPTIMIZE'] = '1'
                result = self.install('--python', python, '--bin-dir', self.bin, env=env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('async', result.stderr.lower())
                self.assertTrue(imported.exists())
                self.assertFalse(self.bin.exists())

    def test_reported_python310_accepts_timeout_four_and_five(self):
        python = self.reported_version_python('3.10')
        for version in ('4.0.3', '5.0.1'):
            with self.subTest(version=version):
                env, imported = self.timeout_dependency(version, version=version)
                destination = self.root / ('bin-' + version)
                result = self.install('--python', python, '--bin-dir', destination, env=env)
                target = self.assert_installed(result, destination / 'cisco-voice')
                self.assertTrue(imported.exists())
                # This proves installer selection and argv forwarding, not a
                # real Python 3.10 runtime or timeout implementation.
                launched = self.launch(target, 'synthetic', env=env)
                self.assertEqual(launched.returncode, 0, launched.stderr)
                self.assertEqual(json.loads(launched.stdout)['argv'], ['synthetic'])

    def test_reported_python310_requires_callable_timeout(self):
        python = self.reported_version_python('3.10')
        env, imported = self.timeout_dependency('noncallable', version='5.0.1', callable_timeout=False)
        result = self.install('--python', python, '--bin-dir', self.bin, env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('async', result.stderr.lower())
        self.assertTrue(imported.exists())
        self.assertFalse(self.bin.exists())

    def test_reported_python311_does_not_import_timeout_backport(self):
        python = self.reported_version_python('3.11')
        env, imported = self.timeout_dependency('not-needed', available=False)
        result = self.install('--python', python, '--bin-dir', self.bin, env=env)
        self.assert_installed(result)
        self.assertFalse(imported.exists(), 'Python 3.11 must not require or import the backport')

    def test_invalid_source_or_repository_context_fails_before_creating_destination(self):
        outside = self.root / 'not-a-repository'
        (outside / 'scripts').mkdir(parents=True)
        shutil.copyfile(self.source, outside / 'scripts' / 'voice_session.py')
        for repo in (outside, self.repo / 'scripts', self.root / 'missing'):
            with self.subTest(repo=repo):
                self.assertEqual(self.install('--repo', repo, '--bin-dir', self.bin).returncode, 1)
                self.assertFalse(self.bin.exists())
        self.source.unlink()
        self.assertEqual(self.install('--bin-dir', self.bin).returncode, 1)
        self.assertFalse(self.bin.exists())
        self.source.symlink_to(outside / 'scripts' / 'voice_session.py')
        self.assertEqual(self.install('--bin-dir', self.bin).returncode, 1)
        self.assertFalse(self.bin.exists())

    def test_moved_checkout_and_missing_python_produce_actionable_launcher_errors(self):
        selected = self.fake_python(self.root / 'selected-python')
        target = self.assert_installed(self.install('--python', selected, '--bin-dir', self.bin))
        moved = self.root / 'moved-checkout'
        self.repo.rename(moved)
        missing_source = self.launch(target)
        self.assertEqual(missing_source.returncode, 127)
        self.assertIn('source missing', missing_source.stderr)
        self.assertIn('stable checkout', missing_source.stderr)
        moved.rename(self.repo)
        selected.unlink()
        missing_python = self.launch(target)
        self.assertEqual(missing_python.returncode, 127)
        self.assertIn('Python unavailable', missing_python.stderr)
        self.assertIn('selected environment', missing_python.stderr)

    def test_concurrent_identical_installers_publish_one_complete_wrapper(self):
        command = ['bash', str(INSTALLER), '--repo', str(self.repo), '--python', sys.executable,
                   '--bin-dir', str(self.bin)]
        workers = [subprocess.Popen(command, cwd=self.root, env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for _ in range(6)]
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, stderr)
            self.assertIn('cisco-voice:', stdout)
        target = self.bin / 'cisco-voice'
        self.assertEqual(list(self.bin.iterdir()), [target])
        result = self.launch(target, 'complete')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['argv'], ['complete'])

    def test_publication_failure_removes_temporary_file_without_partial_target(self):
        python = self.root / 'python-with-link-failure'
        injected = ('import os, sys\n'
                    'code = sys.stdin.read()\n'
                    'sys.argv = sys.argv[1:]\n'
                    'def fail_link(*args, **kwargs):\n'
                    '    raise OSError("synthetic publication failure")\n'
                    'os.link = fail_link\n'
                    'exec(compile(code, "<installer>", "exec"))\n')
        python.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then\n'
                          '    exec ' + shlex.quote(sys.executable) + ' "$@"\nfi\n'
                          'exec ' + shlex.quote(sys.executable) + ' -c ' +
                          shlex.quote(injected) + ' "$@"\n')
        python.chmod(0o755)
        result = self.install('--python', python, '--bin-dir', self.bin)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Existing commands were not replaced', result.stderr)
        self.assertNotIn('synthetic publication failure', result.stderr)
        self.assertEqual(list(self.bin.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
