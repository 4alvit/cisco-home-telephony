import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco


def inventory():
    rooms = [('201', 'Example Room A'), ('203', 'Example Room B'), ('205', 'Example Room C'), ('207', 'Example Room D'),
             ('210', 'Example Room E'), ('212', 'Example Room F'), ('214', 'Example Room G'),
             ('216', 'Example Room H'), ('218', 'Example Room I'), ('220', 'Example Room J')]
    return {'ami_fresh': True, 'observed_at': time.time(),
            'dial_allowlist': ['209', '600'], 'ring_allowlist': ['640'],
            'phones': [{'id': primary, 'lines': [primary, str(int(primary) + 1)], 'room': room,
                        'online': True, 'dnd': False, 'dnd_pending': False, 'autoanswer': False,
                        'channels': [], 'alert': None, 'capabilities': list(cisco.COMMANDS)}
                       for primary, room in rooms],
            'softphones': [{'extension': '209', 'room': "Operator's Mac", 'online': True}]}


class DirectoryTests(unittest.TestCase):
    def invoke(self, args, data=None, *, human=False):
        data = inventory() if data is None else data
        calls = []

        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                calls.append((path, body))
                if body is None:
                    return data
                return {'status': 'accepted', 'action_id': body['idempotency_key']}

        with patch.object(cisco, 'Gateway', Gateway), \
                patch.object(cisco, 'load_config', return_value={}), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            result = cisco.main(args if human else ['--json', *args])
        return result, output.getvalue() if human else json.loads(output.getvalue()), calls

    def test_global_directory_numeric_order_and_safe_softphone_fields(self):
        data = inventory()
        data['phones'].reverse()
        data['phones'][0]['password'] = 'must-not-print'
        data['softphones'][0]['token'] = 'must-not-print'
        data['secret'] = 'must-not-print'
        code, value, calls = self.invoke(['directory'], data)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones', None)])
        self.assertEqual([p['id'] for p in value['directory']],
                         ['201', '203', '205', '207', '209', '210', '212', '214', '216', '218', '220'])
        self.assertFalse(any(p['selected'] for p in value['directory']))
        self.assertNotIn('must-not-print', json.dumps(value))
        mac = next(p for p in value['directory'] if p['id'] == '209')
        self.assertEqual(mac, {'type': 'softphone', 'id': '209', 'lines': ['209'], 'room': "Operator's Mac",
                               'online': True, 'selected': False})

    def test_selected_secondary_line_highlights_only_its_host(self):
        code, value, calls = self.invoke(['213', 'directory'])
        self.assertEqual(code, 0)
        self.assertEqual(value['selected_line'], '213')
        self.assertEqual([p['id'] for p in value['directory'] if p['selected']], ['212'])
        self.assertEqual(len(calls), 1)

    def test_softphone_can_be_highlighted_but_never_controlled(self):
        self.assertEqual(self.invoke(['209', 'directory'])[0], 0)
        for args in (['209'], ['209', 'dial', '600'], ['209', 'dnd', 'on']):
            with self.subTest(args=args):
                code, value, calls = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertEqual(value['reason'], 'line_not_in_cisco_gateway_inventory')
                self.assertEqual(len(calls), 1)

    def test_room_is_shown_in_status_and_does_not_change_dial_line(self):
        code, value, _ = self.invoke(['213'])
        self.assertEqual(code, 0)
        self.assertEqual(value['phone']['room'], 'Example Room F')
        code, _, calls = self.invoke(['213', '600'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[1][0], '/v1/phones/212/dial')
        self.assertEqual(calls[1][1]['line'], '213')

    def test_missing_room_and_softphone_fields_are_backward_compatible(self):
        data = inventory(); del data['softphones']
        for phone in data['phones']:
            del phone['room']
        code, value, _ = self.invoke(['directory'], data)
        self.assertEqual(code, 0)
        self.assertTrue(all(p['room'] is None for p in value['directory']))
        code, value, _ = self.invoke(['213'], data)
        self.assertEqual(code, 0); self.assertIsNone(value['phone']['room'])

    def test_room_boundaries_and_ascii_punctuation(self):
        for room in (None, 'A', 'a' * 48, "Operator's Mac", 'Room_2-West', 'Room (West)'):
            data = inventory(); data['phones'][0]['room'] = room
            self.assertEqual(self.invoke(['directory'], data)[0], 0)
        for room in ('', ' ', ' leading', 'trailing ', 'a' * 49, 'Line\nBreak', '\x1b[0m', 'Café', 'Room/2',
                     '-West', '_West', "'West", 3, ['Room']):
            data = inventory(); data['phones'][0]['room'] = room
            with self.subTest(room=room):
                code, value, calls = self.invoke(['directory'], data)
                self.assertEqual(code, 1); self.assertEqual(value['reason'], 'invalid_phone_room')
                self.assertEqual(len(calls), 1)

    def test_all_phone_status_and_channel_data_are_validated(self):
        variants = []
        data = inventory(); data['phones'][-1]['online'] = 'yes'; variants.append(data)
        data = inventory(); data['phones'][-1]['channels'] = [{'line': '212'}]; variants.append(data)
        data = inventory(); data['phones'][-1]['alert'] = {'id': 'secret'}; variants.append(data)
        data = inventory(); data['phones'][-1]['capabilities'] = [42]; variants.append(data)
        for data in variants:
            for args in (['directory'], ['213', 'directory'], ['213', 'dial', '600']):
                code, _, calls = self.invoke(args, data)
                self.assertEqual(code, 1); self.assertEqual(len(calls), 1)

    def test_duplicate_and_cross_type_line_mappings_fail(self):
        variants = []
        data = inventory(); data['phones'][-1]['lines'][1] = '213'; variants.append(data)
        data = inventory(); data['softphones'][0]['extension'] = '213'; variants.append(data)
        data = inventory(); data['softphones'].append(copy.deepcopy(data['softphones'][0])); variants.append(data)
        data = inventory(); data['phones'][-1]['id'] = '221'; variants.append(data)
        for data in variants:
            self.assertEqual(self.invoke(['directory'], data)[0], 1)

    def test_all_softphone_fields_are_validated(self):
        variants = []
        data = inventory(); data['softphones'] = None; variants.append(data)
        data = inventory(); data['softphones'][0]['extension'] = '600'; variants.append(data)
        data = inventory(); data['softphones'][0]['online'] = 1; variants.append(data)
        data = inventory(); data['softphones'][0]['room'] = 'bad\nname'; variants.append(data)
        for data in variants:
            self.assertEqual(self.invoke(['directory'], data)[0], 1)

    def test_softphone_inventory_accepts_fifty_but_not_fifty_one(self):
        data = inventory()
        data['softphones'] = [{'extension': str(number), 'room': f'Desk {number}', 'online': False}
                             for number in range(240, 290)]
        code, result, calls = self.invoke(['directory'], data)
        self.assertEqual(code, 0)
        self.assertEqual(sum(p['type'] == 'softphone' for p in result['directory']), 50)
        self.assertEqual(calls, [('/v1/phones', None)])
        data['softphones'].append({'extension': '290', 'room': 'Desk 290', 'online': False})
        code, result, calls = self.invoke(['directory'], data)
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'invalid_softphone_inventory')
        self.assertEqual(calls, [('/v1/phones', None)])

    def test_directory_requires_fresh_gateway_and_known_selected_line(self):
        self.assertEqual(self.invoke(['299', 'directory'])[0], 1)
        for field, value in (('ami_fresh', False), ('observed_at', time.time()-100)):
            data = inventory(); data[field] = value
            self.assertEqual(self.invoke(['directory'], data)[0], 1)

    def test_human_directory_status_and_global_options(self):
        code, output, _ = self.invoke(['213', 'directory'], human=True)
        self.assertEqual(code, 0)
        self.assertIn('* 212 / 213: Example Room F', output)
        self.assertIn("209: Operator's Mac", output)
        self.assertIn('softphone (directory only; no Cisco controls)', output)
        self.assertIn('Room: Example Room F', self.invoke(['213'], human=True)[1])
        for args in (['--timeout', '5', 'directory'], ['directory', '--json', '--timeout=5'],
                     ['--dry-run', '213', 'directory']):
            code, _, calls = self.invoke(args)
            self.assertEqual(code, 0); self.assertEqual(calls, [('/v1/phones', None)])


if __name__ == '__main__':
    unittest.main()
