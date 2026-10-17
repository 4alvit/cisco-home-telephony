"""Offline provisioning must preserve account identity and keep its secret private."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import render_linphone
finally:
    sys.path.pop(0)


class LinphoneRendererTests(unittest.TestCase):
    def setUp(self):
        self.site = {'node_ipv4': '192.168.50.10', 'softphones': [
            {'extension': '209', 'label': 'Mac Linphone', 'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]}
        self.secrets = {'209': 'A_valid_existing-secret_1'}

    def test_xml_reuses_identity_secret_transport_and_transient_import(self):
        before = copy.deepcopy(self.secrets)
        for transport in ('tcp', 'udp'):
            self.site['softphones'][0]['transport'] = transport
            raw = render_linphone.render(self.site, self.secrets, '209')
            root = ET.fromstring(raw)
            self.assertEqual(root.tag, '{' + render_linphone.NAMESPACE + '}config')
            sections = {section.attrib['name']: {entry.attrib['name']: entry.text for entry in section}
                        for section in root}
            self.assertEqual(set(sections), {'proxy_0', 'auth_info_0', 'sip', 'misc'})
            self.assertEqual(sections['proxy_0'], {
                'reg_identity': '"Mac Linphone" <sip:209@192.168.50.10>',
                'reg_proxy': 'sip:192.168.50.10;transport=' + transport,
                'reg_sendregister': '1', 'reg_expires': '300'})
            self.assertEqual(sections['auth_info_0'], {
                'username': '209', 'domain': '192.168.50.10', 'passwd': self.secrets['209']})
            self.assertEqual(sections['sip'], {'default_proxy': '0', 'ipv6': '0'})
            self.assertEqual(sections['misc'], {'transient_provisioning': '1'})
            self.assertIn(b'&lt;sip:209@192.168.50.10&gt;', raw)
            self.assertTrue(all(entry.attrib['overwrite'] == 'true' for section in root for entry in section))
        self.assertEqual(self.secrets, before)

    def test_unknown_extension_missing_secret_or_public_domain_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'not a declared softphone'):
            render_linphone.render(self.site, self.secrets, '201')
        with self.assertRaisesRegex(ValueError, 'no existing secret'):
            render_linphone.render(self.site, {}, '209')
        for node in ('8.8.8.8', 'pbx.example.test', '127.0.0.1', '192.168.50.10;transport=tls', None):
            with self.subTest(node=node), self.assertRaises(ValueError):
                render_linphone.render(dict(self.site, node_ipv4=node), self.secrets, '209')

    def test_declared_client_schema_and_secrets_are_validated(self):
        for field, value in [('transport', 'tls'), ('source_ipv4', '0.0.0.0'), ('label', 'bad<xml>')]:
            site = copy.deepcopy(self.site)
            site['softphones'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                render_linphone.render(site, self.secrets, '209')
        for secrets in ({'209': 'short'}, {'209': self.secrets['209'], '210': self.secrets['209']}, []):
            with self.subTest(secrets_type=type(secrets).__name__), self.assertRaises(ValueError):
                render_linphone.render(self.site, secrets, '209')

    def test_private_directory_created_and_output_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'private' / 'linphone.xml'
            render_linphone.write_private(path, b'first')
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                render_linphone.write_private(path, b'replacement')
            self.assertEqual(path.read_bytes(), b'first')

    def test_symlink_output_and_nonprivate_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'target'; target.write_bytes(b'unchanged')
            output = root / 'output'; output.symlink_to(target)
            with self.assertRaises(FileExistsError):
                render_linphone.write_private(output, b'secret')
            self.assertEqual(target.read_bytes(), b'unchanged')
            output.unlink()
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, '0700'):
                render_linphone.write_private(output, b'secret')
            self.assertFalse(output.exists())
            root.chmod(0o700)

    def test_secret_input_must_be_private_regular_and_not_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / 'secrets.json'
            secret.write_text(json.dumps(self.secrets)); secret.chmod(0o600)
            self.assertEqual(render_linphone.read_json(secret, private=True), self.secrets)
            secret.chmod(0o644)
            with self.assertRaisesRegex(ValueError, '0600'):
                render_linphone.read_json(secret, private=True)
            secret.chmod(0o600)
            alias = secret.with_name('alias'); alias.symlink_to(secret)
            with self.assertRaises(OSError):
                render_linphone.read_json(alias, private=True)
            fifo = secret.with_name('fifo'); os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                render_linphone.read_json(fifo, private=True)

    def test_cli_prints_only_success_path_and_does_not_mutate_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / 'site.json'; site.write_text(json.dumps(self.site))
            secrets = root / 'secrets.json'; secrets.write_text(json.dumps(self.secrets)); secrets.chmod(0o600)
            before = secrets.read_bytes()
            output = root / 'new-private' / 'linphone.xml'
            args = ['--site', str(site), '--secrets', str(secrets), '--extension', '209', '--output', str(output)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(render_linphone.main(args), 0)
            self.assertEqual(stdout.getvalue(), str(output) + '\n')
            self.assertEqual(stderr.getvalue(), '')
            self.assertEqual(secrets.read_bytes(), before)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(render_linphone.main(args), 1)
            self.assertNotIn(self.secrets['209'], stdout.getvalue() + stderr.getvalue())
            self.assertNotIn('<config', stdout.getvalue() + stderr.getvalue())

    def test_invalid_input_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / 'site.json'; site.write_text(json.dumps(self.site))
            secrets = root / 'secrets.json'; secrets.write_text('{invalid secret JSON'); secrets.chmod(0o600)
            output = root / 'not-created' / 'linphone.xml'
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = render_linphone.main(['--site', str(site), '--secrets', str(secrets),
                                              '--extension', '209', '--output', str(output)])
            self.assertEqual(result, 1)
            self.assertFalse(output.parent.exists())
            self.assertEqual(stdout.getvalue(), '')
            self.assertNotIn('invalid secret JSON', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
