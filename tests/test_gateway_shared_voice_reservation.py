"""Real shared reservations around fake gateway I/O; no phone or network use."""
from dataclasses import replace
import os
import unittest
from unittest import mock

import test_voice_wake_reservation as wake_fixture
from test_phone_gateway import FakeAMI, gateway


class GatewaySharedReservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = wake_fixture.ReservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.wake = self.fixture.manager()
        self.control = self.fixture.gateway_manager(self.wake)
        config = dict(self.fixture.gateway, dial_allowlist=["600", "204"])
        self.gateway = gateway.Gateway(config, str(self.fixture.local / "gateway-state"))
        self.addCleanup(self.gateway.db.close)
        self.addCleanup(self.gateway.release_voice_shutdown)
        self.gateway.voice = self.control
        self.gateway._voice_phone_proof = object()
        self.gateway._voice_module = wake_fixture.control
        self.gateway._voice_profiles = self.control.config.profiles
        self.network = mock.patch("socket.socket", side_effect=AssertionError("no network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.ami = mock.patch.object(gateway, "AMI", FakeAMI)
        self.ami.start()
        self.addCleanup(self.ami.stop)
        self.execute = mock.patch.object(gateway, "execute")
        self.sent = self.execute.start()
        self.addCleanup(self.execute.stop)
        FakeAMI.channels, FakeAMI.calls = [], []
        FakeAMI.endpoints = {"201": "Not in use", "202": "Not in use"}

    def test_shutdown_budget_covers_external_primary_and_uncertain_cleanup(self):
        control = wake_fixture.control
        primary = 30 + control.STOP_GRACE_SECONDS + control.RUNNER_COMPLETION_MARGIN_SECONDS
        self.assertEqual(wake_fixture.runner.UNCERTAIN_CLEANUP_SECONDS,
                         control.UNCERTAIN_CLEANUP_SECONDS)
        self.assertGreaterEqual(control.EXTERNAL_OWNER_GRACE_SECONDS,
                                primary + control.UNCERTAIN_CLEANUP_SECONDS + 8)

    def test_external_wake_cannot_start_between_idle_guard_and_phone_commands(self):
        self.assert_wake_start_is_fenced()

    def assert_wake_start_is_fenced(self):
        attempts = []
        fixture, wake = self.fixture, self.wake

        class TryWakeAtAMIEntry(FakeAMI):
            def __enter__(self):
                try:
                    fixture.start(wake)
                except wake_fixture.control.ControlError as error:
                    attempts.append(error.reason)
                return self

        with mock.patch.object(gateway, "AMI", TryWakeAtAMIEntry):
            result = self.gateway.mutate("201", "dial", {
                "line": "202", "number": "600", "idempotency_key": "shared-guard-dial"})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(attempts, ["voice_phone_busy"])
        self.assertEqual(self.fixture.launches, [])
        self.assertEqual([call.args[1] for call in self.sent.call_args_list],
                         ["Key:Line2", "Dial:600"])
        self.fixture.start(self.wake)
        self.assertEqual(len(self.fixture.launches), 1)

    def test_running_external_wake_blocks_every_direct_mutation_before_ami(self):
        self.fixture.start(self.wake)
        for action in ("dial", "speaker", "message", "image", "answer"):
            with self.subTest(action=action), mock.patch.object(gateway, "AMI") as ami:
                with self.assertRaises(gateway.Refused) as caught:
                    self.gateway.mutate("201", action, {
                        "line": "202", "idempotency_key": "external-wake-" + action})
                self.assertEqual(caught.exception.reason, "voice_phone_busy")
                ami.assert_not_called()
        self.sent.assert_not_called()

    def test_phone_exception_releases_temporary_reservation(self):
        self.sent.side_effect = OSError("synthetic timeout")
        with self.assertRaises(OSError):
            self.gateway.mutate("201", "speaker", {
                "mode": "toggle", "idempotency_key": "shared-guard-error"})
        self.fixture.start(self.wake)
        self.assertEqual(len(self.fixture.launches), 1)

    def attach_idle_supervisor(self):
        supervisor = gateway.app_module('phone_voice_hands_free').HandsFreeSupervisor(
            self.control, lambda _phone: 'idle', self.gateway._phone_voice_context, monitor=False)
        self.gateway.hands_free = supervisor
        self.gateway.dnd_pending.clear()
        self.addCleanup(supervisor.close)
        return supervisor

    def test_motion_image_fences_external_start_through_display_io(self):
        self.attach_idle_supervisor()
        attempts = []

        def display(_phone, action, body):
            self.assertEqual(action, 'image')
            self.assertEqual(body['idle_close_seconds'], 45)
            try:
                self.fixture.start(self.wake)
            except wake_fixture.control.ControlError as error:
                attempts.append(error.reason)

        with mock.patch.object(self.gateway, 'notify', side_effect=display):
            result = self.gateway.mutate('201', 'motion_image', {
                'idempotency_key': 'motion-reserved'})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(attempts, ['voice_phone_busy'])
        self.assertEqual(self.fixture.launches, [])
        self.fixture.start(self.wake)
        self.assertEqual(len(self.fixture.launches), 1)

    def test_motion_image_and_admission_refuse_active_external_voice(self):
        self.attach_idle_supervisor()
        self.fixture.start(self.wake)
        self.assertEqual(self.gateway.motion_image_admission(self.gateway.phones['201']),
                         {'eligible': False, 'reason': 'voice_phone_busy'})
        with mock.patch.object(gateway, 'AMI') as ami:
            with self.assertRaises(gateway.Refused) as caught:
                self.gateway.mutate('201', 'motion_image', {'idempotency_key': 'motion-external-busy'})
        self.assertEqual(caught.exception.reason, 'voice_phone_busy')
        ami.assert_not_called()
        self.sent.assert_not_called()

    def test_motion_display_failure_releases_both_ownership_fences(self):
        supervisor = self.attach_idle_supervisor()
        with mock.patch.object(self.gateway, 'notify', side_effect=OSError('synthetic timeout')):
            with self.assertRaises(OSError):
                self.gateway.mutate('201', 'motion_image', {'idempotency_key': 'motion-failed-display'})
        self.assertIsNone(supervisor._entries['201'].fd)
        self.assertEqual(supervisor._entries['201'].suspended, 0)
        self.fixture.start(self.wake)
        self.assertEqual(len(self.fixture.launches), 1)

    def add_destination(self):
        profile = self.control.config.profiles[0]
        second = replace(profile, phone_id="203", lines=("203", "204"),
                         device="SEP020000000002", ipv4="192.168.50.21", bind_port=24580,
                         display_port=18091)
        config = replace(self.control.config, profiles=(profile, second))
        self.control = wake_fixture.control.VoiceControl(
            config, authorize=lambda *_: True, monitor=False,
            runner_factory=lambda *_a, **_k: self.fail("no control child"))
        self.fixture.managers.append(self.control)
        self.gateway.voice = self.control
        self.gateway._voice_profiles = config.profiles
        self.gateway.phones["203"] = dict(self.gateway.phones["201"], id="203",
                                         lines=["203", "204"], device=second.device,
                                         ipv4=second.ipv4)
        FakeAMI.endpoints.update({"203": "Not in use", "204": "Not in use"})
        return profile, second

    def make_source_control_only(self):
        source, target = self.add_destination()
        config = replace(self.control.config, profiles=(target,))
        identities = tuple(wake_fixture.control.ControlPhone(p.phone_id, p.lines, p.device)
                           for p in (source, target))
        self.control = wake_fixture.control.VoiceControl(
            config, authorize=lambda *_: True, monitor=False, control_phones=identities,
            runner_factory=lambda *_a, **_k: self.fail("no control child"))
        self.fixture.managers.append(self.control)
        self.gateway.voice = self.control
        self.gateway._voice_profiles = config.profiles
        self.assertNotIn("201", {p.phone_id for p in self.gateway._voice_profiles})

    def test_control_only_handset_cannot_start_external_wake_during_direct_io(self):
        self.make_source_control_only()
        self.assert_wake_start_is_fenced()

    def test_control_only_handset_with_external_wake_blocks_direct_io(self):
        self.make_source_control_only()
        self.fixture.start(self.wake)
        with mock.patch.object(gateway, "AMI") as ami:
            with self.assertRaises(gateway.Refused) as caught:
                self.gateway.mutate("201", "dial", {
                    "number": "600", "idempotency_key": "unprofiled-external-wake"})
        self.assertEqual(caught.exception.reason, "voice_phone_busy")
        ami.assert_not_called()
        self.sent.assert_not_called()
        with self.assertRaisesRegex(wake_fixture.control.ControlError, "unknown_voice_phone"):
            self.fixture.start(self.control)

    def test_shutdown_waits_full_external_grace_for_control_only_wake(self):
        self.make_source_control_only()
        self.fixture.start(self.wake)
        grace = wake_fixture.control.EXTERNAL_OWNER_GRACE_SECONDS
        with mock.patch.object(gateway.time, "monotonic", side_effect=(0, 0, grace - 1, grace + 1)), \
             mock.patch.object(gateway.time, "sleep") as sleep, \
             mock.patch.object(self.control, "reserve_shutdown", wraps=self.control.reserve_shutdown) as reserve:
            self.assertFalse(self.gateway.close_voice())
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual({call.args[0] for call in reserve.call_args_list}, {"201", "203"})
        self.assertEqual(self.wake.status("201", context=None)["state"], "running")
        self.assertEqual(self.fixture.children[0].terminations, 0)
        self.assertEqual(self.fixture.children[0].kills, 0)

    def test_shutdown_waits_for_external_lock_without_a_journal(self):
        self.make_source_control_only()
        fd = self.wake._acquire(self.wake.inputs.profile)
        try:
            self.assertEqual(list(self.fixture.state.glob("*.json")), [])
            grace = wake_fixture.control.EXTERNAL_OWNER_GRACE_SECONDS
            with mock.patch.object(gateway.time, "monotonic", side_effect=(0, 0, grace - 1, grace + 1)), \
                 mock.patch.object(gateway.time, "sleep") as sleep:
                self.assertFalse(self.gateway.close_voice())
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(self.fixture.launches, [])
            self.assertEqual(list(self.fixture.state.glob("*.json")), [])
        finally:
            os.close(fd)

    def test_shutdown_exits_promptly_for_idle_full_inventory(self):
        self.make_source_control_only()
        with mock.patch.object(gateway.time, "monotonic", side_effect=(0, 0)), \
             mock.patch.object(gateway.time, "sleep") as sleep:
            self.assertTrue(self.gateway.close_voice())
        sleep.assert_not_called()
        self.assertEqual(self.gateway._voice_shutdown_phones, {"201", "203"})
        self.assertEqual(list(self.fixture.state.glob("*.json")), [])
        with self.assertRaisesRegex(wake_fixture.control.ControlError, "voice_phone_busy"):
            self.fixture.start(self.wake)
        self.gateway.release_voice_shutdown()
        self.fixture.start(self.wake)
        self.assertEqual(len(self.fixture.launches), 1)

    def test_shutdown_can_exit_after_external_cleanup_is_confirmed(self):
        self.make_source_control_only()
        self.fixture.start(self.wake)
        with mock.patch.object(gateway.time, "monotonic", side_effect=(0, 0, 1)), \
             mock.patch.object(gateway.time, "sleep",
                               side_effect=lambda _: self.fixture.finish(self.wake)) as sleep:
            self.assertTrue(self.gateway.close_voice())
        sleep.assert_called_once_with(0.05)
        self.assertEqual(self.wake.status("201", context=None)["state"], "stopped")
        self.assertEqual(self.fixture.children[0].terminations, 0)

    def test_shutdown_retains_idle_phone_fence_while_another_owner_drains(self):
        self.make_source_control_only()
        self.fixture.start(self.wake)
        target = self.control.config.profiles[0]
        checks = []

        def during_wait(_duration):
            with self.assertRaisesRegex(wake_fixture.control.ControlError, "voice_phone_busy"):
                self.wake._acquire(target)
            checks.append(True)

        grace = wake_fixture.control.EXTERNAL_OWNER_GRACE_SECONDS
        with mock.patch.object(gateway.time, "monotonic", side_effect=(0, 0, grace + 1)), \
             mock.patch.object(gateway.time, "sleep", side_effect=during_wait):
            self.assertFalse(self.gateway.close_voice())
        self.assertEqual(checks, [True])
        with self.assertRaisesRegex(wake_fixture.control.ControlError, "voice_phone_busy"):
            self.wake._acquire(target)

    def assert_main_retains_fences_through_listener_close(self, *, fail_close=False):
        self.make_source_control_only()
        config_path = self.fixture.local / "main-config.json"
        self.fixture.write(config_path, self.gateway.config)
        closed = []
        listeners = [mock.Mock() for _ in range(1 + len(self.gateway.phones))]

        def close_listener(index):
            with self.assertRaisesRegex(wake_fixture.control.ControlError, "voice_phone_busy"):
                self.fixture.start(self.wake)
            closed.append(index)
            if fail_close and index == 0:
                raise RuntimeError("synthetic listener close failure")

        for index, listener in enumerate(listeners):
            listener.server_close.side_effect = lambda index=index: close_listener(index)
        with mock.patch.object(gateway, "Gateway", return_value=self.gateway), \
             mock.patch.object(gateway, "ThreadingHTTPServer", side_effect=listeners), \
             mock.patch.object(gateway.threading, "Thread"), \
             mock.patch.object(gateway.threading, "Event"), \
             mock.patch.object(gateway.signal, "signal"), \
             mock.patch.object(gateway.os, "umask"), \
             mock.patch.object(gateway.time, "monotonic", side_effect=(0, 0)), \
             mock.patch.object(gateway.sys, "argv", [
                 "gateway", "--config", str(config_path),
                 "--state-directory", str(self.fixture.local / "gateway-state")]):
            if fail_close:
                with self.assertRaisesRegex(RuntimeError, "synthetic listener close failure"):
                    gateway.main()
            else:
                gateway.main()
        self.assertEqual(closed, list(range(len(listeners))))
        self.assertEqual(self.gateway._voice_shutdown_phones, set())
        self.fixture.start(self.wake)
        self.assertEqual(len(self.fixture.launches), 1)

    def test_main_keeps_fences_through_final_listener_close(self):
        self.assert_main_retains_fences_through_listener_close()

    def test_main_closes_remaining_listeners_and_releases_fences_after_close_error(self):
        self.assert_main_retains_fences_through_listener_close(fail_close=True)

    def test_intercom_holds_both_reservations_through_the_complete_operation(self):
        profiles = self.add_destination()
        inspected = []

        def operation(*_args):
            for profile in profiles:
                with self.assertRaisesRegex(wake_fixture.control.ControlError, "voice_phone_busy"):
                    self.wake._acquire(profile)
                inspected.append(profile.phone_id)
            return {"connection_state": "connected"}

        with mock.patch.object(self.gateway, "_intercom_reserved", side_effect=operation):
            result = self.gateway.mutate("201", "intercom", {
                "line": "202", "number": "204", "idempotency_key": "shared-intercom-both"})
        self.assertEqual(result["connection_state"], "connected")
        self.assertEqual(inspected, ["201", "203"])
        for profile in profiles:
            fd = self.wake._acquire(profile)
            os.close(fd)

    def test_busy_intercom_destination_releases_source_without_origination(self):
        source, target = self.add_destination()
        fd = self.wake._acquire(target)
        try:
            with self.assertRaises(gateway.Refused) as caught:
                self.gateway.mutate("201", "intercom", {
                    "number": "204", "idempotency_key": "shared-intercom-blocked"})
            self.assertEqual(caught.exception.reason, "voice_phone_busy")
            self.assertEqual(FakeAMI.calls, [])
            self.sent.assert_not_called()
            source_fd = self.wake._acquire(source)
            os.close(source_fd)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
