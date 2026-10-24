"""Reject actionable image findings and incomplete package evidence."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('voice_security', ROOT / 'scripts/verify_voice_security.py')
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


def report():
    return {'SchemaVersion': 2, 'ArtifactType': 'container_image', 'Results': [
        {'Class': 'os-pkgs', 'Type': 'debian', 'Packages': [
            {'Name': name, 'Version': version, 'Release': release}
            for name, (version, release) in security.EXACT_PACKAGES.items()]},
        {'Class': 'lang-pkgs', 'Type': 'python-pkg', 'Packages': [
            {'Name': 'aiohttp', 'Version': '3.14.0'}]},
    ]}


class ImageSecurityTests(unittest.TestCase):
    def test_clean_complete_inventory_passes(self):
        self.assertTrue(security.verify(report())['passed'])

    def test_high_critical_language_findings_block_for_all_analyzers(self):
        for severity in ('HIGH', 'CRITICAL'):
            for analyzer in ('python-pkg', 'sbom'):
                for fixed in ('', '2.0'):
                    with self.subTest(severity=severity, analyzer=analyzer, fixed=fixed):
                        data = report()
                        data['Results'][1]['Packages'].append({'Name': 'example', 'Version': '1', 'AnalyzedBy': analyzer})
                        data['Results'][1]['Vulnerabilities'] = [
                            {'VulnerabilityID': 'CVE-example', 'PkgName': 'example', 'Severity': severity, 'FixedVersion': fixed}]
                        self.assertFalse(security.verify(data)['passed'])

    def test_fixed_os_findings_block_and_unfixed_remain_explicit(self):
        for severity in ('HIGH', 'CRITICAL'):
            for fixed in ('', '2.0'):
                with self.subTest(severity=severity, fixed=fixed):
                    data = report()
                    data['Results'][0]['Vulnerabilities'] = [
                        {'VulnerabilityID': 'CVE-example', 'PkgName': 'example', 'Severity': severity, 'FixedVersion': fixed}]
                    result = security.verify(data)
                    self.assertEqual(result['passed'], not bool(fixed))
                    self.assertEqual(result['all_severity_counts'], {severity: 1})
                    self.assertEqual(len(result['unfixed_os_high_critical']), int(not fixed))

    def test_installer_presence_blocks_even_without_vendor_sbom(self):
        data = report()
        data['Results'][1]['Packages'].append({'Name': 'pip', 'Version': '25.0.1'})
        self.assertFalse(security.verify(data)['passed'])

    def test_all_exact_native_fixes_are_required(self):
        for index in range(3):
            with self.subTest(index=index):
                data = report(); data['Results'][0]['Packages'][index]['Release'] = 'old'
                self.assertFalse(security.verify(data)['passed'])
                data = report(); data['Results'][0]['Packages'].pop(index)
                self.assertFalse(security.verify(data)['passed'])
                data = report(); data['Results'][0]['Packages'].append(copy.deepcopy(data['Results'][0]['Packages'][index]))
                self.assertFalse(security.verify(data)['passed'])

    def test_incomplete_evidence_fails_closed(self):
        mutations = [lambda x: x.pop('Results'), lambda x: x.update(Results=[]),
                     lambda x: x['Results'].pop(), lambda x: x['Results'][1].pop('Packages'),
                     lambda x: x['Results'][1].update(Vulnerabilities=None),
                     lambda x: x['Results'][1].update(Vulnerabilities=[{'Severity': 'HIGH'}])]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data = report(); mutation(data)
                with self.assertRaises(ValueError): security.verify(data)
