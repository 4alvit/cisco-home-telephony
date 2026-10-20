"""A definite phone refusal must not be treated as an accepted RTP start."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'containers/asterisk'))
import phone_gateway as gateway


class ExecuteRejectionTests(unittest.TestCase):
    phone = {'ipv4': '192.0.2.20', 'username': 'test-user', 'password': 'test-secret'}
    uri = 'RTPTx:192.0.2.10:24000'

    def execute(self, body):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = body
        with patch.object(gateway.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            return gateway.execute(self.phone, self.uri)

    def test_known_root_and_matching_item_refusals_preserve_code(self):
        for body in (b'<CiscoIPPhoneError Number="1"/>',
                     b'<CiscoIPPhoneResponse><ResponseItem Status="4" URL="RTPTx:192.0.2.10:24000"/></CiscoIPPhoneResponse>'):
            with self.subTest(body=body), self.assertRaises(gateway.PhoneCommandRejected) as result:
                self.execute(body)
            self.assertIn(result.exception.response_code, (1, 4))
            self.assertEqual(result.exception.reason, 'phone_rejected_action')

    def test_wrong_url_unknown_code_and_multi_item_are_ambiguous(self):
        for body in (b'<CiscoIPPhoneError Number="99"/>',
                     b'<CiscoIPPhoneResponse><ResponseItem Status="1" URL="wrong"/></CiscoIPPhoneResponse>',
                     b'<CiscoIPPhoneResponse><ResponseItem Status="1" URL="RTPTx:192.0.2.10:24000"/><Other/></CiscoIPPhoneResponse>'):
            with self.subTest(body=body), self.assertRaises(gateway.Refused) as result:
                self.execute(body)
            self.assertNotIsInstance(result.exception, gateway.PhoneCommandRejected)

    def test_matching_success_keeps_existing_contract(self):
        self.assertIsNone(self.execute(b'<CiscoIPPhoneResponse><ResponseItem Status="0" URL="RTPTx:192.0.2.10:24000"/></CiscoIPPhoneResponse>'))
