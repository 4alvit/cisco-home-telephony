#!/usr/bin/env python3
"""Run one explicitly authorized, bounded stock-phone Assist session.

Configuration validation is the default. No microphone, network connection or
phone command is started without --run and the explicit pilot flags.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
import phone_voice_transport as transport
from phone_voice_controller import PhoneVoiceController, RtpPCMSink
from voice_assistant.config import build_service, load_config
from voice_assistant.playback import FFmpegDecoder


async def run_session(args, config, private, phone, voice):
    gateway = transport._gateway_module()
    observer = transport._GatewayObserver(gateway, private, phone)
    media = transport.UdpMedia(config['bind_ipv4'], config['bind_port'], phone['ipv4'],
                               config['phone_rtp_port'], config.get('expected_source_port'))
    starts = [transport.tx_uri(media.bind_ip, media.bind_port),
              transport.rx_uri(media.bind_ip, media.phone_port, config.get('volume', 20))]
    watchdog = transport._Watchdog(phone, starts)
    session = transport.VoiceSession(watchdog.execute, media, observer.status,
        duration=config['duration_seconds'], enable_microphone=True, enable_playback=True,
        volume=config.get('volume', 20), media_reserved=True, arm=watchdog.arm)
    sink = RtpPCMSink(session, phone['id'])
    states = []
    display = None
    controller = None
    stopped = threading.Event()
    loop = asyncio.get_running_loop()

    def on_event(event):
        states.append(event.state)
        if display and event.reason != 'new_session':
            labels = {'armed': 'Listening', 'listening': 'Listening', 'thinking': 'Thinking',
                      'speaking': 'Speaking', 'stopped': 'Stopped', 'error': 'Error'}
            try:
                label = 'Thinking' if event.reason == 'model_warmup' else labels[event.state]
                message = ('Preparing wake detector. Please wait for Listening.'
                           if event.reason == 'model_warmup' else
                           'Say your wake phrase.' if event.reason == 'wake_ready' else event.reason or '')
                display.update_status(label, message)
            except Exception:
                # A missing status display stops capture instead of leaving an
                # invisible listener active. Initial setup failure also aborts.
                if controller:
                    controller.request_stop()

    def display_stop(*_args):
        if controller:
            loop.call_soon_threadsafe(controller.request_stop, 'display_stop')
        return stopped.wait(5) and session.stop_confirmed

    service = build_service(voice, Path(args.workspace).resolve(), sink, on_event=on_event,
                            compressed_decoder=FFmpegDecoder(args.ffmpeg, max_seconds=30))
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
                                         request_timeout=8)
            display.start()
            ref = display.start_status('Thinking', 'Preparing a bounded voice session.')
            await asyncio.to_thread(gateway.execute, phone, ref.page_url, 2)
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(number, controller.request_stop, 'shutdown')
        result = await controller.run(mode='wake' if voice['wake']['mode'] == 'onnx' else 'manual')
    except Exception:
        error = 'voice_session_failed'
    finally:
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
        if cleanup_task.result() or cancelled:
            raise asyncio.CancelledError
    return {'error': error, 'result': asdict(result) if result else None, 'states': states,
            'capture_observed': bool(media.receiver.counters['received']),
            'input': dict(media.receiver.counters), 'output_frames': sink.frames_sent,
            'recorded_audio': False, 'cleanup_errors': cleanup_errors, **report}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport-config', required=True)
    parser.add_argument('--voice-config', required=True)
    parser.add_argument('--workspace', default=str(ROOT))
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--display-port', type=int)
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
                   result['result']['reason'] != 'turn_complete' or
                   not (result['result']['stop_confirmed'] or result.get('watchdog_stop_confirmed')))
    except Exception:
        print(json.dumps({'error': 'invalid_config_or_runtime_failure',
                          'success': False, 'automatic_retry': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
