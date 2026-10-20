"""The RTP adapter boundary: signed little-endian PCM16, mono, 16 kHz.

Bytes cannot identify their sample rate or channel count. The transport must
decode/resample before calling this module and declare this format explicitly.
There is no device capture, codec inference, or implicit resampling here.
"""

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
