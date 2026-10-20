"""Finite synthetic RTP and decoder-boundary checks; never contact a phone."""

import base64
import errno
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.sip_video import media


def ue(value):
    binary = bin(value + 1)[2:]
    return "0" * (len(binary) - 1) + binary


def escape_rbsp(raw):
    out, zeros = bytearray(), 0
    for value in raw:
        if zeros == 2 and value <= 3:
            out.append(3)
            zeros = 0
        out.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(out)


def packed(bits):
    bits += "1"
    bits += "0" * ((-len(bits)) % 8)
    return escape_rbsp(int(bits, 2).to_bytes(len(bits) // 8, "big"))


def sps(width_mbs=40, height_mbs=23, *, refs=1, crop=(0, 0, 0, 4), frame_only=True):
    bits = ue(0) + ue(0) + ue(0) + ue(0) + ue(refs) + "0"
    bits += ue(width_mbs - 1) + ue(height_mbs - 1) + str(int(frame_only))
    if not frame_only:
        bits += "0"
    bits += "1" + str(int(crop is not None))
    if crop is not None:
        bits += "".join(ue(item) for item in crop)
    bits += "0"  # no VUI
    return b"\x67\x42\x00\x1e" + packed(bits)


SPS = sps()
PPS = b"\x68" + packed(ue(0) + ue(0) + "00" + ue(0))
IDR = b"\x65" + packed(ue(0) + ue(2) + ue(0) + "10101010")
SPROP = ",".join(base64.b64encode(nal).decode() for nal in (SPS, PPS))


def rtp(payload, *, seq=1, timestamp=9000, ssrc=12345, marker=False, pt=96, first=0x80):
    return struct.pack("!BBHII", first, pt | (128 if marker else 0), seq,
                       timestamp, ssrc) + payload


def aggregate(*nals):
    return b"\x78" + b"".join(struct.pack("!H", len(nal)) + nal for nal in nals)


def offer(port):
    return SimpleNamespace(payload_type=96, source_port=port, packetization_mode=1,
                           sprop_parameter_sets=SPROP, profile_level_id="42001e")


def wait_for(receiver, *, states=("captured", "failed"), timeout=2):
    deadline = time.monotonic() + timeout
    while receiver.state not in states and time.monotonic() < deadline:
        time.sleep(0.005)
    return receiver.snapshot()


class ParameterSetsTest(unittest.TestCase):
    def test_sps_bounds_use_coded_dimensions_before_cropping(self):
        self.assertEqual(media.validate_sps(SPS)["width"], 640)
        self.assertEqual(media.validate_sps(SPS)["height"], 360)
        for invalid in (sps(width_mbs=200, crop=(0, 1500, 0, 0)),
                        sps(height_mbs=200, crop=(0, 0, 0, 1500)),
                        sps(refs=5), sps(frame_only=False), sps(crop=(0, 1000, 0, 0)),
                        SPS[:4], b"\x67\x42\x00\x1e" + b"\x00" * 20):
            with self.subTest(invalid=invalid), self.assertRaises(media.MediaError):
                media.validate_sps(invalid)

    def test_sprop_encoding_types_and_parameter_relationship(self):
        self.assertEqual(media.validate_sprop(SPROP), (SPS, PPS))
        self.assertEqual(media.validate_sprop(None), ())
        wrong_pps = b"\x68" + packed(ue(0) + ue(1) + "00" + ue(0))
        for invalid in ("", "????,????", SPROP + "," + SPROP, 1,
                        ",".join(base64.b64encode(n).decode() for n in (PPS, SPS)),
                        ",".join(base64.b64encode(n).decode() for n in (SPS, wrong_pps))):
            with self.subTest(invalid=invalid), self.assertRaises(media.MediaError):
                media.validate_sprop(invalid)

    def test_negotiated_profile_and_immutable_parameter_sets(self):
        with self.assertRaisesRegex(media.MediaError, "profile-level-id"):
            media.RTPAssembler(96, 1, SPROP, profile_level_id="42c01e")
        assembler = media.RTPAssembler(96, 1, SPROP, profile_level_id="42001e")
        with self.assertRaisesRegex(media.MediaError, "SPS changes"):
            assembler.feed(rtp(sps(width_mbs=20)))

    def test_ebsp_rejects_hidden_delimiters_and_bad_prevention(self):
        for raw in (b"\0\0\0", b"\0\0\1", b"\0\0\2", b"\0\0\3", b"\0\0\3\4"):
            with self.subTest(raw=raw), self.assertRaises(media.MediaError):
                media._rbsp(raw)
        self.assertEqual(media._rbsp(b"\0\0\3\1"), b"\0\0\1")
        assembler = media.RTPAssembler(96, 1, SPROP)
        with self.assertRaisesRegex(media.MediaError, "unescaped"):
            assembler.feed(rtp(IDR + b"\0\0\1" + sps(width_mbs=200), marker=True))


class RTPAssemblerTest(unittest.TestCase):
    def test_single_nal_stap_and_fu_with_sequence_wrap(self):
        assembler = media.RTPAssembler(96, 1, max_frames=2)
        assembler.feed(rtp(aggregate(SPS, PPS), seq=65534))
        assembler.feed(rtp(IDR, seq=65535, marker=True))
        header = (IDR[0] & 0xE0) | 28
        assembler.feed(rtp(bytes([header, 0x85]) + IDR[1:2], seq=0, timestamp=18000))
        assembler.feed(rtp(bytes([header, 0x45]) + IDR[2:], seq=1,
                           timestamp=18000, marker=True))
        self.assertTrue(assembler.complete)
        self.assertEqual(assembler.accepted_packets, 4)
        self.assertEqual(assembler.access_units, 2)
        self.assertEqual(assembler.finish(),
                         b"".join(media.START_CODE + nal for nal in (SPS, PPS, IDR, IDR)))
        with self.assertRaisesRegex(media.MediaError, "frame limit"):
            assembler.feed(rtp(IDR))

    def test_peer_stream_identity_sequence_and_timestamp_fail_closed(self):
        cases = [
            rtp(IDR, seq=3, marker=True),  # loss
            rtp(IDR, seq=1, marker=True),  # duplicate
            rtp(IDR, seq=2, ssrc=999, marker=True),
            rtp(IDR, seq=2, pt=97, marker=True),
            rtp(IDR, seq=2, timestamp=18000, marker=True),  # missing prior marker
            rtp(IDR, seq=2, first=0x90, marker=True),
        ]
        for packet in cases:
            assembler = media.RTPAssembler(96, 1, SPROP)
            assembler.feed(rtp(b"\x09\xf0"))
            with self.subTest(packet=packet), self.assertRaises(media.MediaError):
                assembler.feed(packet)
        for timestamp in (9000, 8000):
            assembler = media.RTPAssembler(96, 1, SPROP)
            assembler.feed(rtp(IDR, marker=True))
            with self.assertRaises(media.MediaError):
                assembler.feed(rtp(IDR, seq=2, timestamp=timestamp, marker=True))

    def test_fu_invalid_flags_or_sequence_interleaving(self):
        invalid = (b"\x7c\xc5x", b"\x7c\xa5x", b"\x7c\x45x", b"\x7c\x85")
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(media.MediaError):
                media.RTPAssembler(96, 1, SPROP).feed(rtp(payload))
        for second in (rtp(IDR, seq=2), rtp(b"\x7c\x85x", seq=2),
                       rtp(b"\x5c\x45x", seq=2), rtp(b"\x7c\x45x", seq=3),
                       rtp(b"\x7c\x45x", seq=2, timestamp=18000)):
            assembler = media.RTPAssembler(96, 1, SPROP)
            assembler.feed(rtp(b"\x7c\x85\xb8"))
            with self.subTest(second=second), self.assertRaises(media.MediaError):
                assembler.feed(second)
        assembler = media.RTPAssembler(96, 1, SPROP)
        with self.assertRaisesRegex(media.MediaError, "marker"):
            assembler.feed(rtp(b"\x7c\x85\xb8", marker=True))
        assembler = media.RTPAssembler(96, 1, SPROP)
        assembler.feed(rtp(b"\x7c\x85\xb8"))
        with self.assertRaisesRegex(media.MediaError, "incomplete"):
            assembler.finish()

    def test_packet_mode_payload_and_stap_bounds(self):
        invalid = (b"", b"x" * 2049, rtp(b"\x78"), rtp(b"\x78\0\0"),
                   rtp(b"\x78\0\x02x"), rtp(b"\x79x"), rtp(b"\xe5xx"))
        for packet in invalid:
            with self.subTest(packet=packet[:20]), self.assertRaises(media.MediaError):
                media.RTPAssembler(96, 1, SPROP).feed(packet)
        with self.assertRaises(media.MediaError):
            media.RTPAssembler(96, 0, SPROP).feed(rtp(aggregate(SPS, PPS)))
        with self.assertRaisesRegex(media.MediaError, "before SPS"):
            media.RTPAssembler(96, 1).feed(rtp(IDR, marker=True))
        with patch.object(media, "MAX_NAL", 2), self.assertRaises(media.MediaError):
            media.RTPAssembler(96, 1).feed(rtp(SPS))
        with patch.object(media, "MAX_INPUT", 20), self.assertRaises(media.MediaError):
            media.RTPAssembler(96, 1, SPROP).feed(rtp(IDR, marker=True))
        with patch.object(media, "MAX_PACKETS", 1):
            assembler = media.RTPAssembler(96, 1, SPROP)
            assembler.feed(rtp(b"\x09\xf0"))
            with self.assertRaises(media.MediaError):
                assembler.feed(rtp(IDR, seq=2, marker=True))


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender.bind(("127.0.0.1", 0))
        self.receivers = []

    def tearDown(self):
        for receiver in self.receivers:
            receiver.close(decode=False)
        self.sender.close()
        self.tmp.cleanup()

    def receiver(self, **kwargs):
        receiver = media.MediaReceiver(offer(self.sender.getsockname()[1]),
                                       self.root / "frames", **kwargs).start()
        self.receivers.append(receiver)
        return receiver

    def test_exact_peer_and_cancel_cleanup(self):
        receiver = self.receiver(max_frames=1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wrong:
            wrong.sendto(rtp(IDR, marker=True), ("127.0.0.1", receiver.port))
        self.sender.sendto(rtp(IDR, marker=True), ("127.0.0.1", receiver.port))
        snapshot = wait_for(receiver)
        self.assertEqual(snapshot["access_units"], 1)
        self.assertEqual(snapshot["ignored_sources"], 1)
        with patch.object(media, "decode_stills") as decode:
            result = receiver.close(decode=False)
            decode.assert_not_called()
        self.assertTrue(result["socket_closed"])
        self.assertFalse(result["worker_alive"])
        self.assertFalse(result["decoder_running"])
        self.assertEqual(result["decoded_frames"], 0)
        self.assertEqual(result, receiver.close())
        self.assertFalse(receiver.assembler.stream)

    def test_timeout_empty_and_partial_capture(self):
        receiver = self.receiver(max_seconds=0.03)
        wait_for(receiver)
        result = receiver.close()
        self.assertIsNone(result["error"])
        self.assertEqual(result["decoded_frames"], 0)
        self.assertTrue(result["socket_closed"])
        self.assertFalse((self.root / "frames").exists())
        receiver = self.receiver(max_seconds=0.03)
        self.sender.sendto(rtp(b"\x7c\x85\xb8"), ("127.0.0.1", receiver.port))
        wait_for(receiver)
        result = receiver.close()
        self.assertIn("incomplete", result["error"])
        self.assertFalse(result["worker_alive"])

    def test_bad_media_does_not_launch_decoder(self):
        receiver = self.receiver()
        self.sender.sendto(b"bad RTP", ("127.0.0.1", receiver.port))
        wait_for(receiver)
        with patch.object(media, "decode_stills") as decode:
            result = receiver.close()
            decode.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["packets_accepted"], 0)
        self.assertTrue(result["socket_closed"])

    def test_write_failure_removes_only_new_outputs(self):
        receiver = self.receiver(max_frames=1)
        self.sender.sendto(rtp(IDR, marker=True), ("127.0.0.1", receiver.port))
        wait_for(receiver)
        real_open = media.os.open
        count = 0

        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic private write error")
            return real_open(*args, **kwargs)

        with patch.object(media, "decode_stills", return_value=[b"frame1", b"frame2"]), \
                patch.object(media.os, "open", side_effect=fail_second):
            result = receiver.close()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["frames"], [])
        self.assertFalse((self.root / "frames").exists())

    def test_existing_output_is_preserved(self):
        receiver = self.receiver(max_frames=1)
        self.sender.sendto(rtp(IDR, marker=True), ("127.0.0.1", receiver.port))
        wait_for(receiver)
        (self.root / "frames").mkdir()
        original = self.root / "frames" / "keep"
        original.write_bytes(b"preserve")
        with patch.object(media, "decode_stills", return_value=[b"frame"]):
            result = receiver.close()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(original.read_bytes(), b"preserve")
        self.assertEqual(list((self.root / "frames").iterdir()), [original])


class DecoderTest(unittest.TestCase):
    def stream(self):
        return b"".join(media.START_CODE + nal for nal in (SPS, PPS, IDR))

    def test_pipe_only_command_and_predecode_sps_validation(self):
        command = media.decoder_command(3)
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
        self.assertEqual(command[command.index("-i") + 1], "pipe:0")
        self.assertEqual(command[-1], "pipe:1")
        with patch.object(media.subprocess, "Popen") as popen:
            with self.assertRaises(media.MediaError):
                media.decode_stills(media.START_CODE + sps(width_mbs=200), 1)
            popen.assert_not_called()

    def test_decoder_timeout_and_oversized_stdout_are_killed_and_reaped(self):
        for body, expected, seconds, cap in (
            ("import time; time.sleep(3)", "time limit", 0.04, media.MAX_OUTPUT),
            ("import sys; sys.stdout.buffer.write(b'x'*100000); sys.stdout.flush()",
             "byte limit", 2, 64),
        ):
            processes = []
            with patch.object(media, "decoder_command", return_value=[sys.executable, "-c", body]), \
                    patch.object(media, "DECODE_SECONDS", seconds), \
                    patch.object(media, "MAX_OUTPUT", cap):
                with self.assertRaisesRegex(media.MediaError, expected):
                    media.decode_stills(self.stream(), 1,
                                        process_hook=lambda item: processes.append(item))
            self.assertIsNone(processes[-1])
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdin.closed)
            self.assertTrue(processes[0].stdout.closed)

    def assert_decoder_reaped(self, processes):
        self.assertEqual(len(processes), 2)
        self.assertIsNone(processes[-1])
        process = processes[0]
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    def test_decoder_partial_write_then_would_block_retries_exact_remaining_bytes(self):
        data, processes, attempts, accepted = self.stream(), [], [], bytearray()
        injected = []
        real_write = os.write

        def write(fd, chunk):
            if processes and processes[0].stdin is not None and not processes[0].stdin.closed and fd == processes[0].stdin.fileno():
                attempts.append(bytes(chunk))
                self.assertEqual(bytes(chunk), data[len(accepted):])
                if accepted and not injected:
                    injected.append(bytes(chunk))
                    raise BlockingIOError(errno.EAGAIN, "injected pipe write race")
                # Natural would-block races may happen too. Limit the first
                # successful write, then inject exactly once after progress.
                count = real_write(fd, chunk[:7] if not accepted else chunk)
                accepted.extend(chunk[:count])
                return count
            return real_write(fd, chunk)

        body = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stdout.flush()"
        with patch.object(media, "decoder_command", return_value=[sys.executable, "-c", body]), \
                patch.object(media.os, "write", side_effect=write), \
                patch.object(media, "split_pngs", return_value=[b"synthetic frame"]) as split:
            result = media.decode_stills(data, 1, process_hook=processes.append)
        self.assertEqual(result, [b"synthetic frame"])
        self.assertGreaterEqual(len(attempts), 3)
        self.assertEqual(len(injected), 1)
        self.assertLess(len(injected[0]), len(data))
        self.assertEqual(bytes(accepted), data)
        split.assert_called_once_with(data, 1)
        self.assert_decoder_reaped(processes)

    def test_decoder_partial_read_then_would_block_retains_output_until_real_eof(self):
        data, processes, attempts, received = self.stream(), [], [], bytearray()
        injected, eof = [], []
        real_read = os.read

        def read(fd, count):
            if processes and not processes[0].stdout.closed and fd == processes[0].stdout.fileno():
                attempts.append(count)
                if received and not injected:
                    injected.append(True)
                    raise BlockingIOError(errno.EWOULDBLOCK, "injected pipe read race")
                chunk = real_read(fd, min(count, 5) if not received else count)
                received.extend(chunk)
                if not chunk:
                    eof.append(True)
                return chunk
            return real_read(fd, count)

        body = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stdout.flush()"
        with patch.object(media, "decoder_command", return_value=[sys.executable, "-c", body]), \
                patch.object(media.os, "read", side_effect=read), \
                patch.object(media, "split_pngs", return_value=[b"synthetic frame"]) as split:
            result = media.decode_stills(data, 1, process_hook=processes.append)
        self.assertEqual(result, [b"synthetic frame"])
        self.assertGreaterEqual(len(attempts), 4)  # partial, would-block, remainder, EOF
        self.assertEqual(injected, [True])
        self.assertEqual(eof, [True])
        self.assertEqual(bytes(received), data)
        split.assert_called_once_with(data, 1)
        self.assert_decoder_reaped(processes)

    def test_decoder_repeated_would_block_obeys_absolute_deadline_and_kills_child(self):
        # The child stays alive without a sleep. Each injected readiness race
        # advances a controlled clock; retries must never restart the deadline.
        body = "import sys,signal; sys.stdout.buffer.write(b'pending'); sys.stdout.flush(); signal.pause()"
        for direction in ("write", "read"):
            with self.subTest(direction=direction):
                processes, retries, clock = [], [], [100.0]
                real_operation = getattr(os, direction)

                def would_block(fd, value):
                    stream_name = "stdin" if direction == "write" else "stdout"
                    stream = getattr(processes[0], stream_name) if processes else None
                    if stream is not None and not stream.closed and fd == stream.fileno():
                        retries.append(fd)
                        if len(retries) > 4:
                            raise AssertionError("decoder restarted its absolute deadline")
                        clock[0] += .25
                        raise BlockingIOError(errno.EAGAIN, "injected repeated readiness race")
                    return real_operation(fd, value)

                with patch.object(media, "decoder_command", return_value=[sys.executable, "-c", body]), \
                        patch.object(media.os, direction, side_effect=would_block), \
                        patch.object(media.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(media, "DECODE_SECONDS", .5), \
                        patch.object(media, "split_pngs") as split:
                    with self.assertRaisesRegex(media.MediaError, "decoder exceeded time limit"):
                        media.decode_stills(self.stream(), 1, process_hook=processes.append)
                self.assertEqual(len(retries), 2)
                self.assertEqual(clock[0], 100.5)
                split.assert_not_called()
                self.assert_decoder_reaped(processes)
                self.assertLess(processes[0].returncode, 0)  # killed, not voluntary exit

    def test_decoder_unrelated_io_errors_propagate_and_still_cleanup(self):
        body = "import sys,signal; sys.stdout.buffer.write(b'pending'); sys.stdout.flush(); signal.pause()"
        for direction in ("write", "read"):
            with self.subTest(direction=direction):
                processes = []
                failure = OSError(errno.EIO, "injected fatal pipe error")
                real_operation = getattr(os, direction)

                def fail(fd, value):
                    stream_name = "stdin" if direction == "write" else "stdout"
                    stream = getattr(processes[0], stream_name) if processes else None
                    if stream is not None and not stream.closed and fd == stream.fileno():
                        raise failure
                    return real_operation(fd, value)

                with patch.object(media, "decoder_command", return_value=[sys.executable, "-c", body]), \
                        patch.object(media.os, direction, side_effect=fail), \
                        patch.object(media, "split_pngs") as split:
                    with self.assertRaises(OSError) as caught:
                        media.decode_stills(self.stream(), 1, process_hook=processes.append)
                self.assertIs(caught.exception, failure)
                split.assert_not_called()
                self.assert_decoder_reaped(processes)
                self.assertLess(processes[0].returncode, 0)

    def test_decoder_broken_stdin_pipe_still_drains_stdout_to_eof(self):
        processes, broken = [], []
        real_write = os.write

        def write(fd, chunk):
            if processes and not processes[0].stdin.closed and fd == processes[0].stdin.fileno():
                broken.append(fd)
                raise BrokenPipeError(errno.EPIPE, "injected decoder closed input")
            return real_write(fd, chunk)

        body = "import sys; sys.stdout.buffer.write(b'decoder output'); sys.stdout.flush()"
        with patch.object(media, "decoder_command", return_value=[sys.executable, "-c", body]), \
                patch.object(media.os, "write", side_effect=write), \
                patch.object(media, "split_pngs", return_value=[b"synthetic frame"]) as split:
            result = media.decode_stills(self.stream(), 1, process_hook=processes.append)
        self.assertEqual(result, [b"synthetic frame"])
        self.assertEqual(len(broken), 1)
        split.assert_called_once_with(b"decoder output", 1)
        self.assert_decoder_reaped(processes)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_real_h264_decodes_to_expected_color_and_dimensions(self):
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
            "color=c=red:s=640x360:r=1", "-frames:v", "1", "-c:v", "libx264",
            "-threads", "1", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            "-f", "h264", "pipe:1",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, "fixture encoder failed")
        # Encoder Annex B may use three- or four-byte delimiters. RTP carries
        # only NAL bytes, so normalize them to the receiver's canonical form.
        import re
        nals = [nal for nal in re.split(b"\x00\x00\x00?\x01", result.stdout) if nal]
        assembler = media.RTPAssembler(96, 1)
        for nal in nals:
            assembler._nal(nal)
        frames = media.decode_stills(bytes(assembler.stream), 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(struct.unpack(">II", frames[0][16:24]), (298, 156))
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow needed for decoded pixel assertion")
        import io
        picture = Image.open(io.BytesIO(frames[0]))
        red, green, blue = picture.getpixel((149, 78))
        self.assertGreater(red, 230)
        self.assertLess(green, 20)
        self.assertLess(blue, 20)


if __name__ == "__main__":
    unittest.main()
