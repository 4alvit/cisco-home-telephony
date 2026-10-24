"""The richer player must control only its own exact, freshly observed call."""
import copy
from contextlib import contextmanager
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

    @contextmanager
    def reservation_ami(self):
        """Stateful AstDB model, including an explicitly lost delete reply."""
        state = {'value': None, 'fail': None, 'get': 0, 'delete': 0, 'put': 0}
        original = PlayerAMI.action

        def action(ami, name, **values):
            if name not in ('DBGet', 'DBDel', 'DBPut'):
                return original(ami, name, **values)
            PlayerAMI.calls.append((name, values))
            state[{'DBGet': 'get', 'DBDel': 'delete', 'DBPut': 'put'}[name]] += 1
            if name == 'DBDel' and state.pop('delete_then_fail', False):
                state['value'] = None  # remote mutation preceded the lost reply
                error = gateway.Refused('ami_error', 503)
                error.ami_action, error.ami_error = name, 'unexpected_error'
                raise error
            if name == state['fail']:
                error = gateway.Refused('ami_error', 503)
                error.ami_action, error.ami_error = name, 'unexpected_error'
                raise error
            if name == 'DBGet' and 'get_rows' in state:
                return state['get_rows']
            if name == 'DBPut':
                state['value'] = values['Val']
            elif name == 'DBDel':
                state['value'] = None
            elif state['value'] is None:
                error = gateway.Refused('ami_error', 503)
                error.ami_action, error.ami_error = name, 'database_entry_not_found'
                raise error
            else:
                return [{'Response': 'Success', 'EventList': 'start'},
                        {'Event': 'DBGetResponse', 'Family': 'cisco-media',
                         'Key': values['Key'], 'Val': state['value']},
                        {'Event': 'DBGetComplete', 'EventList': 'Complete', 'ListItems': '1'}]
            return [{'Response': 'Success'}]

        with patch.object(PlayerAMI, 'action', action):
            yield state

    def reservation_rows(self, player=None):
        player = player or self.server.player
        return player.gateway.db.execute(
            'SELECT phone,line,value FROM player_reservations ORDER BY phone').fetchall()

    def observe_player(self, player=None):
        player = player or self.server.player
        with PlayerAMI({}) as ami:
            return player.observe(self.server.phones['201'], ami, PlayerAMI.channels)

    def test_expired_cleanup_failures_keep_exact_owner_and_block_sibling_start(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            for failure in ('DBGet', 'DBDel'):
                with self.subTest(failure=failure):
                    result = self.request('play', source='https://example.org/a.mp3')
                    session = player.sessions['201']['session']
                    expected = ('201', '201', '23456:' + result['session_id'])
                    self.assertEqual(self.reservation_rows(), [expected])
                    state['fail'] = failure
                    now.return_value += 46
                    self.observe_player()
                    self.assertTrue(session.closed)
                    self.assertNotIn('201', player.sessions)
                    pending = player.pending_cleanup['201']
                    self.assertEqual((pending['line'], pending['value']), expected[1:])
                    self.assertEqual(self.reservation_rows(), [expected])
                    before = (state['get'], state['delete'], state['put'], self.send.call_count)
                    for line in ('201', '202'):
                        with self.assertRaises(gateway.Refused) as caught:
                            self.request('play', line=line, source='https://example.org/b.mp3')
                        self.assertEqual(caught.exception.reason, 'player_cleanup_pending')
                    self.assertEqual((state['get'], state['delete'], state['put'], self.send.call_count), before)
                    status = self.server.listing()['phones'][0]['player']
                    self.assertEqual((status['state'], status['reason']), ('blocked', 'cleanup_pending'))
                    self.assertIsNone(status['session_id'])
                    state['fail'] = None
                    now.return_value = pending['next_retry']
                    self.observe_player()
                    self.assertNotIn('201', player.pending_cleanup)
                    self.assertEqual(self.reservation_rows(), [])
                    self.assertIsNone(state['value'])

    def test_pending_cleanup_retry_is_due_once_and_backoff_is_capped(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            self.request('play', source='https://example.org/a.mp3')
            state['fail'] = 'DBGet'
            now.return_value = 146.
            self.observe_player()
            gaps = []
            for _ in range(8):
                pending = player.pending_cleanup['201']
                due = pending['next_retry']
                gap = due - now.return_value
                gaps.append(gap)
                self.assertGreaterEqual(gap, 2.)
                self.assertLessEqual(gap, 60.)
                before = state['get']
                now.return_value = due - .001
                for _ in range(3):
                    self.observe_player()
                self.assertEqual(state['get'], before)
                now.return_value = due
                self.observe_player()
                self.assertEqual(state['get'], before + 1)
                self.assertEqual(len(self.reservation_rows()), 1)
                self.assertEqual(len(player.pending_cleanup), 1)
            self.assertEqual(gaps[0], 2.)
            self.assertEqual(gaps[-1], 60.)

    def test_failed_dispatch_keeps_pending_owner_and_original_error(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            for failure in ('DBGet', 'DBDel'):
                with self.subTest(failure=failure):
                    state['fail'] = failure
                    error = gateway.Refused('phone_did_not_confirm_action', 502)
                    self.send.side_effect = error
                    with patch.object(Session, 'close', autospec=True) as close:
                        with self.assertRaises(gateway.Refused) as caught:
                            self.request('play', source='https://example.org/a.mp3')
                        self.assertIs(caught.exception, error)
                        close.assert_called_once()
                    self.assertNotIn('201', player.sessions)
                    pending = player.pending_cleanup['201']
                    self.assertEqual(state['value'], pending['value'])
                    self.assertEqual(self.reservation_rows(), [('201', '201', pending['value'])])
                    state['fail'], self.send.side_effect = None, None
                    now.return_value = pending['next_retry']
                    self.observe_player()
                    self.assertEqual(self.reservation_rows(), [])

    def test_unobserved_stop_retains_cleanup_owner_until_missing_or_exact_retry(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            for outcome in ('exact', 'missing', 'successor'):
                with self.subTest(outcome=outcome):
                    result = self.request('play', source='https://example.org/a.mp3')
                    session = player.sessions['201']['session']
                    state['fail'] = 'DBDel'
                    self.request('stop', result['session_id'])
                    self.assertTrue(session.closed)
                    self.assertNotIn('201', player.sessions)
                    pending = player.pending_cleanup['201']
                    self.assertEqual(len(self.reservation_rows()), 1)
                    self.assertFalse(any(name == 'Hangup' for name, _ in PlayerAMI.calls))
                    state['fail'] = None
                    if outcome == 'missing':
                        state['value'] = None
                    elif outcome == 'successor':
                        state['value'] = '24567:00000000-0000-4000-8000-000000000099'
                    expected_value = state['value'] if outcome == 'successor' else None
                    before_delete = state['delete']
                    now.return_value = pending['next_retry']
                    self.observe_player()
                    self.assertNotIn('201', player.pending_cleanup)
                    self.assertEqual(self.reservation_rows(), [])
                    self.assertEqual(state['value'], expected_value)
                    self.assertEqual(state['delete'] - before_delete, int(outcome == 'exact'))

    def test_restart_recovers_only_pending_reservation_not_playback(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            self.request('play', source='https://example.org/a.mp3')
            state['fail'] = 'DBGet'
            now.return_value = 146.
            self.observe_player()
            expected = self.reservation_rows()
            replacement = gateway.Gateway(copy.deepcopy(self.server.config), self.directory.name)
            self.addCleanup(replacement.db.close)
            replacement.dnd_pending.clear()
            restored = replacement.player
            self.assertEqual(restored.sessions, {})
            self.assertEqual(self.reservation_rows(restored), expected)
            self.assertEqual(restored.pending_cleanup['201']['value'], expected[0][2])
            before_send, before_put = self.send.call_count, state['put']
            with self.assertRaises(gateway.Refused) as caught:
                replacement.mutate('201', 'player_play', dict(line='202', session_id=None,
                    source='https://example.org/b.mp3', idempotency_key='restart-cleanup-block'))
            self.assertEqual(caught.exception.reason, 'player_cleanup_pending')
            self.assertEqual((self.send.call_count, state['put']), (before_send, before_put))
            state['fail'] = None
            now.return_value = restored.pending_cleanup['201']['next_retry']
            self.observe_player(restored)
            self.assertEqual(restored.pending_cleanup, {})
            self.assertEqual(self.reservation_rows(restored), [])
            self.assertIsNone(state['value'])

    def test_malformed_completed_dbget_keeps_pending_owner(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            self.request('play', source='https://example.org/a.mp3')
            expected = self.reservation_rows()
            exact = {'Event': 'DBGetResponse', 'Family': 'cisco-media',
                     'Key': '201', 'Val': state['value']}
            malformed = [[], [dict(exact, Family='other')], [dict(exact, Key='202')],
                         [dict(exact, Val=None)], [dict(exact, Val=42)], [exact, exact]]
            now.return_value = 146.
            for entries in malformed:
                with self.subTest(entries=entries):
                    state['get_rows'] = [{'Response': 'Success', 'EventList': 'start'}, *entries,
                        {'Event': 'DBGetComplete', 'EventList': 'Complete', 'ListItems': str(len(entries))}]
                    if player.pending_cleanup:
                        now.return_value = player.pending_cleanup['201']['next_retry']
                    self.observe_player()
                    self.assertEqual(self.reservation_rows(), expected)
                    self.assertIn('201', player.pending_cleanup)
                    self.assertEqual(state['delete'], 0)
            del state['get_rows']
            now.return_value = player.pending_cleanup['201']['next_retry']
            self.observe_player()
            self.assertEqual(player.pending_cleanup, {})
            self.assertEqual(self.reservation_rows(), [])
            self.assertEqual(state['delete'], 1)

    def test_corrupt_restored_owner_cannot_issue_ami_or_break_observation(self):
        value = '23456:00000000-0000-4000-8000-000000000001'
        for line, stored in [('999', value), ('201', 'not-a-reservation'), ('201', b'not-text')]:
            with self.subTest(line=line, stored=stored), self.reservation_ami() as state:
                self.server.db.execute('DELETE FROM player_reservations')
                self.server.db.execute('INSERT INTO player_reservations VALUES (?,?,?)', ('201', line, stored))
                self.server.db.commit()
                replacement = gateway.Gateway(copy.deepcopy(self.server.config), self.directory.name)
                self.addCleanup(replacement.db.close)
                restored = replacement.player
                self.observe_player(restored)
                self.assertIn('201', restored.pending_cleanup)
                self.assertEqual(self.reservation_rows(restored), [('201', line, stored)])
                self.assertEqual((state['get'], state['delete'], state['put']), (0, 0, 0))
                self.send.assert_not_called()

    def test_ambiguous_delete_and_cached_duplicates_never_dispatch_again(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            body = dict(line='201', session_id=None, source='https://example.org/a.mp3',
                        idempotency_key='cached-player-start')
            started = self.server.mutate('201', 'player_play', body)
            before = (state['put'], self.send.call_count)
            self.assertEqual(self.server.mutate('201', 'player_play', body), started)
            self.assertEqual((state['put'], self.send.call_count), before)
            state['delete_then_fail'] = True
            stop = dict(line='201', session_id=started['session_id'], idempotency_key='cached-player-stop')
            stopped = self.server.mutate('201', 'player_stop', stop)
            self.assertIsNone(state['value'])
            self.assertIn('201', player.pending_cleanup)
            self.assertEqual(len(self.reservation_rows()), 1)
            counts = (state['get'], state['delete'], state['put'], self.send.call_count)
            self.assertEqual(self.server.mutate('201', 'player_stop', stop), stopped)
            self.assertEqual((state['get'], state['delete'], state['put'], self.send.call_count), counts)
            now.return_value = player.pending_cleanup['201']['next_retry']
            self.observe_player()
            self.assertEqual(player.pending_cleanup, {})
            self.assertEqual(self.reservation_rows(), [])
            self.assertEqual(state['delete'], 1)
            self.assertEqual(state['put'], 1)
            self.assertEqual(self.send.call_count, 2)

    def test_one_pending_phone_does_not_block_another_physical_phone(self):
        config_value = copy.deepcopy(self.server.config)
        other = copy.deepcopy(config_value['phones'][0])
        other.update(id='203', lines=['203', '204'], ipv4='192.168.50.23', port=18081,
                     device='SEP001122334466', auth_path='/auth/' + 'd' * 64)
        config_value['phones'].append(other)
        self.server = gateway.Gateway(config_value, self.directory.name)
        self.addCleanup(self.server.db.close)
        self.server.dnd_pending.clear()
        PlayerAMI.endpoints.update({'203': 'Not in use', '204': 'Not in use'})
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(player, 'runtime', return_value=Runtime), \
             patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            self.request('play', source='https://example.org/a.mp3')
            original_value = state['value']
            state['fail'] = 'DBGet'
            now.return_value = 146.
            self.observe_player()
            first_pending = dict(player.pending_cleanup['201'])
            original = PlayerAMI.action
            other_writes = []

            def action(ami, name, **values):
                if name == 'DBPut' and values.get('Key') == '203':
                    PlayerAMI.calls.append((name, values))
                    other_writes.append(values['Val'])
                    return [{'Response': 'Success'}]
                return original(ami, name, **values)

            with patch.object(PlayerAMI, 'action', action):
                result = self.server.mutate('203', 'player_play', dict(line='203', session_id=None,
                    source='https://example.org/b.mp3', idempotency_key='second-physical-player'))
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(set(player.sessions), {'203'})
            self.assertEqual(player.pending_cleanup, {'201': first_pending})
            self.assertEqual(state['value'], original_value)
            self.assertEqual(other_writes, ['23456:' + result['session_id']])
            self.assertEqual(len(self.reservation_rows()), 2)
            self.assertEqual(self.send.call_count, 4)

    def test_observed_consumption_retires_durable_ledger_without_db_delete(self):
        with self.reservation_ami() as state:
            result = self.request('play', source='https://example.org/a.mp3')
            self.assertEqual(len(self.reservation_rows()), 1)
            PlayerAMI.channels = [dict(Channel='PJSIP/201-abc', Uniqueid='123.4',
                Linkedid='123.4', Context='home', Exten='699', ChannelStateDesc='Up', Application='Dial')]
            PlayerAMI.marker = '1:' + result['session_id'] + ':123.4'
            state['value'] = None  # the actual dialplan consumed the entry
            self.observe_player()
            self.assertEqual(self.reservation_rows(), [])
            self.assertIn('201', self.server.player.sessions)
            self.assertEqual(state['delete'], 0)

    def test_pending_cleanup_refusal_allows_same_key_only_after_cleanup(self):
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            self.request('play', source='https://example.org/a.mp3')
            state['fail'] = 'DBGet'
            now.return_value = 146.
            self.observe_player()
            body = dict(line='202', session_id=None, source='https://example.org/b.mp3',
                        idempotency_key='retry-after-confirmed-cleanup')
            before = (state['get'], state['delete'], state['put'], self.send.call_count)
            for _ in range(2):
                with self.assertRaises(gateway.Refused) as caught:
                    self.server.mutate('201', 'player_play', body)
                self.assertEqual((caught.exception.reason, caught.exception.status),
                                 ('player_cleanup_pending', 409))
                self.assertEqual(self.server.db.execute(
                    'SELECT response FROM operations WHERE key=?',
                    (body['idempotency_key'],)).fetchall(), [])
            self.assertEqual((state['get'], state['delete'], state['put'], self.send.call_count), before)
            state['fail'] = None
            now.return_value = player.pending_cleanup['201']['next_retry']
            self.observe_player()
            self.assertEqual(self.reservation_rows(), [])
            result = self.server.mutate('201', 'player_play', body)
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(state['put'], before[2] + 1)
            self.assertEqual(self.send.call_count, before[3] + 2)
            self.assertEqual(self.server.mutate('201', 'player_play', body), result)
            self.assertEqual(state['put'], before[2] + 1)
            self.assertEqual(self.send.call_count, before[3] + 2)

    def test_one_cleanup_transaction_per_connection_without_starving_second_phone(self):
        config_value = copy.deepcopy(self.server.config)
        other = copy.deepcopy(config_value['phones'][0])
        other.update(id='203', lines=['203', '204'], ipv4='192.168.50.23', port=18081,
                     device='SEP001122334466', auth_path='/auth/' + 'd' * 64)
        config_value['phones'].append(other)
        self.server = gateway.Gateway(config_value, self.directory.name)
        self.addCleanup(self.server.db.close)
        self.server.dnd_pending.clear()
        PlayerAMI.endpoints.update({'203': 'Not in use', '204': 'Not in use'})
        player = self.server.player
        clock = gateway.app_module('phone_player').time
        with patch.object(player, 'runtime', return_value=Runtime), \
             patch.object(clock, 'monotonic', return_value=100.) as now, self.reservation_ami() as state:
            for phone in ('201', '203'):
                self.server.mutate(phone, 'player_play', dict(line=phone, session_id=None,
                    source='https://example.org/a.mp3', idempotency_key='start-bounded-' + phone))
            state['fail'] = 'DBGet'
            # Reuse one connection across all phones, exactly like a background sweep.
            for at, expected_key in ((146., '201'), (146., '203'), (1000., '201'), (2000., '203')):
                now.return_value = at
                before = state['get']
                with PlayerAMI({}) as ami:
                    for phone in ('201', '203'):
                        player.observe(self.server.phones[phone], ami, [])
                    for phone in ('201', '203'):
                        player.observe(self.server.phones[phone], ami, [])
                self.assertEqual(state['get'], before + 1)
                self.assertEqual([values['Key'] for name, values in PlayerAMI.calls
                                  if name == 'DBGet'][-1], expected_key)
                self.assertEqual(set(player.pending_cleanup), {'201', '203'})
                self.assertEqual(len(self.reservation_rows()), 2)
                self.assertEqual(player.sessions, {})
            self.assertEqual((state['delete'], state['put'], self.send.call_count), (0, 2, 4))

    def test_options_reject_unsafe_values(self):
        for value in ({'enabled': 1}, {'max_calls': 100}, {'allowed_origins': ['file:///tmp']},
                      {'allowed_origins': ['http://example.org/path']}, {'max_seconds': 999999}):
            bad = copy.deepcopy(config()); bad['media_player'] = value
            with self.subTest(value=value), self.assertRaises(gateway.Refused):
                gateway.validate(bad)


if __name__ == '__main__':
    unittest.main()
