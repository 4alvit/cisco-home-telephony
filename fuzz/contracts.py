"""Offline invariants over the real packet parsers; no listeners or devices."""

import ipaddress

from tools.l16_lab import protocol
from tools.sip_video import signaling


def fuzz_protocol(data: bytes) -> None:
    if len(data) > 16384:
        return
    try:
        parsed = protocol.parse_audio_sdp(data)
    except protocol.ProtocolError:
        pass
    else:
        assert 0 <= parsed.port <= 65535
        assert not ipaddress.IPv4Address(parsed.address).is_multicast
        assert parsed.direction in {"sendrecv", "sendonly", "recvonly", "inactive"}
        assert len({codec.payload_type for codec in parsed.codecs}) == len(parsed.codecs)
        assert all(0 <= codec.payload_type <= 127 for codec in parsed.codecs)
        assert protocol.parse_audio_sdp(data) == parsed
    try:
        frame = protocol.parse_audiosocket_frame(data)
    except protocol.ProtocolError:
        pass
    else:
        assert frame.payload == data[3:]
        assert frame.message_type in {0, 1, 3, 0x12, 0xff}
        if frame.message_type == 0x12:
            assert protocol.audiosocket_frame(frame.payload) == data
    try:
        packet = protocol.parse_rtp(data)
    except protocol.ProtocolError:
        pass
    else:
        wire = protocol.make_rtp(packet.payload, packet.payload_type, packet.sequence,
                                 packet.timestamp, packet.ssrc, packet.marker)
        # Canonical serialization discards extension/padding bytes, not media.
        assert protocol.parse_rtp(wire) == packet


def fuzz_signaling(data: bytes) -> None:
    if len(data) > signaling.MAX_MESSAGE:
        return
    try:
        request = signaling.parse_request(data, (signaling.LOOPBACK, 25006))
    except signaling.SipError:
        pass
    else:
        assert request.method in {"INVITE", "ACK", "BYE", "CANCEL", "OPTIONS"}
        assert int(request.headers["content-length"]) == len(request.body)
        assert 0 < request.cseq < 2**31
        assert not request.body or request.method == "INVITE"
        assert signaling.parse_request(data, (signaling.LOOPBACK, 25006)) == request
        for source in (("192.0.2.1", 25006), ("127.0.0.1", 25007)):
            try:
                signaling.parse_request(data, source)
            except signaling.SipError:
                pass
            else:
                raise AssertionError("accepted mismatched/reflection source")
    try:
        offer = signaling.parse_offer(data)
    except signaling.SipError:
        return
    assert 96 <= offer.payload_type <= 127
    assert 1024 <= offer.source_port <= 65535
    assert offer.packetization_mode in {0, 1}
    assert sum(item.kind == "video" for item in offer.media_sections) == 1
    answer = signaling.answer_sdp(offer, 25008)
    assert b"c=IN IP4 127.0.0.1\r\n" in answer
    assert b"a=recvonly\r\n" in answer
    assert b"m=video 25008 RTP/AVP " + str(offer.payload_type).encode() in answer
