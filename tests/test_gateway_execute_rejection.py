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

    def execute(self, body, status=200):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = body
        response.__enter__.return_value.status = status
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

    def test_non_ok_http_status_never_acknowledges_a_key(self):
        body = (b'<CiscoIPPhoneResponse><ResponseItem Status="0" '
                b'URL="RTPTx:192.0.2.10:24000"/></CiscoIPPhoneResponse>')
        for status in (201, 202, 204, 206):
            with self.subTest(status=status), self.assertRaises(gateway.Refused):
                self.execute(body, status)

    def test_success_item_with_extra_root_child_is_ambiguous(self):
        for extra in (b'<Other/>', b'<CiscoIPPhoneError Number="4"/>'):
            body = (b'<CiscoIPPhoneResponse><ResponseItem Status="0" '
                    b'URL="RTPTx:192.0.2.10:24000"/>' + extra + b'</CiscoIPPhoneResponse>')
            with self.subTest(extra=extra):
                with self.assertRaises(gateway.Refused) as result:
                    self.execute(body)
                self.assertNotIsInstance(result.exception, gateway.PhoneCommandRejected)

    def test_success_item_with_nested_result_is_ambiguous(self):
        for nested in (b'<ResponseItem Status="4" URL="RTPTx:192.0.2.10:24000"/>',
                       b'<CiscoIPPhoneError Number="4"/>'):
            body = (b'<CiscoIPPhoneResponse><ResponseItem Status="0" '
                    b'URL="RTPTx:192.0.2.10:24000">' + nested
                    + b'</ResponseItem></CiscoIPPhoneResponse>')
            with self.subTest(nested=nested):
                with self.assertRaises(gateway.Refused) as result:
                    self.execute(body)
                self.assertNotIsInstance(result.exception, gateway.PhoneCommandRejected)

    def test_success_preserves_whitespace_and_documented_data_attribute(self):
        body = (b'<CiscoIPPhoneResponse>\n <ResponseItem Status="0" Data="Success" '
                b'URL="RTPTx:192.0.2.10:24000"> \n </ResponseItem>\n</CiscoIPPhoneResponse>')
        self.assertIsNone(self.execute(body))

    def test_response_text_cannot_contradict_status_attributes(self):
        item = b'<ResponseItem Status="0" URL="RTPTx:192.0.2.10:24000"/>'
        bodies = (
            b'<CiscoIPPhoneResponse>ERROR' + item + b'</CiscoIPPhoneResponse>',
            b'<CiscoIPPhoneResponse>' + item + b'ERROR</CiscoIPPhoneResponse>',
            b'<CiscoIPPhoneResponse><ResponseItem Status="0" '
            b'URL="RTPTx:192.0.2.10:24000">ERROR</ResponseItem></CiscoIPPhoneResponse>',
            b'<CiscoIPPhoneError Number="4">contradictory result</CiscoIPPhoneError>',
        )
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(gateway.Refused) as result:
                self.execute(body)
            self.assertNotIsInstance(result.exception, gateway.PhoneCommandRejected)

    def test_nested_item_does_not_claim_a_definite_rejection(self):
        body = (b'<CiscoIPPhoneResponse><ResponseItem Status="4" '
                b'URL="RTPTx:192.0.2.10:24000"><ResponseItem Status="0" '
                b'URL="RTPTx:192.0.2.10:24000"/></ResponseItem></CiscoIPPhoneResponse>')
        with self.assertRaises(gateway.Refused) as result:
            self.execute(body)
        self.assertNotIsInstance(result.exception, gateway.PhoneCommandRejected)

    def test_matching_success_keeps_existing_contract(self):
        self.assertIsNone(self.execute(b'<CiscoIPPhoneResponse><ResponseItem Status="0" URL="RTPTx:192.0.2.10:24000"/></CiscoIPPhoneResponse>'))
