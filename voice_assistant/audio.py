"""The RTP adapter boundary: signed little-endian PCM16, mono, 16 kHz.

Bytes cannot identify their sample rate or channel count. The transport must
decode/resample before calling this module and declare this format explicitly.
There is no device capture, codec inference, or implicit resampling here.
"""

from dataclasses import dataclass
import math
import struct

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
HOP_SAMPLES = 1_280
HOP_BYTES = HOP_SAMPLES * SAMPLE_WIDTH


def validate_pcm16le(pcm: bytes) -> bytes:
    """Return immutable PCM bytes, rejecting partial samples and mutable buffers.

    An empty chunk is valid and does not signal end-of-stream; lifecycle signals
    belong to the service interface. Arbitrary whole-sample chunk sizes are valid.
    """
    if not isinstance(pcm, bytes):
        raise TypeError("PCM must be immutable bytes")
    if len(pcm) % SAMPLE_WIDTH:
        raise ValueError("PCM16 requires a whole number of two-byte samples")
    return pcm


def pcm_duration(pcm: bytes) -> float:
    """Return seconds under the explicitly agreed mono 16 kHz PCM16 contract."""
    return len(validate_pcm16le(pcm)) / BYTES_PER_SECOND


def pcm_rms(pcm: bytes) -> float:
    """Normalized full-scale RMS; this measures energy, not speech confidence."""
    validate_pcm16le(pcm)
    if not pcm:
        return 0.0
    squares = sum(sample * sample for (sample,) in struct.iter_unpack("<h", pcm))
    return math.sqrt(squares / (len(pcm) // SAMPLE_WIDTH)) / 32768


@dataclass
class UtteranceEndpoint:
    """Advisory local endpoint clocks; HA VAD takes precedence once engaged.

    Observe only live post-trigger frames, not queued wake pre-roll. The energy
    threshold needs telephone-specific tuning; it never gates or modifies PCM.
    """

    started_at: float
    speech_rms_threshold: float
    first_audio_at: float | None = None
    first_speech_at: float | None = None
    last_speech_at: float | None = None
    ha_vad_started: bool = False

    def observe(self, pcm: bytes, now: float) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = now
        if pcm_rms(pcm) >= self.speech_rms_threshold:
            if self.first_speech_at is None:
                self.first_speech_at = now
            self.last_speech_at = now

    def decision(self, now: float) -> str | None:
        if self.ha_vad_started:
            return None
        if self.first_audio_at is None:
            return "audio_not_received" if now - self.started_at > 5.0 else None
        if self.first_speech_at is None:
            return "no_speech" if now - self.first_audio_at > 5.0 else None
        if (now - self.first_speech_at >= 2.5 and self.last_speech_at is not None
                and now - self.last_speech_at >= 1.0):
            return "end_of_speech"
        return None
