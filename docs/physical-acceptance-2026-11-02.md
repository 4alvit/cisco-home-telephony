# Physical acceptance record — 2 November 2026

This record covers bounded tests of the existing deployment on 2 November in
America/Los_Angeles, corresponding to 3 November UTC. It records what worked on
available equipment and what remains unverified. It does **not** certify an
entire release candidate or the latest beta.

## Confirmed by a person

- **Spoken notification on one Cisco handset:** the complete Russian message was
  audible, and the call ended automatically without a manual hangup.
- **Music on that handset:** audio was clear; pause, resume, and stop behaved as
  expected. This observation does not establish acoustic quality on other
  handsets or separately qualify every source format.

## Confirmed by machine observations

These results use delivery receipts, device state, and PBX channel observations.
A completed delivery receipt alone does not prove that a person heard audio.

- **Cisco speech delivery:** three available handsets completed delivery,
  including a two-handset group announcement.
- **Echo speech delivery:** one available Echo satellite completed a direct
  announcement through the Home Assistant satellite route. Human audibility was
  not confirmed for this endpoint.
- **Automatic Home Assistant notifications:** a test event completed speech
  delivery to three Cisco handsets and one Echo satellite. Repeating the same
  event returned `duplicate_event` with no newly accepted targets.
- **Do Not Disturb:** enabling DND caused speech delivery to skip the selected
  handset with reason `dnd`. The previous DND setting was restored afterward.
- **Intercom:** a connection between two Cisco handsets reached the connected
  state with speaker activation requested. The stop operation removed the call.
  This verifies connection and cleanup, not human-confirmed two-way audio.
- **Managed media controls:** a direct MP3 source reached `playing`; pause,
  resume, seek, volume, and mute/unmute produced the corresponding state changes.
  A direct FLAC source was accepted into the queue; next, previous, and queue
  clearing changed the queue as requested. The sampled FLAC transition was
  `buffering`, so this record does not claim independently verified FLAC
  playback or audibility. Stop returned the player to idle and removed its
  channels. Volume and mute preferences were restored.
- **Home Assistant Stop:** stopping both a managed media session and a legacy
  radio session returned the handset to idle with no remaining call channels.

## Defect found during media testing

Stopping playback exposed an HTTP response cleanup race: the cancellation
thread could close the response while its reader was completing a read, causing
an uncaught `AttributeError`. The observed Stop operations still completed;
their success did not make the exception harmless to ignore.

The fix keeps response cleanup in the reader thread. Cancellation interrupts
the socket and stops the decoder, without concurrently closing the response.
A deterministic regression using the real HTTP parser reproduces the original
failure and passes with the fix. A separate test ensures unexpected reader
errors remain visible rather than being suppressed. The 31 media-runtime tests
passed, including the existing synthetic MP3/FLAC playback tests.

## Unavailable or protected endpoints

Seven configured Cisco handsets were offline during this run. Their physical
behavior was not tested. Of the other Echo satellites, one was unavailable
through ESPHome, one lacked the required ADB/SIP guard observations, and one
direct-announcement attempt was skipped because Assist was busy. Missing guards
also caused skips during automatic dispatch; those are protective outcomes,
not successful playback tests. The LifeSize endpoint could not be inspected
because its SSH read timed out.

## Wake assistant: acceptance remains incomplete

The bounded hands-free test reached `armed` / `wake_ready`, then ended without a
completed assistant turn. The PBX phone leg left its bridge **10.04 seconds after
answering**, followed by the AudioSocket connection closing. The matching child
report recorded `peer_closed`, exit code 1, and confirmed cleanup. No TTS playback
occurred in this session.

The configured five-minute deadline had not expired; effective RTP inactivity
timeouts were disabled, and the relevant containers had not restarted. Retained
logs do not identify who initiated the SIP-leg termination. This is not evidence
that the wake detector, Home Assistant, or a particular phone action caused it.
Subsequent tests produced two distinct results:

- **Idle diagnostic run:** the session stayed running for more than 40 seconds,
  with repeated `armed` / `wake_ready` observations. It ended only after the
  operator explicitly disabled it; the captured termination was `explicit_stop`
  with `stop_confirmed`. This run did not reproduce a spontaneous termination.
- **Acoustic stimulation:** another handset played two spoken stimuli into the
  listening handset's microphone. After the first, no wake transition or
  completed turn was observed. After the second, the listener reached
  `thinking`, then a new session appeared with `consecutive_failures: 1` and
  zero completed turns. This was a restart, not a successful answer followed by
  rearming. The matching failure receipt reported `assistant_stopped`, with no
  TTS playback and confirmed cleanup before the supervisor restarted the
  session. This differs from the earlier `peer_closed` failure; its underlying
  Home Assistant error category has not yet been established. The final
  explicit stop was confirmed.

These observations do not establish reliable wake detection or a completed
hands-free conversation. The acoustic-run failure remains under investigation;
the successful idle stop does not explain or invalidate the earlier termination.

## Local suite and CI status

The broader local suite reported 2,835 tests, 22 skips, and two errors. Both
errored tests passed an unchanged targeted rerun. This is not recorded as a clean
full-suite pass. Completion of all required CI checks remains pending.

## Scope and provenance

The deployment contained mixed source revisions and image identities. For one
bounded source check, all 28 installed `cisco_phone_control` files matched
[`1ac0f3052d8c5a9ebc4b446498f804493a52ce70`](https://github.com/ha-homelab/asterisk-telephony/commit/1ac0f3052d8c5a9ebc4b446498f804493a52ce70).
File equality does not prove that every long-running process reloaded those
bytes. Served TFTP profiles likewise do not prove which firmware every handset
booted.

Device assignments, credentials, addresses, session identifiers, and detailed
runtime evidence remain private. Future rollout results must identify their
own tested candidate and must not inherit these observations as whole-release
certification. See [candidate freezing](media-candidate-acceptance.md) for that
separate process.
