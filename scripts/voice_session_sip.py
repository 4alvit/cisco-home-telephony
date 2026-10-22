#!/usr/bin/env python3
"""One opt-in SIP/AudioSocket wake session owned by the existing gateway.

Only the VoiceControl parent supplies the inherited lease descriptor and session
identity. The listener is loopback-only and must share the PBX host namespace.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'containers/asterisk'), str(ROOT / 'scripts')]
import phone_voice_control as control
import phone_voice_transport as transport
from phone_voice_sip import BoundaryError, Session, Wire
from phone_voice_sip_ami import AmiPort, LeaseWitness
from voice_session import SessionProgress, ProgressPlayback
from voice_assistant.config import build_service
from voice_assistant.playback import FFmpegDecoder


class DeferredSink:
    def __init__(self):
        self.target = None

    async def write_pcm(self, *args):
        if self.target is None:
            raise RuntimeError('sip_media_unavailable')
        return await self.target.write_pcm(*args)

    async def stop(self, *args):
        if self.target is not None:
            return await self.target.stop(*args)


def startup_diagnostic(error, stage, port=None, *, reason=None):
    """Only fixed categories cross the private child-report boundary."""
    gateway_error = getattr(getattr(port, 'gateway', None), 'Refused', ())
    if isinstance(error, BoundaryError):
        kind = 'BoundaryError'
        reason = error.args[0] if len(error.args) == 1 else None
    elif isinstance(error, gateway_error):
        kind, reason = 'Refused', error.reason
    elif isinstance(error, TimeoutError):
        kind = 'TimeoutError'
    elif isinstance(error, OSError):
        kind = 'OSError'
    elif isinstance(error, RuntimeError):
        kind = 'RuntimeError'
    else:
        kind = 'other'

    def fixed(value, allowed):
        return value if type(value) is str and value in allowed else 'other'

    return {
        'version': 1,
        'reason': fixed(reason, control.STARTUP_DIAGNOSTIC_REASONS),
        'error_type': kind,
        'stage': fixed(stage, control.STARTUP_DIAGNOSTIC_STAGES),
        'ami_stage': fixed(getattr(port, 'startup_stage', 'not_started'),
                           control.STARTUP_DIAGNOSTIC_AMI_STAGES),
        'ami_action': fixed(getattr(error, 'ami_action', None),
                            control.STARTUP_DIAGNOSTIC_AMI_ACTIONS),
        'ami_error': fixed(getattr(error, 'ami_error', None),
                           control.STARTUP_DIAGNOSTIC_AMI_ERRORS),
    }


async def run(args, config, private, phone, voice):
    token = UUID(hex=args.session_id)
    # Bind a private cleanup report to this trusted child launch; this is
    # correlation, not authentication of arbitrary executable code.
    identity = (os.environ.get('CISCO_VOICE_SESSION_ID', ''),
                os.environ.get('CISCO_VOICE_LAUNCH_CHALLENGE', ''))
    if not (identity[0] == token.hex and re.fullmatch(r'[0-9a-f]{64}', identity[1])):
        identity = None
    witness = LeaseWitness(args.lease_fd, args.state_directory, phone, token)
    witness.verify()
    progress = SessionProgress(args.workspace, args.status_file)
    port = AmiPort(transport._gateway_module(), private, phone, config['line'], token,
                   args.lease_expires_unix, config['bind_port'], witness)
    sink = DeferredSink()
    session = service = server = wire = None
    connections = []
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    accepted = loop.create_future()
    report = {'transport': 'sip_audiosocket', 'session_id': token.hex,
              'recorded_audio': False, 'error': None, 'result': None,
              'cleanup_errors': [], 'turns_completed': 0}
    stop_waiter = None
    cancelled = False
    stop_signalled = startup_stopped = rejected_connection = False
    signal_handlers = []
    stage, failure_reason = 'service_build', None

    def cleanup_error(reason):
        if reason not in report['cleanup_errors']:
            report['cleanup_errors'].append(reason)

    def abort_connection(writer):
        try:
            writer.transport.abort()
        except Exception:
            cleanup_error('connection_abort_failed')

    def close_connection(writer, *, abort=False):
        try:
            writer.close()
        except Exception:
            cleanup_error('connection_close_failed')
            abort = True
        if abort:
            abort_connection(writer)

    def stop():
        stopped.set()
        port.stop_event.set()
        if session is not None:
            session.request_stop()

    def signal_stop():
        nonlocal stop_signalled
        stop_signalled = True
        stop()

    def event(value):
        if value.reason != 'new_session':
            try:
                progress.publish('preparing' if value.reason == 'model_warmup' else value.state,
                                 value.reason)
            except Exception:
                stop()

    def connected(reader, writer):
        nonlocal rejected_connection
        # No task is spawned, and a second peer never replaces the first one.
        if (accepted.done() or stopped.is_set() or
                writer.get_extra_info('peername', (None,))[0] != '127.0.0.1'):
            rejected_connection = True
            # These transports belong to this listener but never to the media
            # session. Abort promptly rather than retain an unbounded peer list.
            close_connection(writer, abort=True)
            return
        connections.append(writer)
        accepted.set_result((reader, writer))

    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(number, signal_stop)
            signal_handlers.append(number)
        # ONNX construction happens before ringing or listening sockets.
        service = build_service(voice, Path(args.workspace).resolve(), sink,
                                on_event=event, compressed_decoder=FFmpegDecoder(args.ffmpeg, max_seconds=30))
        service.playback = ProgressPlayback(service.playback, progress)
        stage = 'authorize_phone'
        service.ensure_allowed(phone['id'])
        if stopped.is_set():
            failure_reason = 'stopped_before_start'
            raise RuntimeError(failure_reason)
        # Renew on the same port after closed connections enter TIME_WAIT.
        # SO_REUSEPORT stays disabled: an active listener must remain exclusive.
        stage = 'listener_bind'
        server = await asyncio.start_server(connected, '127.0.0.1', config['bind_port'],
                                            limit=8192, backlog=1, reuse_address=True,
                                            reuse_port=False)
        stage = 'sip_start'
        grant = await port.start()
        if stopped.is_set():
            failure_reason = 'stopped_during_start'
            raise RuntimeError(failure_reason)
        stage = 'peer_accept'
        stop_waiter = asyncio.create_task(stopped.wait())
        done, _ = await asyncio.wait((accepted, stop_waiter),
            timeout=max(0, min(3, grant.deadline - time.monotonic())),
            return_when=asyncio.FIRST_COMPLETED)
        if stopped.is_set():
            failure_reason = 'stopped_before_session'
            raise RuntimeError(failure_reason)
        if accepted not in done:
            failure_reason = 'accept_timeout'
            raise TimeoutError(failure_reason)
        reader, writer = accepted.result()
        server.close()
        # wait_closed can wait for this accepted transport too. Consume it
        # before waiting for server closure, otherwise both sides can deadlock.
        stage = 'wire_session'
        wire = Wire(reader, writer, token)
        session = Session(wire, grant, port)
        sink.target = session.sink
        report['result'] = asdict(await session.run(service))
        report['cleanup_errors'].extend(report['result']['cleanup_errors'])
        if report['result']['reason'] not in control.SIP_CLEAN_REASONS:
            report['error'] = 'voice_session_failed'
    except asyncio.CancelledError:
        cancelled = True
    except Exception as error:
        report['error'] = 'voice_session_failed'
        startup_stopped = (stop_signalled and stage == 'sip_start'
                           and isinstance(error, BoundaryError) and bool(error.args)
                           and type(error.args[0]) is str and error.args[0] == 'explicit_stop')
        # Capture the first cause before cleanup changes the port state.
        report['startup_diagnostic'] = startup_diagnostic(error, stage, port, reason=failure_reason)
    finally:
        stop()

        async def cleanup():
            if not accepted.done():
                accepted.cancel()
            if stop_waiter is not None:
                stop_waiter.cancel()
            if server is not None:
                try:
                    server.close()
                except Exception:
                    cleanup_error('listener_cleanup_failed')
            for writer in connections:
                close_connection(writer)
            if stop_waiter is not None:
                await asyncio.gather(stop_waiter, return_exceptions=True)
            for writer in connections:
                try:
                    await asyncio.wait_for(writer.wait_closed(), .5)
                except (Exception, asyncio.CancelledError):
                    cleanup_error('connection_cleanup_failed')
                    abort_connection(writer)
            if server is not None:
                try:
                    await asyncio.wait_for(server.wait_closed(), .5)
                except (Exception, asyncio.CancelledError):
                    cleanup_error('listener_cleanup_failed')
                    for writer in connections:
                        abort_connection(writer)
            try:
                if service is not None:
                    await asyncio.wait_for(service.close(), 2)
            except (Exception, asyncio.CancelledError):
                cleanup_error('assistant_cleanup_failed')
            # Session.run already performs this exact cleanup. If setup failed,
            # reconcile this operation's root/phone identities here instead.
            if report['result'] is None:
                try:
                    await asyncio.wait_for(port.hangup_and_confirm(), 5)
                except (Exception, asyncio.CancelledError):
                    cleanup_error('pbx_cleanup_failed')

        cleanup_task = asyncio.create_task(cleanup())
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_task.result()
        report.update(originate_attempted=port.originate_attempted,
                      speaker_attempted=port.speaker_attempted,
                      pbx_deadline_armed=port.deadline_armed,
                      pbx_call_absent=port.absence_confirmed,
                      safe_no_media_started=not port.originate_attempted,
                      turns_completed=progress.turns_completed,
                      input_frames=wire.frames_received if wire else 0,
                      output_frames=wire.frames_sent if wire else 0)
        if session is not None:
            report['silence_frames'] = session.sink.silence_frames
        reason = (report['result'] or {}).get('reason')
        public_reason = {'call_changed': 'call', 'call_preempted': 'call', 'peer_ended': 'transport_stopped',
                         'dtmf_stop': 'explicit_stop'}.get(reason, reason)
        if cancelled:
            public_reason = 'shutdown'
        failed = bool(report['error'] or report['cleanup_errors'] or not port.absence_confirmed)
        try:
            progress.publish('error' if failed else 'stopped', public_reason)
        except Exception:
            report['cleanup_errors'].append('status_cleanup_failed')
        for number in signal_handlers:
            loop.remove_signal_handler(number)
    if cancelled:
        raise asyncio.CancelledError
    if (identity is not None and startup_stopped and session is None and wire is None
            and not rejected_connection and report['result'] is None
            and report['cleanup_errors'] == [] and report['pbx_call_absent'] is True
            and report['turns_completed'] == 0 and report['input_frames'] == 0
            and report['output_frames'] == 0 and port.seal_startup_stop()):
        report['cleanup_proof'] = {
            'version': 1, 'kind': 'safe_sip_startup_stop', 'transport': 'sip_audiosocket',
            'session_id': identity[0], 'challenge': identity[1],
            'originate_attempted': True, 'startup_drained': True, 'startup_sealed': True,
            'phone_seen': True, 'cleanup_complete': True,
        }
        return report
    if (identity is not None and report['error'] == 'voice_session_failed'
            and report['result'] is None and report['cleanup_errors'] == []
            and not connections and wire is None and report['pbx_call_absent'] is True
            and report['turns_completed'] == 0 and report['input_frames'] == 0
            and report['output_frames'] == 0 and port.seal_no_originate()):
        report['cleanup_proof'] = {
            'version': 1, 'kind': 'safe_no_sip_originate', 'transport': 'sip_audiosocket',
            'session_id': identity[0], 'challenge': identity[1],
            'originate_attempted': False, 'cleanup_complete': True,
        }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('transport-config', 'voice-config', 'workspace', 'state-directory', 'session-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--lease-fd', type=int, required=True)
    parser.add_argument('--lease-expires-unix', type=float, required=True)
    parser.add_argument('--status-file', required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    for flag in ('run', 'attended', 'exclusive-media', 'enable-microphone'):
        parser.add_argument('--' + flag, action='store_true')
    args = parser.parse_args()
    stage = 'inputs_or_initialization'
    try:
        config, private, phone, voice, _ = control._inputs(args.transport_config, args.voice_config, args.workspace)
        if config.get('media_transport') != 'sip_audiosocket':
            raise ValueError('sip_transport_not_enabled')
        if not (args.run and args.attended and args.exclusive_media and args.enable_microphone):
            print(json.dumps({'valid': True, 'network_actions': 0, 'microphone_started': False}))
            return 0
        stage = 'runner_unhandled'
        result = asyncio.run(run(args, config, private, phone, voice))
        print(json.dumps(result, sort_keys=True))
        return 1 if result['error'] or result['cleanup_errors'] else 0
    except Exception as error:
        # Input/lease initialization can fail before run creates its full report.
        result = {'error': 'voice_session_failed', 'transport': 'sip_audiosocket',
                  'startup_diagnostic': startup_diagnostic(error, stage)}
        try:
            result['session_id'] = UUID(hex=args.session_id).hex
        except (ValueError, AttributeError):
            pass
        print(json.dumps(result, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
