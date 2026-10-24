"""The richer player must control only its own exact, freshly observed call."""
import copy
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_phone_gateway import config, gateway, FakeAMI

REAL_AMI = gateway.AMI


class PlayerAMI(FakeAMI):
    marker = ''
    supported = True
    reservation = ''

    def action(self, name, **values):
        self.calls.append((name, values))
        if name == 'DBGet':
            assert values['complete'] == 'DBGetComplete' and values['Family'] == 'cisco-media'
            assert values['Key'] in ('201', '202')
            return [{'Response': 'Success', 'EventList': 'start'},
                    {'Event': 'DBGetResponse', 'Family': values['Family'], 'Key': values['Key'],
                     'Val': self.reservation},
                    {'Event': 'DBGetComplete', 'EventList': 'Complete', 'ListItems': '1'}]
        if name == 'Getvar':
            variable = values['Variable']
            assert not variable.startswith('DB('), 'Do not bypass hardened PBX function policy'
            value = ('1' if self.supported else '0') if variable.startswith('DIALPLAN_EXISTS') else self.marker
            return [{'Response': 'Success', 'Variable': variable, 'Value': value}]
        return [{'Response': 'Success'}]


class Session:
    def __init__(self, token, source, **kwargs):
        self.token, self.source, self.port, self.closed = token, source, 23456, False
        self.commands = []
    def start(self): pass
    def close(self): self.closed = True
    def control(self, action, **values): self.commands.append((action, values))
    def snapshot(self):
        return {'state': 'playing', 'source_id': 'https://example.org/song.mp3?token=secret',
                'position': 3.5, 'duration': 60, 'seekable': True, 'error': None}


class Runtime:
    MediaSession = Session
    @staticmethod
    def validate_source(value, _origins):
        if not (value.get('url', '').startswith('https://') or value.get('station')):
            raise ValueError('bad URL')
        return value


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        PlayerAMI.calls, PlayerAMI.channels = [], []
        PlayerAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        PlayerAMI.marker, PlayerAMI.supported = '', True
        PlayerAMI.reservation = ''
        self.ami = patch.object(gateway, 'AMI', PlayerAMI)
        self.ami.start(); self.addCleanup(self.ami.stop)
        self.execute = patch.object(gateway, 'execute')
        self.send = self.execute.start(); self.addCleanup(self.execute.stop)
        value = config()
        value['media_player'] = {'enabled': True}
        self.server = gateway.Gateway(value, self.directory.name)
        self.server.dnd_pending.clear()
        self.addCleanup(self.server.db.close)
        self.runtime = patch.object(self.server.player, 'runtime', return_value=Runtime)
        self.runtime.start(); self.addCleanup(self.runtime.stop)
        self.counter = 0

    def request(self, action, session_id=None, line='201', **body):
        self.counter += 1
        return self.server.mutate('201', 'player_' + action, dict(
            idempotency_key=f'player-test-{self.counter:04}', line=line,
            session_id=session_id, **body))

    def start(self, line='201'):
        result = self.request('play', line=line, source='https://example.org/music.mp3')
        token = result['session_id']
        channel = dict(Channel=f'PJSIP/{line}-abc', Uniqueid='123.4', Linkedid='123.4',
                       Context='home', Exten='699', ChannelStateDesc='Up', Application='Dial')
        PlayerAMI.channels = [channel]
        PlayerAMI.marker = f'1:{token}:123.4'
        PlayerAMI.endpoints[line] = 'In use'
        return token

    def test_start_reserves_only_port_token_and_uses_selected_line(self):
        token = self.start('202')
        writes = [v for n, v in PlayerAMI.calls if n == 'DBPut']
        self.assertEqual(writes, [{'Family': 'cisco-media', 'Key': '202', 'Val': '23456:' + token}])
        self.assertEqual([x.args[1] for x in self.send.call_args_list], ['Key:Line2', 'Dial:699'])
        self.assertNotIn('example.org', str(PlayerAMI.calls))

    def test_fresh_exact_identity_controls_existing_socket(self):
        token = self.start('202')
        self.request('pause', token, line='202')
        self.request('seek', token, line='202', position=10.)
        self.request('play', token, line='202', source='https://example.org/next.mp3', enqueue='add')
        session = self.server.player.sessions['201']['session']
        self.assertIn(('pause', {}), session.commands)
        self.assertIn(('seek', {'position': 10.}), session.commands)
        self.assertEqual(self.send.call_count, 2)

    def test_selected_line_change_cannot_redirect_transport(self):
        token = self.start('202')
        with self.assertRaises(gateway.Refused):
            self.request('stop', token, line='201')
        self.assertFalse(any(n == 'Hangup' for n, _ in PlayerAMI.calls))

    def test_replacement_call_cannot_be_stopped(self):
        token = self.start()
        self.request('pause', token)
        PlayerAMI.channels[0]['Uniqueid'] = '999.8'
        PlayerAMI.marker = f'1:{token}:999.8'
        with self.assertRaises(gateway.Refused):
            self.request('stop', token)
        self.assertFalse(any(n == 'Hangup' for n, _ in PlayerAMI.calls))

    def test_wrong_marker_never_binds(self):
        token = self.start()
        PlayerAMI.marker = '1:wrong:123.4'
        with self.assertRaises(gateway.Refused):
            self.request('pause', token)

    def test_wrong_session_cannot_control(self):
        self.start()
        with self.assertRaises(gateway.Refused):
            self.request('stop', '00000000-0000-0000-0000-000000000000')

    def test_stop_uses_exact_uid_not_recyclable_name(self):
        token = self.start()
        self.request('stop', token)
        self.assertEqual([v for n, v in PlayerAMI.calls if n == 'Hangup'], [{'Channel': '123.4', 'Cause': '16'}])

    def test_stop_succeeds_if_socket_close_already_ended_exact_call(self):
        token = self.start()
        original = PlayerAMI.action
        def action(ami, name, **values):
            if name == 'Hangup':
                error = gateway.Refused('ami_error', 503)
                error.ami_error = 'no_such_channel'
                raise error
            return original(ami, name, **values)
        with patch.object(PlayerAMI, 'action', action):
            self.assertEqual(self.request('stop', token)['status'], 'accepted')
        self.assertNotIn('201', self.server.player.sessions)

    def test_busy_dnd_offline_or_missing_route_refuses_start(self):
        for reason in ('busy', 'offline', 'dnd', 'route'):
            with self.subTest(reason=reason):
                PlayerAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
                PlayerAMI.supported = reason != 'route'
                self.server.db.execute('UPDATE preferences SET dnd=?', (int(reason == 'dnd'),))
                if reason in ('busy', 'offline'):
                    PlayerAMI.endpoints['202'] = 'In use' if reason == 'busy' else 'Unavailable'
                with self.assertRaises(gateway.Refused):
                    self.request('play', source='https://example.org/a.mp3')
        self.send.assert_not_called()

    def test_persist_idle_volume_and_mute_without_phone_io(self):
        self.request('volume', volume_level=.2)
        self.request('mute', is_volume_muted=True)
        self.assertEqual(self.server.player.preferences('201'), {'volume_level': .2, 'is_volume_muted': True})
        self.send.assert_not_called()

    def test_source_and_numeric_control_rejection_precedes_dispatch(self):
        for action, body in [('play', {'source': 'file:///etc/passwd'}),
                             ('play', {'source': 'https://example.org/a', 'metadata': {'password': 'no'}}),
                             ('volume', {'volume_level': float('nan')}),
                             ('volume', {'volume_level': True}), ('seek', {'position': -1}),
                             ('mute', {'is_volume_muted': 1})]:
            with self.subTest(action=action, body=body), self.assertRaises(gateway.Refused):
                self.request(action, **body)
        self.send.assert_not_called()

    def test_cleanup_only_removes_the_same_unconsumed_reservation(self):
        token = self.start()
        record = self.server.player.sessions['201']
        PlayerAMI.reservation = '23456:' + token
        with PlayerAMI({}) as ami:
            self.server.player.release_reservation(record, ami)
            self.assertIn(('DBDel', {'Family': 'cisco-media', 'Key': '201'}), PlayerAMI.calls)
            PlayerAMI.calls.clear()
            PlayerAMI.reservation = '23456:00000000-0000-0000-0000-000000000000'
            self.server.player.release_reservation(record, ami)
            self.assertFalse(any(n == 'DBDel' for n, _ in PlayerAMI.calls))

    def cleanup_wire(self, record, frames):
        """Exercise the real AMI parser, including the asynchronous DBGet list."""
        ami = REAL_AMI({})
        ami.sock = Mock()
        ami.file = io.BytesIO(''.join(''.join(f'{key}: {value}\r\n' for key, value in frame.items())
                                    + '\r\n' for frame in frames).encode())
        with patch.object(gateway.secrets, 'token_hex', side_effect=['read', 'delete']):
            self.server.player.release_reservation(record, ami)
        return [call.args[0].decode() for call in ami.sock.sendall.call_args_list]

    def test_cleanup_waits_for_dbget_complete_and_ignores_other_action(self):
        token = self.start()
        record = self.server.player.sessions['201']
        frames = [
            {'Response': 'Success', 'ActionID': 'read', 'EventList': 'start'},
            {'Event': 'DBGetResponse', 'ActionID': 'other', 'Family': 'cisco-media',
             'Key': '201', 'Val': 'foreign-reservation'},
            {'Event': 'DBGetComplete', 'ActionID': 'other', 'EventList': 'Complete', 'ListItems': '1'},
            {'Event': 'DBGetResponse', 'ActionID': 'read', 'Family': 'cisco-media',
             'Key': '201', 'Val': '23456:' + token},
            {'Event': 'DBGetComplete', 'ActionID': 'read', 'EventList': 'Complete', 'ListItems': '1'},
            {'Response': 'Success', 'ActionID': 'delete'},
        ]
        sent = self.cleanup_wire(record, frames)
        self.assertEqual(sent, ['Action: DBGet\r\nActionID: read\r\nFamily: cisco-media\r\nKey: 201\r\n\r\n',
                                'Action: DBDel\r\nActionID: delete\r\nFamily: cisco-media\r\nKey: 201\r\n\r\n'])

    def test_cleanup_requires_one_exact_family_key_and_value(self):
        token = self.start()
        record = self.server.player.sessions['201']
        exact = {'Event': 'DBGetResponse', 'ActionID': 'read', 'Family': 'cisco-media',
                 'Key': '201', 'Val': '23456:' + token}
        for entries in ([], [dict(exact, Family='cisco-dnd')], [dict(exact, Key='202')],
                        [dict(exact, Val='23456:replacement')], [exact, exact]):
            with self.subTest(entries=entries):
                sent = self.cleanup_wire(record, [
                    {'Response': 'Success', 'ActionID': 'read', 'EventList': 'start'}, *entries,
                    {'Event': 'DBGetComplete', 'ActionID': 'read', 'EventList': 'Complete',
                     'ListItems': str(len(entries))}])
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0].startswith('Action: DBGet\r\n'))

    def test_cleanup_missing_entry_is_done_but_permission_failure_is_not_hidden(self):
        self.start()
        record = self.server.player.sessions['201']
        sent = self.cleanup_wire(record, [
            {'Response': 'Error', 'ActionID': 'read', 'Message': 'Database entry not found'}])
        self.assertEqual(len(sent), 1)
        for message in ('Permission denied', 'Unexpected failure'):
            with self.subTest(message=message), self.assertRaises(gateway.Refused):
                self.cleanup_wire(record, [{'Response': 'Error', 'ActionID': 'read', 'Message': message}])

    def test_cleanup_rejects_incomplete_list_and_skips_consumed_reservation(self):
        token = self.start()
        record = self.server.player.sessions['201']
        with self.assertRaises(gateway.Refused):
            self.cleanup_wire(record, [
                {'Response': 'Success', 'ActionID': 'read', 'EventList': 'start'},
                {'Event': 'DBGetResponse', 'ActionID': 'read', 'Family': 'cisco-media',
                 'Key': '201', 'Val': '23456:' + token}])
        record['uid'] = '123.4'
        self.assertEqual(self.cleanup_wire(record, []), [])

    def test_status_redacts_signed_url_and_retains_physical_identity(self):
        token = self.start('202')
        data = self.server.listing()['phones'][0]['player']
        self.assertEqual(data['session_id'], token)
        self.assertEqual(data['line'], '202')
        self.assertEqual(data['source_id'], 'https://example.org/song.mp3')
        self.assertNotIn('secret', str(data))

    def test_options_reject_unsafe_values(self):
        for value in ({'enabled': 1}, {'max_calls': 100}, {'allowed_origins': ['file:///tmp']},
                      {'allowed_origins': ['http://example.org/path']}, {'max_seconds': 999999}):
            bad = copy.deepcopy(config()); bad['media_player'] = value
            with self.subTest(value=value), self.assertRaises(gateway.Refused):
                gateway.validate(bad)


if __name__ == '__main__':
    unittest.main()
