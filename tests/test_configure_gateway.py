"""Room metadata migrates without widening the gateway's control surface."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import configure_gateway
finally:
    sys.path.pop(0)


class ConfigureGatewayTests(unittest.TestCase):
    def setUp(self):
        self.callback = {
            'device': 'SEP020000000001', 'auth_bind_ipv4': '192.168.50.10', 'auth_port': 18080,
            'auth_path': '/auth/' + 'c' * 64, 'phone_ipv4': '192.168.50.21',
            'username': 'u' * 24, 'password': 'p' * 24}
        self.site = {'node_ipv4': '192.168.50.10', 'phone_cidrs': ['192.168.50.21/32'],
                     'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'}}
        self.inventory = {'phones': [{
            'model': 'CP-7945G', 'verified': True, 'mac': '02:00:00:00:00:01',
            'lines': [{'extension': '201', 'label': '201'}, {'extension': '202', 'label': '202'}],
            'authentication_url': 'http://192.168.50.10:18080/auth/' + 'c' * 64}]}
        self.ami = {'username': 'gateway', 'secret': 'b' * 48}

    def prepare(self):
        with patch.object(configure_gateway.secrets, 'token_urlsafe', return_value='a' * 48):
            return configure_gateway.prepare(self.site, self.inventory, [self.callback],
                                             self.ami, ['192.168.50.11'])

    def test_absent_rooms_preserve_existing_gateway_shape(self):
        result = self.prepare()
        self.assertNotIn('room', result['phones'][0])
        self.assertNotIn('softphones', result)
        self.site['softphones'] = []
        self.assertEqual(self.prepare(), result)

    def test_rooms_and_softphone_metadata_propagate_without_credential_changes(self):
        original = self.prepare()
        self.inventory['phones'][0]['room'] = 'Living Room'
        self.site['softphones'] = [{'extension': '209', 'label': 'Mac', 'room': "Operator's Mac",
                                   'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]
        before = copy.deepcopy((self.site, self.inventory, self.callback, self.ami))
        result = self.prepare()
        self.assertEqual(result['phones'][0].pop('room'), 'Living Room')
        self.assertEqual(result.pop('softphones'), [{'extension': '209', 'room': "Operator's Mac"}])
        self.assertEqual(result['dial_allowlist'], ['201', '202', '209', '600', '601', '620', '630', '640'])
        result['dial_allowlist'].remove('209')
        self.assertEqual(result, original)
        self.assertEqual((self.site, self.inventory, self.callback, self.ami), before)
        self.site['softphones'][0].pop('room')
        self.assertEqual(self.prepare()['softphones'], [{'extension': '209', 'room': None}])

    def test_invalid_inventory_and_softphone_rooms_are_not_normalized(self):
        for room in (None, '', ' Room', 'Room ', 'Room;value=x', 1):
            for kind in ('phone', 'softphone'):
                with self.subTest(room=room, kind=kind):
                    self.inventory['phones'][0].pop('room', None)
                    self.site.pop('softphones', None)
                    if kind == 'phone':
                        self.inventory['phones'][0]['room'] = room
                    else:
                        self.site['softphones'] = [{'extension': '209', 'label': 'Mac', 'room': room,
                                                   'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]
                    with self.assertRaisesRegex(ValueError, 'room must'): self.prepare()

    def test_softphone_cannot_duplicate_a_physical_secondary_line(self):
        self.site['softphones'] = [{'extension': '202', 'label': 'Mac',
                                   'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]
        with self.assertRaisesRegex(ValueError, 'duplicate extension'): self.prepare()


if __name__ == '__main__':
    unittest.main()
