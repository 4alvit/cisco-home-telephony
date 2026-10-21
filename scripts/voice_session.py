#!/usr/bin/env python3
"""Run one explicitly authorized, bounded stock-phone Assist session.

Configuration validation is the default. No microphone, network connection or
phone command is started without --run and the explicit pilot flags.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
import phone_voice_transport as transport
from phone_voice_controller import PhoneVoiceController, RtpPCMSink
from voice_assistant.config import build_service, load_config
from voice_assistant.playback import FFmpegDecoder


PROGRESS_PHASES = frozenset(('preparing', 'armed', 'listening', 'thinking', 'speaking', 'stopped', 'error'))
PROGRESS_REASONS = frozenset(('model_warmup', 'wake_ready', 'turn_complete', 'new_session',
    'user_stop', 'explicit_stop', 'display_stop', 'shutdown', 'duplicate_activation', 'call',
    'audio_backpressure', 'wake_inference_failed', 'turn_timeout', 'turn_failed',
    'playback_stop_failed', 'start_failed', 'lease_expired', 'transport_stopped',
    'receive_idle_timeout', 'assistant_error', 'audio_not_received', 'no_speech',
    'turn_cancelled', 'session_finished', 'voice_session_failed', 'speech_start',
    'end_of_speech', 'utterance_limit'))


class SessionProgress:
    """Small private snapshots, never a transcript or an alternate stdout stream."""
    def __init__(self, workspace, filename=None):
        self.workspace = Path(workspace).resolve()
        self.parts = None
        self.phase, self.reason, self.turns_completed = 'preparing', None, 0
        self.failed = False
        self.failure_reason = None
        if filename is not None:
            path = Path(filename)
            if '..' in path.parts:
                raise ValueError('private_status_path_required')
            if not path.is_absolute():
                path = self.workspace / path
            relative = path.relative_to(self.workspace / '.local')
            if len(relative.parts) < 2:
                raise ValueError('private_status_directory_required')
            self.parts = ('.local', *relative.parts)
        self.publish('preparing')

    def publish(self, phase, reason=None):
        if self.failed:
            phase, reason = 'error', self.failure_reason
        self.phase = phase if type(phase) is str and phase in PROGRESS_PHASES else 'error'
        self.reason = reason if type(reason) is str and reason in PROGRESS_REASONS else None
        if self.phase == 'error':
            self.failed = True
            self.failure_reason = self.reason
        if self.parts is None:
            return
        value = {'schema_version': 1, 'phase': self.phase, 'reason': self.reason,
                 'turns_completed': self.turns_completed, 'updated_at': time.time()}
        directory = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = None
        try:
            # Open every component relative to the preceding directory descriptor;
            # symlinks cannot redirect either traversal or atomic replacement.
            for name in self.parts[:-1]:
                next_directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                         dir_fd=directory)
                os.close(directory)
                directory = next_directory
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('private_status_directory_required')
            try:
                info = os.stat(self.parts[-1], dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise ValueError('private_status_file_required')
            name = '.progress-' + secrets.token_hex(16)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            temporary = name
            with os.fdopen(fd, 'w') as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(value, target, sort_keys=True)
                target.write('\n')
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
        finally:
            try:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=directory)
            finally:
                os.close(directory)

    def completed_reply(self):
        self.turns_completed += 1
        self.publish(self.phase, self.reason)


class ProgressPlayback:
    """Count a reply only after playback returns, never when a turn is canceled."""
    def __init__(self, playback, progress):
        self.playback, self.progress = playback, progress

    async def play(self, *args, **kwargs):
        result = await self.playback.play(*args, **kwargs)
        self.progress.completed_reply()
        return result

    async def stop(self, *args, **kwargs):
        return await self.playback.stop(*args, **kwargs)


async def run_session(args, config, private, phone, voice):
    # Correlation with the owning supervisor, not authentication of untrusted
    # code. The fixed runner and its imports remain part of the trusted runtime.
    identity = (os.environ.get('CISCO_VOICE_SESSION_ID', ''),
                os.environ.get('CISCO_VOICE_LAUNCH_CHALLENGE', ''))
    if not (re.fullmatch(r'[0-9a-f]{32}', identity[0])
            and re.fullmatch(r'[0-9a-f]{64}', identity[1])):
        identity = None
    progress = SessionProgress(args.workspace, getattr(args, 'status_file', None))
    try:
        return await _run_session(args, config, private, phone, voice, progress, identity)
    except asyncio.CancelledError:
        try:
            progress.publish('error' if progress.failed else 'stopped',
                             progress.failure_reason if progress.failed else 'shutdown')
        except Exception:
            pass
        raise
    except Exception:
        try:
            progress.publish('error')
        except Exception:
            pass
        raise


async def _run_session(args, config, private, phone, voice, progress, identity=None):
    gateway = transport._gateway_module()
    observer = transport._GatewayObserver(gateway, private, phone)
    media = transport.UdpMedia(config['bind_ipv4'], config['bind_port'], phone['ipv4'],
                               config['phone_rtp_port'], config.get('expected_source_port'),
                               max_packets=transport.packet_budget(config['duration_seconds']))
    starts = [transport.tx_uri(media.bind_ip, media.bind_port),
              transport.rx_uri(media.bind_ip, media.phone_port, config.get('volume', 20))]
    watchdog = transport._Watchdog(phone, starts)
    session = transport.VoiceSession(watchdog.execute, media, observer.status,
        duration=config['duration_seconds'], enable_microphone=True, enable_playback=True,
        volume=config.get('volume', 20), media_reserved=True, arm=watchdog.arm)
    sink = RtpPCMSink(session, phone['id'])
    states = []
    display = None
    display_ref = None
    controller = None
    stopped = threading.Event()
    captions_stopped = threading.Event()
    loop = asyncio.get_running_loop()
    progress_failed = False

    def request_stop(reason='explicit_stop'):
        captions_stopped.set()
        if controller:
            controller.request_stop(reason)

    def on_event(event):
        nonlocal progress_failed
        states.append(event.state)
        if event.reason == 'new_session':
            return
        try:
            phase = 'preparing' if event.reason == 'model_warmup' else event.state
            progress.publish(phase, event.reason)
        except Exception:
            progress_failed = True
            request_stop()
        if display and event.reason != 'new_session':
            if event.state in ('stopped', 'error'):
                captions_stopped.set()
            labels = {'armed': 'Listening', 'listening': 'Listening', 'thinking': 'Thinking',
                      'speaking': 'Speaking', 'stopped': 'Stopped', 'error': 'Error'}
            try:
                label = 'Thinking' if event.reason == 'model_warmup' else labels[event.state]
                message = ('Preparing wake detector. Please wait for Listening.'
                           if event.reason == 'model_warmup' else
                           'Wake ready. Say your wake phrase.' if event.reason == 'wake_ready' else
                           'Listening for your request.' if event.state == 'listening' else
                           progress.reason or '')
                display.update_status(label, message)
            except Exception:
                # A missing status display stops capture instead of leaving an
                # invisible listener active. Initial setup failure also aborts.
                request_stop()

    def on_reply(phone_id, stream_id, text):
        # Replies belong only to this ephemeral display, never to state events
        # or the JSON report. No fetch, push, sleep or media-lease extension.
        if (captions_stopped.is_set() or stopped.is_set() or display is None
                or display_ref is None or display.session_ref is not display_ref
                or phone_id != phone['id'] or session.state != 'active'
                or not service.owns_reply(phone_id, stream_id)):
            return
        try:
            display.update_status('Speaking', text)
        except Exception:
            request_stop()

    def display_stop(*_args):
        captions_stopped.set()
        if controller:
            loop.call_soon_threadsafe(request_stop, 'display_stop')
        return stopped.wait(5) and session.stop_confirmed

    service = build_service(voice, Path(args.workspace).resolve(), sink, on_event=on_event,
                            on_reply=(on_reply if voice.get('display_reply_text') is True
                                      and args.display_port else None),
                            compressed_decoder=FFmpegDecoder(args.ffmpeg, max_seconds=30))
    service.playback = ProgressPlayback(service.playback, progress)
    controller = PhoneVoiceController(session, service, phone['id'], duration=config['duration_seconds'],
                                      receive_idle_seconds=config.get('receive_idle_seconds', 3))
    error, result, report = None, None, {}
    cleanup_errors = []
    try:
        observer.thread.start()
        if not await asyncio.to_thread(observer.ready.wait, 5) or observer.status() != 'idle':
            raise RuntimeError('phone_not_idle')
        if args.display_port:
            from phone_voice_display import VoiceDisplayServer
            display = VoiceDisplayServer(config['bind_ipv4'], args.display_port,
                                         phone['id'], phone['ipv4'],
                                         focus=observer.status, on_stop=display_stop,
                                         request_timeout=8,
                                         ttl_seconds=max(5, math.ceil(config['duration_seconds']) + 30))
            display.start()
            ref = display.start_status('Thinking', 'Preparing voice session. Please wait for Listening.')
            display_ref = ref
            await asyncio.to_thread(gateway.execute, phone, ref.page_url, 2)
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(number, request_stop, 'shutdown')
        result = await controller.run(mode='wake' if voice['wake']['mode'] == 'onnx' else 'manual')
    except Exception:
        error = 'voice_session_failed'
    finally:
        captions_stopped.set()
        async def cleanup():
            nonlocal report
            cancelled = False
            try:
                await asyncio.to_thread(session.stop, error or 'session_finished')
            except (Exception, asyncio.CancelledError) as cleanup_error:
                cancelled |= isinstance(cleanup_error, asyncio.CancelledError)
                cleanup_errors.append('transport_cleanup_failed')
            finally:
                stopped.set()
                observer.done.set()
            try:
                await service.close()
            except (Exception, asyncio.CancelledError) as cleanup_error:
                cancelled |= isinstance(cleanup_error, asyncio.CancelledError)
                cleanup_errors.append('assistant_cleanup_failed')
            try:
                report = await asyncio.to_thread(watchdog.finish)
            except (Exception, asyncio.CancelledError) as cleanup_error:
                cancelled |= isinstance(cleanup_error, asyncio.CancelledError)
                cleanup_errors.append('supervisor_cleanup_failed')
            try:
                if display:
                    display.close()
            except (Exception, asyncio.CancelledError) as cleanup_error:
                cancelled |= isinstance(cleanup_error, asyncio.CancelledError)
                cleanup_errors.append('display_cleanup_failed')
            finally:
                for number in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(number)
            return cancelled

        # Keep the existing cleanup order and component deadlines. Cancelling
        # to_thread's await does not stop its worker; drain the one cleanup task
        # before propagating cancellation, including repeated caller requests.
        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        try:
            # Shutdown emits its own stopped event; retain the actual terminal
            # outcome after every cleanup step has finished.
            terminal_error = error or cleanup_errors or progress_failed or progress.failed
            progress.publish('error' if terminal_error else 'stopped',
                             progress.failure_reason if progress.failed else
                             'shutdown' if cancelled or cleanup_task.result() else
                             getattr(result, 'reason', progress.reason))
        except Exception:
            cleanup_errors.append('status_cleanup_failed')
        if cleanup_task.result() or cancelled:
            raise asyncio.CancelledError
    if progress.failed or progress_failed:
        error = 'voice_session_failed'
    proof = {}
    if (identity is not None and error == 'voice_session_failed' and result is None
            and not cleanup_errors and report == {'watchdog_armed': False}
            and progress.turns_completed == 0 and sink.frames_sent == 0
            and not media.receiver.counters
            and session.no_media_started(watchdog)):
        # Emitted only after all cleanup has returned. This never fabricates a
        # remote Stop acknowledgment or a successful conversation.
        proof['cleanup_proof'] = {
            'version': 1, 'kind': 'safe_no_media_started',
            'session_id': identity[0], 'challenge': identity[1],
            'rtp_start_attempted': False, 'watchdog_arm_attempted': False,
            'watchdog_process_created': False, 'cleanup_complete': True,
        }
    return {'error': error, 'result': asdict(result) if result else None, 'states': states,
            'turns_completed': progress.turns_completed,
            'capture_observed': bool(media.receiver.counters['received']),
            'input': dict(media.receiver.counters), 'output_frames': sink.frames_sent,
            'recorded_audio': False, 'cleanup_errors': cleanup_errors, **report, **proof}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport-config', required=True)
    parser.add_argument('--voice-config', required=True)
    parser.add_argument('--workspace', default=str(ROOT))
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--display-port', type=int)
    parser.add_argument('--status-file', help='private progress JSON under workspace/.local in an owned 0700 directory')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--attended', action='store_true')
    parser.add_argument('--exclusive-media', action='store_true')
    parser.add_argument('--enable-microphone', action='store_true')
    args = parser.parse_args(argv)
    try:
        config, private, phone = transport.load_pilot_config(args.transport_config)
        voice = load_config(args.voice_config)
        if args.display_port is not None and not 1024 <= args.display_port <= 65535:
            raise ValueError('invalid_display_port')
        if set(voice['phones']) != {phone['id']} or voice['phones'][phone['id']] is not True:
            raise ValueError('one_selected_phone_required')
        if not args.run:
            print(json.dumps({'valid': True, 'network_actions': 0, 'microphone_started': False}))
            return 0
        if not (args.attended and args.exclusive_media and args.enable_microphone):
            raise ValueError('explicit_pilot_flags_required')
        result = asyncio.run(run_session(args, config, private, phone, voice))
        print(json.dumps(result, sort_keys=True))
        return int(bool(result['error']) or not result.get('result') or
                   bool(result['cleanup_errors']) or
                   result.get('watchdog_armed') is not True or
                   result.get('watchdog_finished') is not True or
                   result.get('watchdog_stop_errors') != 0 or
                   result['result']['reason'] not in ('turn_complete', 'explicit_stop', 'display_stop',
                                                       'shutdown', 'lease_expired') or
                   not (result['result']['stop_confirmed'] or result.get('watchdog_stop_confirmed')))
    except Exception:
        print(json.dumps({'error': 'invalid_config_or_runtime_failure',
                          'success': False, 'automatic_retry': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
