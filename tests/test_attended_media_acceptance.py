import copy
import importlib.util
import json
import contextlib
import io
import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'scripts/attended_media_acceptance.py'
SPEC = importlib.util.spec_from_file_location('attended_media_acceptance', PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def spec(route='ha'):
    return {'schema_version': 1, 'route': route, 'target': '201',
            'entity_id': 'media_player.example', 'test_id': 'a' * 32,
            'fixture': {'url': 'http://media.example/' + 'a' * 32 + '/speech.mp3',
                        'sha256': 'b' * 64, 'bytes': 100, 'duration_seconds': 3,
                        'title': 'Attended speech'},
            'connections': {'ha': 'http://ha.example', 'gateway': 'http://gateway.example',
                            'ma': 'http://ma.example', 'ma_stream': 'http://stream.example'}}


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class FakeClient:
    def __init__(self, config, clock):
        self.spec, self.clock = config, clock
        self.selected = {'id': config['target'], 'online': True, 'channels': [],
                         'player': {'state': 'idle', 'session_id': None, 'line': None,
                                    'volume_level': .6, 'is_volume_muted': True}}
        self.other = {'id': '203', 'channels': [{'uniqueid': 'unrelated-uid'}],
                      'player': {'session_id': 'unrelated-session'}}
        self.items, self.autoplay, self.started, self.operations = [], True, None, []
        self.foreign_source = False
        self.foreign_queue = False
        self.replace_after_start = False
        self.unrelated_drift = False
        self.start_timeout = False
        self.mutation_failure = None

    def phones(self):
        if self.started is not None:
            if self.replace_after_start and self.clock.now > .1:
                self.selected['player'].update(session_id='replacement-session', source_id='http://foreign.example/a')
                self.selected['channels'] = [{'uniqueid': 'replacement-uid'}]
            elif self.clock.now - self.started >= self.spec['fixture']['duration_seconds']:
                self.finish()
            if self.unrelated_drift and self.clock.now > .1:
                self.other['player']['session_id'] = 'changed-independently'
        return copy.deepcopy([self.selected, self.other])

    def finish(self):
        self.selected['player'].update(state='idle', session_id=None, line=None)
        self.selected['channels'] = []

    def fixture(self):
        pass

    def start(self, source):
        self.started = self.clock.now
        self.selected['player'].update(state='playing', session_id='owned-session',
            source_id='http://foreign.example/a' if self.foreign_source else source,
            line=self.spec['target'], title=self.spec['fixture']['title'])
        self.selected['channels'] = [{'uniqueid': 'owned-uid'}]
        if self.start_timeout:
            raise TimeoutError('response lost')

    def gateway(self, operation, session=None, **values):
        self.operations.append(('gateway', operation, session))
        if operation == self.mutation_failure:
            raise TimeoutError('lost mutation response')
        if session != self.selected['player']['session_id']:
            raise module.Refused('player_session_changed')
        if operation == 'volume':
            self.selected['player']['volume_level'] = values['volume_level']
        elif operation == 'mute':
            self.selected['player']['is_volume_muted'] = values['is_volume_muted']
        elif operation == 'stop':
            self.finish()

    def ha(self, operation, **values):
        self.operations.append(('ha', operation, None))
        if operation == 'play_media':
            self.last_ha_payload = values
        self.start(values['media_content_id'])

    def ma(self, command, **values):
        if command == 'player_queues/get':
            return {'state': 'idle' if self.selected['player']['session_id'] is None else 'playing',
                    'items': len(self.items), 'autoplay_enabled': self.autoplay}
        if command == 'player_queues/items':
            return copy.deepcopy(self.items)
        if command == 'players/get':
            return {'available': True}
        self.operations.append(('ma', command, None))
        if command == 'player_queues/autoplay':
            self.autoplay = values['autoplay_enabled']
        elif command == 'player_queues/play_media':
            self.items = [{'queue_item_id': 'q' * 32,
                           'media_item': {'item_id': self.spec['fixture']['url']}}]
            if self.foreign_queue:
                self.items.append({'queue_item_id': 'foreign', 'media_item': {'item_id': 'other'}})
            entity = self.spec['entity_id']
            self.start(f'http://stream.example/flow/session/{entity}/{"q" * 32}/{entity}.mp3')
        elif command == 'player_queues/delete_item':
            self.items = [item for item in self.items
                          if item['queue_item_id'] != values['item_id_or_index']]
        else:
            raise AssertionError(command)


class AttendedTests(unittest.TestCase):
    def runner(self, route='ha'):
        config, clock = spec(route), Clock()
        clock.now = 0
        client = FakeClient(config, clock)
        saved = []
        run = module.Run(config, client, lambda state: saved.append(copy.deepcopy(state)),
                         clock=clock, sleep=clock.sleep)
        return run, client, clock, saved

    def test_ha_single_speech_restores_preferences_and_keeps_unrelated(self):
        run, client, _, _ = self.runner()
        result = run.perform(40)
        self.assertNotIn('error', result)
        self.assertTrue(all(result['checks'].values()))
        self.assertTrue(all(result['cleanup'].values()))
        self.assertEqual(client.selected['player']['volume_level'], .6)
        self.assertTrue(client.selected['player']['is_volume_muted'])
        self.assertEqual(sum(x[:2] == ('ha', 'play_media') for x in client.operations), 1)
        self.assertEqual(client.last_ha_payload['extra']['metadata'],
                         {'title': 'Attended speech', 'duration': 3})
        self.assertEqual(client.other['player']['session_id'], 'unrelated-session')
        self.assertEqual(result['human_confirmation']['heard_expected_phrase'], 'pending')

    def test_ma_exact_flow_queue_and_autoplay_restore(self):
        run, client, _, _ = self.runner('ma')
        result = run.perform(40)
        self.assertNotIn('error', result)
        self.assertNotIn('error', result['cleanup'])
        self.assertEqual(result['queue_item_id'], 'q' * 32)
        self.assertEqual(client.items, [])
        self.assertTrue(client.autoplay)
        self.assertFalse(any(x[1] == 'player_queues/stop' for x in client.operations))

    def test_replacement_session_is_never_stopped_or_volume_changed(self):
        run, client, _, _ = self.runner()
        client.replace_after_start = True
        result = run.perform(40)
        self.assertEqual(result['error'], 'pending_session_changed')
        self.assertIn('error', result['cleanup'])
        self.assertFalse(any(x[1] == 'stop' for x in client.operations))
        self.assertEqual(sum(x[1] == 'volume' for x in client.operations), 1)

    def test_foreign_origin_or_same_origin_wrong_flow_refused(self):
        config = spec('ma')
        entity = config['entity_id']
        good = f'http://stream.example/flow/token/{entity}/owned/{entity}.mp3'
        self.assertTrue(module.flow_matches(good, config['connections']['ma_stream'], entity, 'owned'))
        for bad in (good.replace('/owned/', '/foreign/'), good.replace('/flow/', '/single/'),
                    good.replace('stream.example', 'foreign.example'), good.replace('/token/', '/../'),
                    good.replace('/owned/', '/owned/extra/')):
            self.assertFalse(module.flow_matches(bad, config['connections']['ma_stream'], entity, 'owned'))

    def test_foreign_queue_is_not_cleared(self):
        run, client, _, _ = self.runner('ma')
        client.foreign_queue = True
        result = run.perform(40)
        self.assertEqual(result['error'], 'foreign_queue_item')
        self.assertIn('error', result['cleanup'])
        self.assertFalse(any(x[1] in ('stop', 'player_queues/delete_item') for x in client.operations))

    def test_start_timeout_is_not_retried_exact_owned_call_is_cleaned(self):
        run, client, _, _ = self.runner('ma')
        client.start_timeout = True
        result = run.perform(40)
        self.assertEqual(result['error'], 'TimeoutError')
        self.assertTrue(result['cleanup']['owned_call_stopped'])
        self.assertEqual(sum(x[1] == 'player_queues/play_media' for x in client.operations), 1)
        self.assertTrue(client.autoplay)

    def test_session_appears_seconds_after_lost_start_reply(self):
        run, client, clock, _ = self.runner()
        original_phones = client.phones
        def lost_reply(operation, **values):
            client.operations.append(('ha', operation, None))
            raise TimeoutError('response lost before session appeared')
        def delayed_phones():
            if clock.now >= 3 and client.started is None:
                client.start(run.spec['fixture']['url'])
            return original_phones()
        client.ha, client.phones = lost_reply, delayed_phones
        result = run.perform(40)
        self.assertGreaterEqual(clock.now, 3)
        self.assertEqual(result['error'], 'TimeoutError')
        self.assertTrue(result['cleanup']['owned_call_stopped'])
        self.assertEqual(sum(x[1] == 'play_media' for x in client.operations), 1)
        self.assertEqual([x for x in client.operations if x[1] == 'stop'],
                         [('gateway', 'stop', 'owned-session')])

    def test_unknown_start_never_claims_stop_success(self):
        run, client, clock, _ = self.runner()
        run.preflight()
        run.state['dispatched'] = True
        run.cleanup()
        self.assertEqual(run.state['cleanup']['error'], 'start_outcome_unknown')
        self.assertNotIn('owned_call_stopped', run.state['cleanup'])
        self.assertLessEqual(clock.now, 8.25)

    def test_exact_pre_call_session_with_no_channel_can_be_cancelled(self):
        run, client, _, _ = self.runner()
        run.preflight()
        run.state['dispatched'] = True
        client.start(run.spec['fixture']['url'])
        client.selected['channels'] = []
        client.selected['player']['state'] = 'buffering'
        run.cleanup()
        self.assertTrue(run.state['cleanup']['pre_call_session_cancelled'])
        self.assertTrue(run.state['cleanup']['owned_call_stopped'])
        self.assertIsNone(run.state['owned'])
        self.assertEqual([x for x in client.operations if x[1] == 'stop'],
                         [('gateway', 'stop', 'owned-session')])

    def test_replacement_during_pending_channel_wait_is_untouched(self):
        run, client, _, _ = self.runner()
        run.preflight()
        run.state['dispatched'] = True
        client.start(run.spec['fixture']['url'])
        client.selected['channels'] = []
        client.selected['player']['state'] = 'buffering'
        self.assertFalse(run.bind(client.phones()[0]))
        client.selected['player']['session_id'] = 'replacement'
        run.cleanup()
        self.assertEqual(run.state['cleanup']['error'], 'pending_session_changed')
        self.assertFalse(any(x[1] == 'stop' for x in client.operations))

    def test_concurrent_ma_append_survives_exact_item_deletion(self):
        run, client, _, _ = self.runner('ma')
        original = client.ma
        def append_during_delete(command, **values):
            if command == 'player_queues/delete_item':
                client.items.append({'queue_item_id': 'foreign', 'media_item': {'item_id': 'other'}})
            return original(command, **values)
        client.ma = append_during_delete
        result = run.perform(40)
        self.assertEqual(result['cleanup']['error'], 'queue_clear_not_confirmed')
        self.assertEqual(client.items, [{'queue_item_id': 'foreign', 'media_item': {'item_id': 'other'}}])
        self.assertFalse(any(x[1] == 'player_queues/clear' for x in client.operations))

    def test_watchdog_replays_only_exact_owned_journal(self):
        run, client, clock, _ = self.runner('ma')
        run.preflight()
        run.state.update(dispatched=True, autoplay_changed=True, preferences_changed=True)
        client.ma('player_queues/play_media')
        run.bind(client.phones()[0])
        recovered = module.Run(run.spec, client, lambda _state: None, clock=clock, sleep=clock.sleep)
        recovered.state = copy.deepcopy(run.state)
        recovered.cleanup()
        self.assertTrue(recovered.state['cleanup']['owned_call_stopped'])
        self.assertTrue(recovered.state['cleanup']['queue_cleared'])
        self.assertEqual([x for x in client.operations if x[1] == 'stop'],
                         [('gateway', 'stop', 'owned-session')])

    def test_watchdog_esrch_race_still_allows_journal_cleanup(self):
        run, client, _, _ = self.runner()
        run.preflight()
        run.state.update(dispatched=True, preferences_changed=True)
        client.start(run.spec['fixture']['url'])
        run.bind(client.phones()[0])
        worker = Mock(pid=12345)
        worker.wait.side_effect = [subprocess.TimeoutExpired('fake', 45), 0]
        with patch.object(module.os, 'killpg', side_effect=ProcessLookupError):
            watchdog, interrupted = module.wait_for_worker(worker)
        self.assertTrue(watchdog)
        self.assertFalse(interrupted)
        run.cleanup()
        self.assertTrue(run.state['cleanup']['owned_call_stopped'])
        self.assertTrue(run.state['cleanup']['preferences_restored'])

    def test_initial_dispatch_has_longer_timeout_than_observation(self):
        client = module.Client(spec(), {'ha_token': 'fake', 'ma_token': 'fake'},
                               time.monotonic() + 60)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        client.opener = Mock()
        client.opener.open.return_value = response
        client.ha('play_media', media_content_id='fixture')
        self.assertEqual(client.opener.open.call_args.kwargs['timeout'], 12)
        client.ma('player_queues/play_media', media=['fixture'])
        self.assertEqual(client.opener.open.call_args.kwargs['timeout'], 12)
        client.ma('player_queues/get')
        self.assertEqual(client.opener.open.call_args.kwargs['timeout'], 2)

    def test_budget_is_rechecked_after_preference_changes(self):
        run, client, clock, _ = self.runner()
        original = client.gateway
        def slow_preferences(operation, **values):
            if operation == 'volume':
                clock.sleep(22)
            return original(operation, **values)
        client.gateway = slow_preferences
        result = run.perform(40)
        self.assertEqual(result['error'], 'insufficient_dispatch_budget')
        self.assertFalse(result['dispatched'])
        self.assertFalse(any(x[1] == 'play_media' for x in client.operations))

    def test_initially_disabled_autoplay_stays_disabled(self):
        run, client, _, _ = self.runner('ma')
        client.autoplay = False
        result = run.perform(40)
        self.assertTrue(result['cleanup']['autoplay_restored'])
        self.assertFalse(client.autoplay)

    def test_unrelated_drift_does_not_prevent_exact_owned_stop(self):
        run, client, _, _ = self.runner()
        client.unrelated_drift = True
        result = run.perform(40)
        self.assertEqual(result['error'], 'unrelated_session_changed')
        self.assertTrue(result['cleanup']['owned_call_stopped'])
        self.assertFalse(result['cleanup']['unrelated_sessions_unchanged'])
        self.assertEqual(client.other['player']['session_id'], 'changed-independently')

    def test_active_target_refuses_before_mutations(self):
        run, client, _, _ = self.runner()
        client.selected['player']['state'] = 'blocked'
        result = run.perform(40)
        self.assertEqual(result['error'], 'target_not_idle')
        self.assertEqual(client.operations, [])

    def test_empty_budget_refuses_before_mutations(self):
        run, client, _, _ = self.runner()
        result = run.perform(2)
        self.assertEqual(result['error'], 'insufficient_start_budget')
        self.assertEqual(client.operations, [])

    def test_attestation_exact_binding_freshness(self):
        raw, freeze = b'{}', b'{"candidate":1}'
        d = module.digest(freeze)
        gate = {'schema_version': 1, 'collector_verified': True, 'candidate_sha256': d,
                'test_spec_sha256': module.digest(raw), 'target': '201',
                'generated_at_unix': 100, 'expires_at_unix': 220}
        module.validate_attestation(raw, freeze, d, gate, '201', 101)
        for key, value in [('target', '203'), ('expires_at_unix', 150),
                           ('test_spec_sha256', '0' * 64), ('collector_verified', False)]:
            altered = dict(gate, **{key: value})
            with self.assertRaises(module.Refused):
                module.validate_attestation(raw, freeze, d, altered, '201', 101)

    def test_fixture_requires_unique_finite_path(self):
        module.validate_spec(spec())
        for field, value in [('duration_seconds', float('nan')), ('duration_seconds', 31),
                             ('bytes', 3 * 1024 * 1024), ('url', 'http://media.example/shared.mp3')]:
            invalid = spec()
            invalid['fixture'][field] = value
            with self.assertRaises(module.Refused):
                module.validate_spec(invalid)

    def test_default_plan_constructs_no_network_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            manifest, freeze = root / 'test.json', root / 'freeze.json'
            manifest.write_text(json.dumps(spec()))
            freeze.write_text('{}')
            manifest.chmod(0o600)
            freeze.chmod(0o600)
            output = io.StringIO()
            with patch.object(module, 'Client', side_effect=AssertionError('network forbidden')):
                with contextlib.redirect_stdout(output):
                    result = module.main(['--manifest', str(manifest), '--freeze', str(freeze),
                        '--freeze-sha256', module.digest(freeze.read_bytes()),
                        '--output-dir', str(root / 'new-run')])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())['network_actions'], 0)
            self.assertFalse((root / 'new-run').exists())

    def test_real_child_process_against_local_fake_http(self):
        """Exercise process launch, private journal and REST contracts without audio."""
        config = spec()
        payload = b'local fake fixture'
        config['fixture'].update(bytes=len(payload), sha256=module.digest(payload), duration_seconds=1)
        clock = Clock()
        clock.now = 0
        fake = FakeClient(config, clock)
        started = [None]

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path.endswith('/speech.mp3'):
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.assert_path('/v1/phones')
                if started[0] is not None:
                    clock.now = time.monotonic() - started[0]
                self.respond({'phones': fake.phones()})

            def assert_path(self, expected):
                if self.path != expected:
                    raise AssertionError(self.path)

            def respond(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/api/services/media_player/play_media':
                    started[0] = time.monotonic()
                    fake.ha('play_media', media_content_id=body['media_content_id'])
                elif self.path.startswith('/v1/phones/201/player_'):
                    operation = self.path.rsplit('_', 1)[-1]
                    fake.gateway(operation, session=body['session_id'],
                                 **{key: value for key, value in body.items()
                                    if key not in ('line', 'session_id', 'idempotency_key')})
                else:
                    raise AssertionError(self.path)
                self.respond({})

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f'http://127.0.0.1:{server.server_port}'
            config['fixture']['url'] = origin + '/' + config['test_id'] + '/speech.mp3'
            config['connections'].update(ha=origin, gateway=origin)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                values = {'test.json': config, 'freeze.json': {},
                          'credentials.json': {'ha_token': 'fake', 'gateway_token': 'fake'}}
                for name, value in values.items():
                    (root / name).write_text(json.dumps(value))
                    (root / name).chmod(0o600)
                freeze_sha = module.digest((root / 'freeze.json').read_bytes())
                gate = {'schema_version': 1, 'collector_verified': True,
                        'candidate_sha256': freeze_sha,
                        'test_spec_sha256': module.digest((root / 'test.json').read_bytes()),
                        'target': '201', 'generated_at_unix': time.time(),
                        'expires_at_unix': time.time() + 119}
                (root / 'attestation.json').write_text(json.dumps(gate))
                (root / 'attestation.json').chmod(0o600)
                result = subprocess.run([sys.executable, str(PATH), '--run', '--attended',
                    '--manifest', str(root / 'test.json'), '--freeze', str(root / 'freeze.json'),
                    '--freeze-sha256', freeze_sha, '--attestation', str(root / 'attestation.json'),
                    '--credentials', str(root / 'credentials.json'), '--output-dir', str(root / 'run')],
                    capture_output=True, text=True, timeout=12)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                receipt = json.loads((root / 'run/receipt.private.json').read_text())
                self.assertTrue(receipt['machine_passed'])
                self.assertFalse(receipt['watchdog_triggered'])
                self.assertEqual(receipt['human_confirmation']['heard_expected_phrase'], 'pending')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
