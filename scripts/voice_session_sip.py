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
from phone_voice_sip import Session, Wire
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

    def stop():
        stopped.set()
        port.stop_event.set()
        if session is not None:
            session.request_stop()

    def event(value):
        if value.reason != 'new_session':
            try:
                progress.publish('preparing' if value.reason == 'model_warmup' else value.state,
                                 value.reason)
            except Exception:
                stop()

    def connected(reader, writer):
        # No task is spawned, and a second peer never replaces the first one.
        if (accepted.done() or stopped.is_set() or
                writer.get_extra_info('peername', (None,))[0] != '127.0.0.1'):
            writer.close()
            return
        connections.append(writer)
        accepted.set_result((reader, writer))

    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop)
    try:
        # ONNX construction happens before ringing or listening sockets.
        service = build_service(voice, Path(args.workspace).resolve(), sink,
                                on_event=event, compressed_decoder=FFmpegDecoder(args.ffmpeg, max_seconds=30))
        service.playback = ProgressPlayback(service.playback, progress)
        service.ensure_allowed(phone['id'])
        if stopped.is_set():
            raise RuntimeError('stopped_before_start')
        server = await asyncio.start_server(connected, '127.0.0.1', config['bind_port'],
                                            limit=8192, backlog=1, reuse_address=False)
        grant = await port.start()
        if stopped.is_set():
            raise RuntimeError('stopped_during_start')
        reader, writer = await asyncio.wait_for(accepted, min(3, grant.deadline - time.monotonic()))
        server.close()
        await server.wait_closed()
        wire = Wire(reader, writer, token)
        session = Session(wire, grant, port)
        sink.target = session.sink
        report['result'] = asdict(await session.run(service))
        report['cleanup_errors'].extend(report['result']['cleanup_errors'])
        if report['result']['reason'] not in ('explicit_stop', 'lease_expired', 'shutdown',
                                             'dtmf_stop', 'peer_ended', 'call_changed'):
            report['error'] = 'voice_session_failed'
    except Exception:
        report['error'] = 'voice_session_failed'
    finally:
        stop()
        if server is not None:
            server.close()
            await server.wait_closed()
        for writer in connections:
            writer.close()
        try:
            if service is not None:
                await service.close()
        except Exception:
            report['cleanup_errors'].append('assistant_cleanup_failed')
        # Session.run already performs this exact cleanup. If setup failed,
        # reconcile this operation's root/phone identities here instead.
        if report['result'] is None:
            try:
                await port.hangup_and_confirm()
            except Exception:
                report['cleanup_errors'].append('pbx_cleanup_failed')
        report.update(originate_attempted=port.originate_attempted,
                      speaker_attempted=port.speaker_attempted,
                      pbx_deadline_armed=port.deadline_armed,
                      pbx_call_absent=port.absence_confirmed,
                      safe_no_media_started=not port.originate_attempted,
                      turns_completed=progress.turns_completed,
                      input_frames=wire.frames_received if wire else 0,
                      output_frames=wire.frames_sent if wire else 0)
        reason = (report['result'] or {}).get('reason')
        public_reason = {'call_changed': 'call', 'peer_ended': 'transport_stopped',
                         'dtmf_stop': 'explicit_stop'}.get(reason, reason)
        failed = bool(report['error'] or report['cleanup_errors'] or not port.absence_confirmed)
        try:
            progress.publish('error' if failed else 'stopped', public_reason)
        except Exception:
            report['cleanup_errors'].append('status_cleanup_failed')
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(number)
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
    try:
        config, private, phone, voice, _ = control._inputs(args.transport_config, args.voice_config, args.workspace)
        if config.get('media_transport') != 'sip_audiosocket':
            raise ValueError('sip_transport_not_enabled')
        if not (args.run and args.attended and args.exclusive_media and args.enable_microphone):
            print(json.dumps({'valid': True, 'network_actions': 0, 'microphone_started': False}))
            return 0
        result = asyncio.run(run(args, config, private, phone, voice))
        print(json.dumps(result, sort_keys=True))
        return 1 if result['error'] or result['cleanup_errors'] else 0
    except Exception:
        print(json.dumps({'error': 'voice_session_failed', 'transport': 'sip_audiosocket'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
