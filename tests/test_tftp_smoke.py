"""Offline tests of the exact denial classifier embedded in the Docker client."""
import importlib.util
import json
from pathlib import Path
import struct
import unittest


spec = importlib.util.spec_from_file_location("tftp_smoke", Path(__file__).with_name("tftp_managed_runtime.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
namespace = {}
exec(smoke.DENIAL_CLASSIFIER, namespace)
require_denial = namespace["require_denial"]


class TFTPDenialTests(unittest.TestCase):
    def diagnostic(self, packet, opcode=1, filename="SIP45.synthetic.loads"):
        with self.assertRaises(AssertionError) as caught:
            require_denial(opcode, filename, packet)
        return json.loads(str(caught.exception).split(": ", 1)[1])

    def test_existing_error_codes_remain_accepted_for_all_synthetic_cases(self):
        cases = ((1, "SIP45.synthetic.loads"), (1, "apps45.synthetic.sbn"),
                 (1, "../" + smoke.PROFILE_NAME), (1, "./../" + smoke.PROFILE_NAME),
                 (1, "/" + smoke.PROFILE_NAME), (1, "/etc/passwd"),
                 (2, "new-upload.xml"), (2, smoke.PROFILE_NAME))
        self.assertEqual(namespace["DENIAL_CASES"], cases)
        for opcode, filename in cases:
            for code in (1, 2):
                with self.subTest(opcode=opcode, filename=filename, code=code):
                    self.assertIsNone(require_denial(opcode, filename,
                                      struct.pack("!HH", 5, code) + b"synthetic error\0"))

    def test_error_code_zero_and_other_codes_are_not_silently_accepted(self):
        for code in (0, 3, 4, 5, 6, 7, 8, 65535):
            with self.subTest(code=code):
                detail = self.diagnostic(struct.pack("!HH", 5, code) + b"error\0")
                self.assertEqual(detail["response_operation"], 5)
                self.assertEqual(detail["error_code"], code)
                self.assertTrue(detail["valid_error_framing"])

    def test_data_ack_and_option_ack_always_fail_even_with_error_like_fields(self):
        for opcode in (3, 4, 6):
            for second_field in (0, 1, 2):
                with self.subTest(opcode=opcode, second_field=second_field):
                    detail = self.diagnostic(struct.pack("!HH", opcode, second_field) + b"payload\0",
                                             opcode=2, filename=smoke.PROFILE_NAME)
                    self.assertEqual(detail["response_operation"], opcode)
                    self.assertIsNone(detail["error_code"])
                    self.assertFalse(detail["valid_error_framing"])

    def test_short_unterminated_and_trailing_error_bytes_fail(self):
        for packet in (b"", b"\0", b"\0\5", b"\0\5\0", b"\0\5\0\2",
                       b"\0\5\0\2unterminated", b"\0\5\0\2error\0trailing",
                       b"\0\5\0\2error\0trailing\0"):
            with self.subTest(length=len(packet)):
                detail = self.diagnostic(packet)
                self.assertEqual(detail["length"], len(packet))
                self.assertFalse(detail["valid_error_framing"])

    def test_diagnostic_contains_only_allowlisted_names_and_numeric_packet_metadata(self):
        packet = b"\0\5\0\0secret-payload-not-for-logs\n\0"
        detail = self.diagnostic(packet, opcode=2, filename=smoke.PROFILE_NAME)
        self.assertEqual(detail, {
            "filename": smoke.PROFILE_NAME, "request_opcode": 2,
            "response_operation": 5, "error_code": 0, "length": len(packet),
            "valid_error_framing": True,
        })
        detail = self.diagnostic(packet, opcode="secret-opcode", filename="secret-filename")
        self.assertEqual(detail["filename"], "other")
        self.assertEqual(detail["request_opcode"], "other")
        self.assertNotIn("secret", json.dumps(detail))


if __name__ == "__main__":
    unittest.main()
