import copy
import hashlib
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('phone_notifications', Path(__file__).resolve().parents[1] /
                                              'containers/asterisk/phone_notifications.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def config(**changes):
    value = {'enabled': True, 'sources': ['general'], 'screen_destinations': ['201', '202']}
    value.update(changes)
    return value


def event(**changes):
    value = {'source': 'general', 'event_id': 'notification-0001', 'event_at': 1000,
             'title': 'Test notification', 'message': 'Hello <world> & привет'}
    value.update(changes)
    return value


class Gateway:
    def __init__(self):
        self.stopped = threading.Event()
        self.calls = []
        self.row = {'id': '201', 'lines': ['201', '202'], 'online': True,
                    'dnd': False, 'dnd_pending': False, 'channels': [], 'alert': None,
                    'notification': {'eligible': True, 'reason': None},
                    'capabilities': ['notification', 'answer']}
        self.now = 1000

    def listing(self):
        return {'ami_fresh': True, 'observed_at': self.now, 'phones': [copy.deepcopy(self.row)]}

    def mutate(self, phone, action, body):
        self.calls.append((phone, action, body))
        return {'status': 'accepted', 'action_id': body['idempotency_key']}

    def answer_notification(self, phone, body):
        return self.mutate(phone, 'notification_answer', body)


class DeferredThread:
    def __init__(self, *, target, args, **kwargs):
        self.target, self.args, self.active = target, args, False

    def start(self):
        self.active = True

    def is_alive(self):
        return self.active

    def run(self):
        try:
            self.target(*self.args)
        finally:
            self.active = False


class Notifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'private'
        self.gateway = Gateway()
        self.now = 1000
        self.jobs = []
        self.stops = []
        self.observations = []

    def announcer(self, target, body, timeout):
        self.jobs.append((target, body, timeout))
        return {'id': hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32],
                'status': 'accepted', 'recipients': [{'line': line, 'status': 'queued'}
                                                     for line in body['lines']]}

    def dispatcher(self, value=None, **kwargs):
        return module.Dispatcher(config() if value is None else value, self.gateway, self.directory,
                                 clock=lambda: self.now, announcer=self.announcer,
                                 thread_factory=DeferredThread, **kwargs)

    def echo_config(self, **changes):
        value = config(speech_enabled=True, echo_destinations=['223', '224'],
                       announcement={'base_url': 'http://127.0.0.1:18085', 'token': 'a' * 48})
        value.update(changes)
        return value

    def speech_config(self, **changes):
        return self.echo_config(screen_destinations=[], speech_destinations=['*'], **changes)

    def job(self, status='ringing', **changes):
        body = self.jobs[-1][1]
        value = {'id': hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32],
                 'status': 'running', 'expires_at': 1180, 'recipients': [
                    {'line': line, 'phone_id': line, 'driver': route, 'status': status if route == 'sip' else 'playing',
                     'channel': 'PJSIP/' + line + '-abcd' if route == 'sip' else None,
                     'uniqueid': '1000.1' if route == 'sip' else None, 'caller_id': '812345678'}
                    for line, route in body['expected_routes'].items()]}
        value.update(changes)
        return value

    def read_job(self, config, body, timeout):
        self.assertNotIn('text', body)
        self.assertTrue(self.observations, 'unexpected status read')
        value = self.observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def stop_job(self, config, body, timeout):
        self.assertNotIn('text', body)
        self.stops.append(body)

    def speech_dispatcher(self, **changes):
        dispatcher = self.dispatcher(self.speech_config(**changes), reader=self.read_job,
                                     stopper=self.stop_job, sleep=lambda _: None)
        self.assertTrue(dispatcher.dispatch(event())['accepted'])
        self.assertIsNotNone(dispatcher.worker)
        return dispatcher

    def set_ringing(self):
        self.gateway.row['channels'] = [{'name': 'PJSIP/201-abcd', 'uniqueid': '1000.1',
                                         'state': 'Ringing', 'line': '201'}]

    def test_cisco_and_echo_speech_share_one_synthesis_with_fixed_routes(self):
        dispatcher = self.speech_dispatcher()
        body = self.jobs[0][1]
        self.assertEqual(body['lines'], ['201', '223', '224'])
        self.assertEqual(body['expected_routes'], {'201': 'sip', '223': 'ha_satellite', '224': 'ha_satellite'})
        self.assertEqual(body['text'], event()['message'])
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(dispatcher.dispatch(event(event_id='notification-0002'))['reason'],
                         'notification_speech_busy')

    def test_cisco_sibling_lines_deduplicate_to_primary_speech_line(self):
        value = self.speech_config()
        value['speech_destinations'] = ['201', '202']
        result = self.dispatcher(value).dispatch(event(), dry_run=True)
        self.assertEqual(result['eligible'], ['201', '223', '224'])
        self.assertFalse(self.jobs)
        self.assertFalse(self.directory.exists())

    def test_speech_screen_alias_overlap_refuses_before_side_effects(self):
        value = self.echo_config(screen_destinations=['201'], speech_destinations=['202'])
        result = self.dispatcher(value).dispatch(event())
        self.assertEqual(result['reason'], 'overlapping_notification_destinations')
        self.assertFalse(self.jobs)
        self.assertFalse(self.gateway.calls)

    def test_speech_all_phones_honors_idle_dnd_alert_and_voice_guards(self):
        for index, change in enumerate(({'dnd': True}, {'dnd_pending': True}, {'online': False},
                {'channels': [{}]}, {'alert': {'state': 'ringing'}},
                {'notification': {'eligible': False}}, {'capabilities': ['notification']})):
            with self.subTest(change=change):
                self.gateway = Gateway()
                self.gateway.row.update(change)
                result = self.dispatcher(self.speech_config()).dispatch(
                    event(event_id=f'notification-{index:04}'), dry_run=True)
                self.assertNotIn('201', result['eligible'])
                self.assertEqual(len(result['skipped']), 1)
                self.assertFalse(self.jobs)
                self.assertFalse(self.gateway.calls)

    def test_disabled_speech_never_starts_monitor_or_calls(self):
        dispatcher = self.dispatcher(self.speech_config(speech_enabled=False))
        result = dispatcher.dispatch(event())
        self.assertEqual([row['reason'] for row in result['skipped']], ['speech_disabled'] * 3)
        self.assertFalse(self.jobs)
        self.assertIsNone(dispatcher.worker)

    def test_owned_incoming_call_is_answered_once_without_global_autoanswer(self):
        dispatcher = self.speech_dispatcher()
        self.set_ringing()
        ringing = self.job()
        self.observations = [ringing, ringing, ringing, self.job(status='completed')]
        self.observations[-1]['status'] = 'finished'
        dispatcher.worker.run()
        self.assertEqual(len(self.gateway.calls), 1)
        phone, action, body = self.gateway.calls[0]
        self.assertEqual((phone, action), ('201', 'notification_answer'))
        self.assertEqual(body, {'line': '201', 'channel': 'PJSIP/201-abcd', 'uniqueid': '1000.1',
                               'idempotency_key': 'announce-' + ringing['id'] + '-201'})
        self.assertFalse(self.stops)
        self.assertFalse(dispatcher.worker.is_alive())

    def test_ambiguous_answer_is_not_repeated_and_only_owned_job_is_stopped(self):
        dispatcher = self.speech_dispatcher()
        self.set_ringing()
        ringing = self.job()
        self.observations = [ringing, ringing]
        def fail(phone, body):
            self.gateway.calls.append((phone, 'notification_answer', body))
            raise OSError('private details')
        self.gateway.answer_notification = fail
        dispatcher.worker.run()
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(self.stops[0]['idempotency_key'], self.jobs[0][1]['idempotency_key'])
        self.assertEqual(dispatcher.dispatch(event())['reason'], 'duplicate_event')

    def test_changed_job_identity_refuses_answer_and_stops_owned_job(self):
        dispatcher = self.speech_dispatcher()
        self.set_ringing()
        changed = self.job()
        changed['recipients'][0]['uniqueid'] = '1000.2'
        self.observations = [self.job(), changed]
        dispatcher.worker.run()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.stops), 1)

    def test_changed_phone_call_dnd_or_alert_refuses_answer(self):
        cases = [{'channels': [{'name': 'PJSIP/201-other', 'uniqueid': '1000.2',
                               'state': 'Ringing', 'line': '201'}]},
                 {'dnd': True}, {'dnd_pending': True}, {'alert': {'state': 'ringing'}}]
        for changes in cases:
            with self.subTest(changes=changes):
                # Use a separate private ledger for each independent job.
                self.directory = Path(self.temp.name) / ('private-' + str(len(self.jobs)))
                self.gateway = Gateway()
                dispatcher = self.speech_dispatcher()
                self.set_ringing()
                self.gateway.row.update(changes)
                self.observations = [self.job(), self.job()]
                dispatcher.worker.run()
                self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.stops), len(cases))

    def test_stale_event_during_preparation_never_answers(self):
        dispatcher = self.speech_dispatcher()
        self.set_ringing()
        self.now = 1091
        self.gateway.now = self.now
        self.observations = [self.job()]
        dispatcher.worker.run()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.stops), 1)

    def test_stale_event_while_synthesizing_is_stopped_before_ringing(self):
        dispatcher = self.speech_dispatcher()
        self.observations = [self.job(status='queued')]
        def advance(_):
            self.now = 1091
        dispatcher.sleep = advance
        dispatcher.worker.run()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.stops), 1)
        self.assertFalse(self.observations)

    def test_quiet_hours_beginning_during_preparation_prevents_answer(self):
        dispatcher = self.speech_dispatcher()
        dispatcher.config['quiet_hours'] = {'start': '00:17', 'end': '00:20', 'timezone': 'UTC'}
        self.observations = [self.job(status='queued')]
        dispatcher.sleep = lambda _: setattr(self, 'now', 1020)
        dispatcher.worker.run()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.stops), 1)

    def test_thread_start_failure_stops_accepted_sip_job_once(self):
        class BrokenThread(DeferredThread):
            def start(self):
                raise RuntimeError('cannot start')
        dispatcher = self.dispatcher(self.speech_config(), stopper=self.stop_job)
        dispatcher.thread_factory = BrokenThread
        result = dispatcher.dispatch(event())
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(len(result['unknown']), 3)
        self.assertEqual(len(self.stops), 1)
        self.assertFalse(self.gateway.calls)

    def test_monitor_deadline_stops_once(self):
        dispatcher = self.speech_dispatcher()
        ticks = iter([0, 181])
        dispatcher.monotonic = lambda: next(ticks)
        dispatcher.worker.run()
        self.assertEqual(len(self.stops), 1)
        self.assertFalse(self.gateway.calls)

    def test_gateway_stop_cancels_only_owned_job(self):
        dispatcher = self.speech_dispatcher()
        self.gateway.stopped.set()
        dispatcher.worker.run()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.stops), 1)
        self.assertFalse(self.observations)

    def test_read_error_stops_once_even_when_stop_is_ambiguous(self):
        dispatcher = self.speech_dispatcher()
        self.observations = [OSError('unavailable')]
        def uncertain(*args):
            self.stop_job(*args)
            raise OSError('unavailable')
        dispatcher.stopper = uncertain
        dispatcher.worker.run()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.stops), 1)

    def test_aggregate_speech_target_limit_refuses_before_effects(self):
        value = self.speech_config(echo_destinations=[str(n) for n in range(223, 239)])
        result = self.dispatcher(value).dispatch(event())
        self.assertEqual(result['reason'], 'too_many_notification_speakers')
        self.assertFalse(self.jobs)
        self.assertFalse(self.gateway.calls)

    def test_sip_response_rejects_wrong_identity_channels_routes_and_expiry(self):
        self.speech_dispatcher()
        body = self.jobs[0][1]
        value = self.job()
        value['lines'] = body['lines']
        self.assertEqual(module.validate_announcement(value, body)['recipients'][0]['status'], 'ringing')
        for key, change in (('channel', 'PJSIP/202-other'), ('uniqueid', None),
                            ('caller_id', 'private'), ('driver', 'ha_satellite'), ('phone_id', '202')):
            with self.subTest(key=key), self.assertRaises(module.NotificationError):
                corrupt = copy.deepcopy(value)
                corrupt['recipients'][0][key] = change
                module.validate_announcement(corrupt, body)
        for expires in (None, True, float('inf'), float('nan'), -1):
            with self.subTest(expires=expires), self.assertRaises(module.NotificationError):
                module.validate_announcement(dict(value, expires_at=expires), body)

    def test_sibling_lines_deduplicated_and_xml_text_untouched(self):
        result = self.dispatcher().dispatch(event())
        self.assertEqual(result['accepted'], [{'target': '201', 'route': 'screen'}])
        self.assertEqual(len(self.gateway.calls), 1)
        phone, action, body = self.gateway.calls[0]
        self.assertEqual((phone, action), ('201', 'notification'))
        self.assertEqual(body['text'], event()['message'])
        self.assertEqual(body['idle_close_seconds'], 45)
        self.assertFalse(result['display_verified'])

    def test_replay_after_restart_does_not_send(self):
        self.dispatcher().dispatch(event())
        result = self.dispatcher().dispatch(event())
        self.assertEqual(result['reason'], 'duplicate_event')
        self.assertEqual(len(self.gateway.calls), 1)

    def test_conflicting_payload_is_not_replayed(self):
        self.dispatcher().dispatch(event())
        result = self.dispatcher().dispatch(event(message='Different'))
        self.assertEqual(result['reason'], 'notification_event_conflict')
        self.assertEqual(len(self.gateway.calls), 1)

    def test_ambiguous_screen_failure_is_claimed(self):
        self.gateway.mutate = lambda *args: (_ for _ in ()).throw(OSError('private details'))
        result = self.dispatcher().dispatch(event())
        self.assertEqual(result['unknown'], [{'target': '201', 'reason': 'delivery_unknown'}])
        self.assertNotIn('private details', json.dumps(result))
        self.assertEqual(self.dispatcher().dispatch(event())['reason'], 'duplicate_event')

    def test_cooldown_drops_instead_of_deferring(self):
        self.dispatcher().dispatch(event())
        second = event(event_id='notification-0002')
        result = self.dispatcher().dispatch(second)
        self.assertEqual(result['skipped'], [{'target': '201', 'reason': 'cooldown'}])
        self.now = 1070
        self.gateway.now = self.now
        self.assertEqual(self.dispatcher().dispatch(second)['reason'], 'duplicate_event')
        self.assertEqual(len(self.gateway.calls), 1)

    def test_dry_run_does_not_create_state_or_claim(self):
        result = self.dispatcher(self.echo_config()).dispatch(event(), dry_run=True)
        self.assertEqual(result['eligible'], ['201', '223', '224'])
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.gateway.calls, [])
        self.assertTrue(self.dispatcher().dispatch(event())['accepted'])

    def test_dry_run_existing_state_is_readonly_and_observes_cooldown(self):
        self.dispatcher().dispatch(event())
        before = (self.directory / 'notifications.sqlite3').read_bytes()
        result = self.dispatcher().dispatch(event(event_id='notification-0002'), dry_run=True)
        self.assertEqual(result['skipped'], [{'target': '201', 'reason': 'cooldown'}])
        self.assertEqual(before, (self.directory / 'notifications.sqlite3').read_bytes())

    def test_disabled_performs_no_state_or_inventory_access(self):
        self.gateway.listing = lambda: self.fail('listing while disabled')
        self.assertEqual(self.dispatcher({}).dispatch(event())['reason'], 'disabled')
        self.assertFalse(self.directory.exists())

    def test_speech_is_separately_disabled(self):
        result = self.dispatcher(self.echo_config(speech_enabled=False)).dispatch(event())
        self.assertEqual(self.jobs, [])
        self.assertEqual([row['reason'] for row in result['skipped']], ['speech_disabled'] * 2)
        self.assertTrue(result['accepted'])

    def test_source_and_timestamps_fail_before_io(self):
        for changes in ({'source': 'unlisted'}, {'event_at': 900}, {'event_at': 1006},
                        {'event_at': float('nan')}, {'event_at': True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.dispatcher().dispatch(event(**changes))['status'], 'refused')
        self.assertFalse(self.directory.exists())

    def test_busy_dnd_offline_and_capability_fail_closed(self):
        cases = [({'online': False}, 'offline'), ({'dnd': True}, 'dnd'),
                 ({'dnd_pending': True}, 'dnd'), ({'channels': [{}]}, 'busy'),
                 ({'alert': {'state': 'ringing'}}, 'alert'),
                 ({'notification': {'eligible': False}}, 'voice_busy_or_unknown'),
                 ({'notification': None}, 'voice_busy_or_unknown'),
                 ({'capabilities': ['message']}, 'notification_unavailable')]
        for index, (changes, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                self.gateway = Gateway()
                self.gateway.row.update(changes)
                result = self.dispatcher().dispatch(event(event_id=f'notification-{index:04}'))
                self.assertEqual(result['skipped'], [{'target': '201', 'reason': reason}])
                self.assertFalse(self.gateway.calls)

    def test_echo_submission_pins_direct_routes_and_records_job_only(self):
        result = self.dispatcher(self.echo_config()).dispatch(event())
        body = self.jobs[0][1]
        self.assertEqual(body['expected_routes'], {'223': 'ha_satellite', '224': 'ha_satellite'})
        self.assertEqual(body['lines'], ['223', '224'])
        self.assertEqual(body['text'], event()['message'])
        self.assertEqual(result['accepted'][1]['job_id'],
                         hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32])
        self.assertFalse(result['playback_verified'])
        self.assertEqual(self.dispatcher(self.echo_config()).dispatch(event())['reason'], 'duplicate_event')
        self.assertEqual(len(self.jobs), 1)

    def test_partial_echo_failure_does_not_repeat_screen(self):
        dispatcher = self.dispatcher(self.echo_config())
        dispatcher.announcer = lambda *args: (_ for _ in ()).throw(OSError('private speech'))
        result = dispatcher.dispatch(event())
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['unknown']), 2)
        self.assertNotIn('private speech', json.dumps(result))
        self.assertEqual(dispatcher.dispatch(event())['reason'], 'duplicate_event')

    def test_echo_terminal_failure_is_known_skip_not_audible_acceptance(self):
        dispatcher = self.dispatcher(self.echo_config())
        def fail(*args):
            job = self.announcer(*args)
            job['status'] = 'failed'
            for row in job['recipients']:
                row['status'] = 'failed'
            return job
        dispatcher.announcer = fail
        result = dispatcher.dispatch(event())
        self.assertEqual(len(result['accepted']), 1)
        self.assertEqual([row['reason'] for row in result['skipped']], ['announcement_failed'] * 2)
        self.assertFalse(result['unknown'])

    def test_alias_collision_prevents_any_delivery(self):
        value = self.echo_config(screen_destinations=['*'], echo_destinations=['202'])
        result = self.dispatcher(value).dispatch(event())
        self.assertEqual(result['reason'], 'overlapping_notification_destinations')
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.jobs, [])

    def test_inventory_failure_does_not_block_independent_echo_route(self):
        self.gateway.listing = lambda: (_ for _ in ()).throw(OSError('offline'))
        result = self.dispatcher(self.echo_config()).dispatch(event())
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(result['skipped'][0]['reason'], 'inventory_unavailable')

    def test_text_limits_and_secret_free_journal(self):
        title = 'T' * 100
        message = 'я' * 1500
        self.dispatcher(self.echo_config()).dispatch(event(title=title, message=message))
        self.assertEqual(len(self.gateway.calls[0][2]['title']), 32)
        self.assertEqual(len(self.jobs[0][1]['text']), 1000)
        raw = (self.directory / 'notifications.sqlite3').read_bytes()
        self.assertNotIn(title.encode(), raw)
        self.assertNotIn(message.encode(), raw)
        self.assertNotIn(('a' * 48).encode(), raw)
        self.assertEqual((self.directory / 'notifications.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_invalid_text_fields_rejected(self):
        for changes in ({'message': ''}, {'message': ' '}, {'message': 'x' * 2001},
                        {'message': '\x00'}, {'message': '\ud800'}, {'title': ''},
                        {'title': 'x' * 129}, {'service': 'evil'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.dispatcher().dispatch(event(**changes))['status'], 'refused')
        self.assertFalse(self.gateway.calls)

    def test_stop_and_deadline_prevent_dispatch(self):
        self.gateway.stopped.set()
        self.assertEqual(self.dispatcher().dispatch(event())['reason'], 'notification_gateway_stopping')
        self.assertFalse(self.directory.exists())
        self.gateway.stopped.clear()
        ticks = iter([0, 46])
        result = self.dispatcher(monotonic=lambda: next(ticks)).dispatch(event())
        self.assertEqual(result['reason'], 'notification_deadline')
        self.assertFalse(self.gateway.calls)

    def test_concurrent_duplicate_has_one_mutation(self):
        entered, release = threading.Event(), threading.Event()
        original = self.gateway.mutate
        def blocking(*args):
            entered.set()
            release.wait(3)
            return original(*args)
        self.gateway.mutate = blocking
        first = self.dispatcher()
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(first.dispatch(event())))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(first.dispatch(event())['reason'], 'notification_dispatch_busy')
            self.assertEqual(self.dispatcher().dispatch(event())['reason'], 'duplicate_event')
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_state_permission_failure_sends_nothing(self):
        self.directory.mkdir(mode=0o755)
        os.chmod(self.directory, 0o755)
        self.assertEqual(self.dispatcher().dispatch(event())['reason'], 'notification_state_permissions')
        self.assertFalse(self.gateway.calls)

    def test_state_symlink_is_refused(self):
        self.directory.mkdir(mode=0o700)
        target = Path(self.temp.name) / 'other'
        target.write_text('not a database')
        (self.directory / 'notifications.sqlite3').symlink_to(target)
        result = self.dispatcher().dispatch(event())
        self.assertEqual(result['reason'], 'notification_state_permissions')
        self.assertEqual(target.read_text(), 'not a database')
        self.assertFalse(self.gateway.calls)

    def test_durable_claim_before_remote_mutation(self):
        def inspect(*args):
            db = sqlite3.connect(self.directory / 'notifications.sqlite3')
            try:
                self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM targets').fetchone()[0], 1)
            finally:
                db.close()
            raise OSError('acknowledgement lost')
        self.gateway.mutate = inspect
        self.assertEqual(len(self.dispatcher().dispatch(event())['unknown']), 1)

    def test_future_inventory_and_missing_guard_fail_closed(self):
        self.gateway.now = self.now + 1
        result = self.dispatcher().dispatch(event())
        self.assertEqual(result['skipped'][0]['reason'], 'inventory_unavailable')
        self.assertFalse(self.gateway.calls)

    def test_announcement_response_identity_routes_and_states(self):
        body = {'idempotency_key': 'notification-test', 'lines': ['223', '224']}
        value = {'id': hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32],
                 'status': 'accepted', 'lines': ['223', '224'], 'recipients': [
                     {'line': line, 'phone_id': line, 'driver': 'ha_satellite',
                      'channel': None, 'uniqueid': None, 'status': 'queued'} for line in body['lines']]}
        self.assertEqual(module.validate_announcement(value, body)['id'], value['id'])
        for mutation in ('id', 'lines', 'duplicate', 'driver', 'physical', 'channel', 'ringing', 'terminal'):
            corrupt = copy.deepcopy(value)
            if mutation == 'id': corrupt['id'] = 'f' * 32
            elif mutation == 'lines': corrupt['lines'] = ['223', '223']
            elif mutation == 'duplicate': corrupt['recipients'][1] = corrupt['recipients'][0]
            elif mutation == 'driver': corrupt['recipients'][0]['driver'] = 'sip'
            elif mutation == 'physical': corrupt['recipients'][0]['phone_id'] = '201'
            elif mutation == 'channel': corrupt['recipients'][0]['channel'] = 'PJSIP/223-abcd'
            elif mutation == 'ringing': corrupt['recipients'][0]['status'] = 'ringing'
            elif mutation == 'terminal': corrupt['status'] = 'finished'
            with self.subTest(mutation=mutation), self.assertRaises(module.NotificationError):
                module.validate_announcement(corrupt, body)
        value['status'] = 'failed'
        for row in value['recipients']:
            row['status'] = 'failed'
        self.assertEqual(module.validate_announcement(value, body)['status'], 'failed')

    def test_loopback_submit_uses_no_proxy_or_redirect_and_correlates_response(self):
        body = {'idempotency_key': 'notification-test', 'lines': ['223']}
        value = {'id': hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32],
                 'status': 'accepted', 'lines': ['223'], 'recipients': [
                     {'line': '223', 'phone_id': '223', 'driver': 'ha_satellite',
                      'channel': None, 'uniqueid': None, 'status': 'queued'}]}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps(value).encode()
        with patch.object(module.request, 'build_opener') as build:
            build.return_value.open.return_value = Response()
            result = module.submit_announcement({'base_url': 'http://127.0.0.1:18085', 'token': 'x' * 48}, body, 4)
            self.assertEqual(result['id'], value['id'])
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], module.NoRedirect)
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, 'http://127.0.0.1:18085/v1/announcements')
            self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 4)

    def test_quiet_hours_drop_event_and_span_midnight(self):
        value = config(quiet_hours={'start': '22:00', 'end': '08:00', 'timezone': 'UTC'})
        self.now = datetime(2026, 11, 1, 23, tzinfo=timezone.utc).timestamp()
        current = event(event_at=self.now)
        result = self.dispatcher(value).dispatch(current)
        self.assertEqual(result['reason'], 'quiet_hours')
        self.assertFalse(self.gateway.calls)
        self.assertEqual(self.dispatcher(value).dispatch(current)['reason'], 'duplicate_event')
        self.assertTrue(module.quiet_now(module.validate_config(value), self.now + 8 * 3600))
        self.assertFalse(module.quiet_now(module.validate_config(value), self.now + 9 * 3600))

    def test_quiet_hours_dst_fall_back_covers_both_repeated_hours(self):
        value = module.validate_config(config(quiet_hours={
            'start': '01:00', 'end': '02:00', 'timezone': 'America/Los_Angeles'}))
        for hour in (8, 9):
            stamp = datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc).timestamp()
            self.assertTrue(module.quiet_now(value, stamp))

    def test_configuration_rejects_unsafe_routes_and_typos(self):
        bad = [{'enabled': 1}, {'unknown': 1}, {'screen_destinations': ['*', '201']},
               {'echo_destinations': ['223']}, {'echo_destinations': ['223', '223']},
               {'cooldown_seconds': 0}, {'sources': ['UPPER']},
               {'quiet_hours': {'start': '00:00', 'end': '00:00', 'timezone': 'UTC'}},
               {'quiet_hours': {'start': '22:00', 'end': '08:00', 'timezone': 'No/Such_Zone'}}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(module.NotificationError):
                module.validate_config(value)
        for url in ('http://example.com:18085', 'https://127.0.0.1:18085',
                    'http://user:pass@127.0.0.1:18085', 'http://127.0.0.1:18085/x'):
            with self.subTest(url=url), self.assertRaises(module.NotificationError):
                module.validate_config({'announcement': {'base_url': url, 'token': 'a' * 48}})


if __name__ == '__main__':
    unittest.main()
