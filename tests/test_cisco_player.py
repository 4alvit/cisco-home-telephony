"""Music CLI contracts with synthetic library, resolver, and gateway only."""
import contextlib
import copy
import io
import json
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_cisco_cli import cisco, inventory
import cisco_player as player


SESSION = 'aaaaaaaa-1111-4111-8111-111111111111'
OTHER = 'bbbbbbbb-2222-4222-8222-222222222222'
PRIVATE = 'synthetic-private-query-token'


def state(**changes):
    return dict(v=1, capabilities=['play', 'url', 'pause', 'resume', 'seek', 'volume',
                'mute', 'queue', 'next', 'previous'], state='idle', reason=None,
                session_id=None, line=None, source_id=None, title=None, artist=None,
                album=None, position=0., position_updated_at=time.time(), duration=None,
                seekable=False, queue_index=0, queue_length=0, volume_level=.5,
                is_volume_muted=False, **changes)


def active(**changes):
    value = state()
    value.update(state='playing', session_id=SESSION, line='213',
                 source_id='https://media.example/song.flac?token=' + PRIVATE,
                 title='Example song', duration=60., seekable=True, queue_length=1)
    value.update(changes)
    return value


def track(name):
    return dict(relative_path='Example/' + name + '.flac',
                media_content_id='media-source://media_source/library/Example/' + name + '.flac',
                size_bytes=110000000)


class PlayerCLITests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket', 'socket.create_connection', 'subprocess.Popen'):
            guard = patch(name, side_effect=AssertionError('external operation forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def invoke(self, args, *, initial=None, tracks=None, posts=None, reads=None,
               resolver_side_effect=None, human=False, config=None):
        data = inventory()
        data['phones'][0]['player'] = state() if initial is None else copy.deepcopy(initial)
        calls, clients = [], []
        replies = iter(posts) if posts is not None else None
        snapshots = iter(reads) if reads is not None else None

        class Client:
            def __init__(self, settings, timeout):
                self.config, self.timeout, self.on_dispatch = settings, timeout, None
                clients.append(self)

            def request(self, path, body=None):
                calls.append((path, copy.deepcopy(body)))
                if body is None:
                    if snapshots is not None:
                        snapshot = next(snapshots)
                        data['phones'][0]['player'] = copy.deepcopy(snapshot)
                    return copy.deepcopy(data)
                if self.on_dispatch is not None:
                    self.on_dispatch()
                if replies is not None:
                    response = next(replies)
                    if isinstance(response, Exception):
                        raise response
                    if callable(response):
                        return response(body)
                    return response
                response = {'status': 'accepted', 'action_id': body['idempotency_key']}
                action = path.rsplit('/player_', 1)[-1]
                if action in ('volume', 'mute'):
                    response.update(volume_level=body.get('volume_level', .5),
                                    is_volume_muted=body.get('is_volume_muted', False))
                elif '/player_' in path:
                    response['session_id'] = body['session_id'] or SESSION
                    if body['session_id'] is None:
                        response['line'] = body['line']
                    if action == 'play':
                        data['phones'][0]['player'] = active(line=body['line'])
                return response

        selected = [track('One')] if tracks is None else tracks
        library = Mock(return_value=selected)
        def resolve(client, ids):
            if resolver_side_effect:
                return resolver_side_effect(client, ids, calls)
            return ['https://media.example/' + str(i) + '.flac?token=' + PRIVATE
                    for i in range(len(ids))]
        resolver = Mock(side_effect=resolve)
        configuration = (dict(music={'directory': '/example/library'},
                              home_assistant={'api': 'home_assistant'}) if config is None else config)
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {
                'cisco_library': SimpleNamespace(select_tracks=library),
                'cisco_media_source': SimpleNamespace(resolve_media=resolver)}), \
                patch.object(cisco, 'Gateway', Client), \
                patch.object(cisco, 'load_config', return_value=configuration), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cisco.main([*([] if human else ['--json']), '213', *args])
        self.assertEqual(errors.getvalue(), '')
        return code, output.getvalue() if human else json.loads(output.getvalue()), calls, library, resolver

    def test_direct_url_uses_selected_line_without_library_or_ha(self):
        code, result, calls, library, resolver = self.invoke(
            ['play', 'https://media.example/song.mp3?token=' + PRIVATE], config={})
        self.assertEqual((code, result['status']), (0, 'accepted'))
        self.assertEqual(calls[0], ('/v1/phones', None))
        self.assertEqual(calls[1][0], '/v1/phones/212/player_play')
        self.assertEqual(calls[1][1]['line'], '213')
        self.assertIsNone(calls[1][1]['session_id'])
        self.assertEqual(result['session_id'], SESSION)
        library.assert_not_called(); resolver.assert_not_called()
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_search_never_constructs_gateway_or_resolves_audio(self):
        code, result, calls, library, resolver = self.invoke(['search', 'ONE'])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        library.assert_called_once_with({'directory': '/example/library'}, 'ONE')
        resolver.assert_not_called()
        self.assertEqual(result['matches'], [{'relative_path': 'Example/One.flac', 'size_bytes': 110000000}])

    def test_session_is_observed_before_library_urls_are_resolved(self):
        def resolve(client, ids, calls):
            self.assertEqual(calls, [('/v1/phones', None)])
            self.assertEqual(client.config, {'api': 'home_assistant'})
            self.assertEqual(ids, [track('One')['media_content_id']])
            return ['https://media.example/one.flac']
        code, _, calls, _, _ = self.invoke(['play', 'one'], initial=active(), resolver_side_effect=resolve)
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1][1]['session_id'], SESSION)
        self.assertEqual(calls[-1][1]['metadata'], {'title': 'One'})

    def test_multiple_matches_play_by_default_and_legacy_all_is_equivalent(self):
        for flags in ([], ['--all']):
            with self.subTest(flags=flags):
                code, result, calls, _, resolver = self.invoke(
                    ['play', 'example', *flags], tracks=[track('One'), track('Two')])
                self.assertEqual((code, result['accepted_count']), (0, 2))
                resolver.assert_called_once()
                self.assertEqual([body['metadata']['title'] for _, body in calls if body], ['One', 'Two'])
                self.assertEqual([body['line'] for _, body in calls if body], ['213', '213'])

    def test_all_tracks_resolve_up_front_and_append_only_after_readiness(self):
        code, result, calls, _, resolver = self.invoke(
            ['play', 'example', '--idempotency-key', 'playlist-example'],
            tracks=[track('One'), track('Two'), track('Three')])
        self.assertEqual((code, result['accepted_count'], result['remaining_count']), (0, 3, 0))
        resolver.assert_called_once()
        self.assertEqual([body['enqueue'] for _, body in calls if body], ['replace', 'add', 'add'])
        self.assertEqual([body['session_id'] for _, body in calls if body], [None, SESSION, SESSION])
        self.assertEqual(calls[2], ('/v1/phones', None))
        self.assertEqual(result['action_ids'], ['playlist-example:1', 'playlist-example:2', 'playlist-example:3'])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_next_and_play_enqueue_preserve_selection_order(self):
        selected = [track('One'), track('Two'), track('Three')]
        for mode, titles, modes in (
                ('next', ['Three', 'Two', 'One'], ['next', 'next', 'next']),
                ('play', ['One', 'Three', 'Two'], ['play', 'next', 'next']),
                ('add', ['One', 'Two', 'Three'], ['add', 'add', 'add'])):
            with self.subTest(mode=mode):
                code, _, calls, _, _ = self.invoke(['play', 'example', '--enqueue', mode],
                                                   tracks=selected, initial=active())
                self.assertEqual(code, 0)
                bodies = [body for _, body in calls if body]
                self.assertEqual([body['metadata']['title'] for body in bodies], titles)
                self.assertEqual([body['enqueue'] for body in bodies], modes)

    def test_queue_failure_retains_accepted_and_uncertain_requests_without_retry(self):
        def accepted(body):
            return dict(status='accepted', action_id=body['idempotency_key'], session_id=SESSION)
        code, result, calls, _, _ = self.invoke(
            ['play', 'example', '--idempotency-key', 'partial-example'],
            tracks=[track('One'), track('Two'), track('Three')], initial=active(),
            posts=[accepted, OSError(PRIVATE)])
        self.assertEqual((code, result['status']), (1, 'not_confirmed'))
        self.assertEqual((result['accepted_count'], result['remaining_count'],
                          result['unconfirmed_count'], result['not_attempted_count']), (1, 2, 1, 1))
        self.assertEqual(result['accepted_action_ids'], ['partial-example:1'])
        self.assertEqual(result['action_ids'], ['partial-example:1', 'partial-example:2'])
        self.assertEqual(len(calls), 3)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_replacement_session_during_readiness_is_never_adopted(self):
        code, result, calls, _, _ = self.invoke(
            ['play', 'example', '--all'], tracks=[track('One'), track('Two')],
            reads=[state(), active(session_id=OTHER)])
        self.assertEqual((code, result['status'], result['reason']), (1, 'partial', 'player_session_changed'))
        self.assertEqual((result['accepted_count'], result['remaining_count']), (1, 1))
        self.assertEqual(len([body for _, body in calls if body]), 1)

    def test_readiness_is_bounded_and_does_not_repeat_play(self):
        with patch.object(player.time, 'monotonic', side_effect=[0., 0., 0., 21.]), \
                patch.object(player.time, 'sleep'):
            code, result, calls, _, _ = self.invoke(
                ['play', 'example', '--all'], tracks=[track('One'), track('Two')],
                reads=[state(), active(state='buffering')])
        self.assertEqual((code, result['status'], result['reason']), (1, 'partial', 'player_readiness_timeout'))
        self.assertEqual(len([body for _, body in calls if body]), 1)

    def test_sibling_line_is_refused_before_resolution(self):
        code, result, calls, library, resolver = self.invoke(['play', 'one'], initial=active(line='212'))
        self.assertEqual((code, result['reason']), (1, 'player_owned_by_other_line'))
        self.assertEqual(len(calls), 1)
        library.assert_not_called(); resolver.assert_not_called()

    def test_controls_pin_the_observed_session_and_do_not_dial(self):
        for command, action in (('pause', 'pause'), ('resume', 'resume'), ('stop', 'stop'),
                                ('next', 'next'), ('previous', 'previous'), ('clear-queue', 'clear_queue'),
                                ('mute', 'mute'), ('unmute', 'mute')):
            with self.subTest(command=command):
                code, _, calls, _, _ = self.invoke([command], initial=active())
                self.assertEqual(code, 0)
                self.assertEqual(calls[-1][0], '/v1/phones/212/player_' + action)
                self.assertEqual(calls[-1][1]['session_id'], SESSION)
                self.assertEqual(calls[-1][1]['line'], '213')

    def test_stop_without_player_session_does_not_hang_up_a_call(self):
        initial = state(); initial.update(state='blocked', reason='call')
        code, result, calls, _, _ = self.invoke(['stop'], initial=initial)
        self.assertEqual((code, result['reason']), (1, 'player_not_active'))
        self.assertEqual(len(calls), 1)

    def test_gain_and_seek_validate_numeric_bounds_and_meanings(self):
        code, _, calls, _, _ = self.invoke(['gain', '75'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1][1]['volume_level'], .75)
        self.assertIsNone(calls[-1][1]['session_id'])
        code, _, calls, _, _ = self.invoke(['seek', '12.5'], initial=active())
        self.assertEqual(code, 0); self.assertEqual(calls[-1][1]['position'], 12.5)
        for command in (['gain', 'nan'], ['gain', 'inf'], ['gain', '1e1000'], ['gain', '-1'],
                        ['gain', '101'], ['seek', '61'], ['seek', 'nan']):
            code, _, calls, _, _ = self.invoke(command, initial=active())
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)

    def test_malformed_receipts_remain_unknown(self):
        for changed in ({'action_id': 'wrong'}, {'session_id': OTHER},
                        {'session_id': True}, {'line': '212'}, {'status': 'ok'}):
            def response(body, changed=changed):
                return dict(dict(status='accepted', action_id=body['idempotency_key'], session_id=SESSION), **changed)
            code, result, calls, _, _ = self.invoke(['pause'], initial=active(), posts=[response])
            self.assertEqual((code, result['status']), (1, 'not_confirmed'))
            self.assertEqual(len(calls), 2)

    def test_invalid_player_schema_cannot_dispatch(self):
        for field, invalid in (('v', True), ('queue_length', True), ('queue_index', 10**1000),
                               ('position', 10**1000), ('volume_level', True), ('duration', float('nan')),
                               ('session_id', 'bad'), ('reason', PRIVATE)):
            initial = active(); initial[field] = invalid
            code, result, calls, _, _ = self.invoke(['pause'], initial=initial)
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_music_and_queue_status_hide_signed_query_strings(self):
        for command in ('music', 'queue'):
            code, result, calls, _, _ = self.invoke([command], initial=active())
            self.assertEqual(code, 0); self.assertEqual(len(calls), 1)
            self.assertEqual(result['player']['source_id'], 'https://media.example/song.flac')
            self.assertNotIn(PRIVATE, json.dumps(result))
        _, output, _, _, _ = self.invoke(['music'], initial=active(), human=True)
        self.assertIn('Queue: 1/1', output); self.assertNotIn(PRIVATE, output)

    def test_invalid_resolved_url_rejects_whole_selection_before_mutation(self):
        code, result, calls, _, _ = self.invoke(['play', 'example', '--all'],
            tracks=[track('One'), track('Two')],
            resolver_side_effect=lambda *_: ['https://media.example/one.flac', 'file:///example/two.flac'])
        self.assertEqual((code, result['reason']), (1, 'invalid_music_url'))
        self.assertEqual(len(calls), 1)

    def test_queue_limits_dry_run_and_long_batch_key_do_not_mutate(self):
        for args, initial in ((['play', 'example', '--all', '--enqueue', 'add'], active(queue_length=50)),
                              (['play', 'example', '--all', '--idempotency-key', 'a'*126], active())):
            code, _, calls, _, _ = self.invoke(args, initial=initial, tracks=[track('One'), track('Two')])
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)
        code, result, calls, _, _ = self.invoke(['play', 'one', '--dry-run'])
        self.assertEqual((code, result['status']), (0, 'dry_run'))
        self.assertEqual(len(calls), 1); self.assertNotIn(PRIVATE, json.dumps(result))

    def test_numeric_shortcut_remains_a_phone_call(self):
        code, result, calls, library, resolver = self.invoke(['600'])
        self.assertEqual((code, result['action']), (0, 'dial'))
        self.assertEqual(calls[-1][0], '/v1/phones/212/dial')
        library.assert_not_called(); resolver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
