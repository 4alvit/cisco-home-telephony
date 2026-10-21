"""Opt-in SIP call admission using the existing bounded gateway AMI client."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import UUID

from phone_voice_sip import BoundaryError, Channel, Grant, Snapshot, focus


class LeaseWitness:
    """Validate the descriptor inherited from the existing VoiceControl owner.

    This neither creates nor releases ownership. The manager remains responsible
    for the durable journal, child signalling and post-exit reconciliation.
    """
    def __init__(self, fd, directory, phone, session):
        self.fd, self.directory, self.phone, self.session = fd, Path(directory), phone, session
        key = hashlib.sha256(phone['device'].encode()).hexdigest()
        self.path, self.journal = self.directory / (key + '.lock'), self.directory / (key + '.json')

    def verify(self):
        info = os.fstat(self.fd)
        path = self.path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (path.st_dev, path.st_ino)):
            raise BoundaryError('lease_identity_changed')
        probe = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise BoundaryError('lease_not_held')
        finally:
            os.close(probe)
        fd = os.open(self.journal, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            ji = os.fstat(source.fileno())
            if not stat.S_ISREG(ji.st_mode) or ji.st_uid != os.getuid() or ji.st_mode & 0o077 or ji.st_size > 16384:
                raise BoundaryError('lease_journal_invalid')
            value = json.load(source)
        if (value.get('session_id') != self.session.hex or value.get('phone') != self.phone['id']
                or value.get('reserved') is not True or value.get('state') not in ('starting', 'running', 'stopping')):
            raise BoundaryError('lease_journal_changed')


class AmiPort:
    """One SIP call, one speaker attempt, no preference changes or retries."""
    def __init__(self, gateway, private, phone, line, session, expires_unix, port, witness,
                 *, clock=time.monotonic, wall_clock=time.time):
        if (line not in phone['lines'] or type(session) is not UUID or type(port) is not int
                or not 1024 <= port <= 65535 or not math.isfinite(expires_unix)):
            raise ValueError('invalid_sip_admission')
        remaining = expires_unix - wall_clock()
        if not 0 < remaining <= 300:
            raise ValueError('invalid_sip_deadline')
        self.gateway, self.private, self.phone, self.line = gateway, private, phone, line
        self.session, self.port, self.witness = session, port, witness
        self.clock, self.wall_clock = clock, wall_clock
        self.created = clock()
        self.deadline = self.created + remaining
        self.expires = int(expires_unix)  # round downward; never extend the grant
        self.root = 'cisco-voice-' + session.hex
        self.roots = {self.root, self.root + '-b'}
        self.stop_event = threading.Event()
        self.thread = None
        self.observation = None
        self.grant = None
        self.originate_attempted = self.seen_phone = self.deadline_armed = False
        self.speaker_attempted = self.used = False
        self.start_attempted = False
        self.startup_stage = 'not_started'
        self.absence_confirmed = False
        self._lifecycle_lock = threading.RLock()
        self._start_active = self._sealed = False

    def _guard(self):
        if self._sealed or self.stop_event.is_set():
            raise BoundaryError('explicit_stop')
        if self.clock() >= self.deadline:
            raise BoundaryError('lease_expired')
        self.witness.verify()

    def _related(self, channels):
        return [item for item in channels if self.roots.intersection(
            (item.get('Uniqueid'), item.get('Linkedid')))]

    def _snapshot(self, ami):
        started = self.clock()
        endpoints, channels = ami.snapshot()
        own = self.gateway.own_channels(self.phone, channels)
        known = {'Not in use', 'In use', 'Busy', 'Ringing', 'Ring+Inuse', 'On Hold'}
        registered = all(endpoints.get(line) in known for line in self.phone['lines'])
        conflicting = any(endpoints.get(line) in {'On Hold', 'Busy', 'Ringing', 'Ring+Inuse'}
                          for line in self.phone['lines'])
        self.observation = Snapshot(self.phone['id'], started, registered,
            tuple(Channel(item.get('Channel', ''), item.get('Uniqueid', ''),
                          item.get('Channel', '').split('/')[1].split('-')[0]) for item in own),
            frozenset(line for line in self.phone['lines'] if endpoints.get(line) != 'Not in use'),
            conflicting_call_state=conflicting)
        if self.clock() - started > 1:
            raise BoundaryError('snapshot_stale')
        return endpoints, channels, own

    @staticmethod
    def _getvar(ami, variable, uniqueid=None):
        headers = {'Variable': variable}
        if uniqueid is not None:
            headers['Channel'] = uniqueid
        result = ami.action('Getvar', **headers)
        if len(result) != 1 or 'Value' not in result[0]:
            raise BoundaryError('pbx_proof_unavailable')
        return result[0]['Value']

    def _verify_channel(self, ami, item):
        self.startup_stage = 'verify_identity'
        uid = item.get('Uniqueid', '')
        if (not uid or item not in self._related([item]) or
                not re.fullmatch(r'PJSIP/' + re.escape(self.line) + r'-[0-9a-f]+', item.get('Channel', ''))):
            raise BoundaryError('call_identity_changed')
        self.startup_stage = 'verify_session'
        if (self._getvar(ami, 'VOICE_SIP_SESSION', uid) != self.session.hex or
                self._getvar(ami, 'VOICE_SIP_END', uid) != str(self.expires)):
            raise BoundaryError('call_admission_mismatch')
        self.startup_stage = 'verify_deadline'
        try:
            observed_at = self.clock()
            seconds = float(self._getvar(ami, 'TIMEOUT(absolute)', uid))
        except ValueError:
            raise BoundaryError('pbx_deadline_unverified') from None
        if not math.isfinite(seconds) or not 0 < seconds <= self.deadline - observed_at + .05:
            raise BoundaryError('pbx_deadline_unverified')
        self.deadline_armed = True

    def _start(self):
        self.startup_stage = 'guard'
        self._guard()
        self.startup_stage = 'login'
        with self.gateway.AMI(self.private['ami']) as ami:
            self.startup_stage = 'idle_snapshot'
            endpoints, channels, own = self._snapshot(ami)
            if own or any(endpoints.get(line) != 'Not in use' for line in self.phone['lines']):
                raise BoundaryError('phone_not_idle')
            self.startup_stage = 'dnd'
            for line in self.phone['lines']:
                # DB() through Getvar is an escalating function in Asterisk;
                # use the existing SYSTEM-authorized read action instead.
                rows = ami.action('DBGet', complete='DBGetComplete', Family='cisco-dnd', Key=line)
                values = [item.get('Val') for item in rows if item.get('Event') == 'DBGetResponse'
                          and item.get('Family') == 'cisco-dnd' and item.get('Key') == line]
                if values != ['0']:
                    raise BoundaryError('do_not_disturb_or_unknown')
            # Seal and final dispatch intent are atomic; never hold this lock
            # across the network call. A queued start cannot outlive its proof.
            self.startup_stage = 'guard'
            with self._lifecycle_lock:
                self._guard()
                self.originate_attempted = True  # sticky BEFORE ambiguous network I/O
            self.startup_stage = 'originate'
            ami.action('Originate', Channel='Local/' + self.line + '@cisco-voice-sip/n',
                ChannelId=self.root, OtherChannelId=self.root + '-b',
                Application='Dial', Data=f'AudioSocket/127.0.0.1:{self.port}/{self.session}/c(slin16),3,g',
                CallerID='Voice assistant <' + self.line + '>', Timeout='8000', Async='true',
                Variable='__VOICE_SIP_SESSION=' + self.session.hex + ',__VOICE_SIP_END=' + str(self.expires))
            setup_end = min(self.deadline, self.clock() + 10)
            while self.clock() < setup_end:
                self.startup_stage = 'guard'
                self._guard()
                self.startup_stage = 'setup_snapshot'
                _, channels, own = self._snapshot(ami)
                if len(own) > 1 or any(item not in self._related(channels) for item in own):
                    raise BoundaryError('call_changed')
                if own:
                    item = own[0]
                    self.seen_phone = True
                    # An allocated channel can precede the pre-dial handler
                    # that arms its native timeout. Wait for an answerable
                    # state before requiring that handler's admission proof.
                    if item.get('ChannelStateDesc') not in ('Ringing', 'Up'):
                        self.stop_event.wait(.1)
                        continue
                    self._verify_channel(ami, item)
                    if item.get('ChannelStateDesc') == 'Ringing' and not self.speaker_attempted:
                        self.startup_stage = 'guard'
                        self._guard()
                        # Re-read after all proof queries before the one key press.
                        self.startup_stage = 'speaker_snapshot'
                        _, _, fresh = self._snapshot(ami)
                        if len(fresh) != 1 or fresh[0].get('Uniqueid') != item['Uniqueid'] or fresh[0].get('ChannelStateDesc') != 'Ringing':
                            continue
                        self.startup_stage = 'guard'
                        self._guard()
                        self.startup_stage = 'speaker'
                        self.speaker_attempted = True
                        self.gateway.execute(self.phone, 'Key:Speaker')
                    elif item.get('ChannelStateDesc') == 'Up':
                        self.startup_stage = 'grant'
                        grant = Grant(self.session, self.phone['id'],
                                      Channel(item['Channel'], item['Uniqueid'], self.line),
                                      self.created, self.deadline)
                        if focus(grant, self.observation, self.clock()) == 'owned_call':
                            self.grant = grant
                            self.startup_stage = 'complete'
                            return grant
                self.stop_event.wait(.1)
        raise BoundaryError('call_setup_timeout')

    async def start(self):
        with self._lifecycle_lock:
            if self.start_attempted or self._sealed:
                raise BoundaryError('single_use_call')
            self.start_attempted = self._start_active = True
        # Drain the bounded gateway worker even if its awaiting task is cancelled;
        # an abandoned thread must not send a late speaker key during cleanup.
        try:
            task = asyncio.create_task(asyncio.to_thread(self._start))
            cancelled = False
            while not task.done():
                try:
                    # Wait does not cancel the worker or rethrow its exception;
                    # cancellation wins after the one worker has been drained.
                    await asyncio.wait((task,))
                except asyncio.CancelledError:
                    self.stop_event.set()
                    cancelled = True
            if cancelled:
                try:
                    task.result()
                except Exception:
                    pass
                raise asyncio.CancelledError
            grant = task.result()
            self.thread = threading.Thread(target=self._observe, daemon=True)
            self.thread.start()
            return grant
        finally:
            with self._lifecycle_lock:
                self._start_active = False

    def seal_no_originate(self):
        """Seal future starts; report only an unattempted, drained lifecycle."""
        with self._lifecycle_lock:
            self._sealed = True
            self.stop_event.set()
            return (self._start_active is False and self.originate_attempted is False
                    and self.speaker_attempted is False and self.deadline_armed is False
                    and self.seen_phone is False and self.used is False
                    and self.thread is None and self.grant is None)

    def _observe(self):
        while not self.stop_event.is_set():
            try:
                with self.gateway.AMI(self.private['ami']) as ami:
                    self._snapshot(ami)
            except Exception:
                self.observation = None
            self.stop_event.wait(.2)

    def snapshot(self):
        return self.observation

    async def validate_admission(self, grant):
        if self.used or grant != self.grant or not self.deadline_armed:
            return False
        self.witness.verify()
        self.used = True
        return focus(grant, self.observation, self.clock()) == 'owned_call'

    def _hangup(self):
        self.stop_event.set()
        with self.gateway.AMI(self.private['ami']) as ami:
            _, channels, _ = self._snapshot(ami)
            for candidate in self._related(channels):
                uid = candidate.get('Uniqueid')
                if not uid:
                    return False
                _, fresh, _ = self._snapshot(ami)
                if not any(item.get('Uniqueid') == uid and item.get('Channel') == candidate.get('Channel')
                           for item in self._related(fresh)):
                    continue
                try:
                    # Pinned Asterisk 22.11.0 resolves this field by Uniqueid as
                    # well as by name. Do not target a recycled channel name.
                    ami.action('Hangup', Channel=uid, Cause='16')
                except Exception:
                    pass  # exact absence below is the only success condition
            end = self.clock() + 2
            while self.clock() < end:
                _, fresh, _ = self._snapshot(ami)
                if not self._related(fresh):
                    # An unseen async originate may still be queued. A short
                    # empty snapshot is insufficient proof against a late call.
                    self.absence_confirmed = (not self.originate_attempted or self.seen_phone or
                                              self.wall_clock() >= self.expires + 10)
                    return self.absence_confirmed
                time.sleep(.05)
        return False

    async def hangup_and_confirm(self, grant=None):
        if grant is not None and grant != self.grant:
            return False
        return await asyncio.to_thread(self._hangup)
