"""AMI failure diagnostics identify actions without exposing wire contents."""
import io
import unittest
from unittest.mock import Mock, patch

from test_phone_gateway import gateway


class AMIDiagnostics(unittest.TestCase):
    def test_rejected_actions_keep_public_code_and_bounded_private_diagnostics(self):
        cases = [('Login', 'Login Already In Use', 'login_in_use'),
                 ('Login', 'Authentication failed', 'authentication_failed'),
                 ('PJSIPShowEndpoints', 'No endpoints found', 'no_endpoints'),
                 ('PJSIPShowEndpoints', 'Invalid/unknown command: PJSIPShowEndpoints', 'unknown_action'),
                 ('CoreShowChannels', 'Permission denied', 'permission_denied'),
                 ('Hangup', 'No such channel', 'no_such_channel'),
                 ('Originate', 'do not expose: synthetic-private-value', 'other')]
        for action, message, expected in cases:
            with self.subTest(action=action, classification=expected):
                ami = gateway.AMI({})
                ami.sock = Mock()
                ami.file = io.BytesIO(('Response: Error\r\nActionID: synthetic\r\nMessage: ' +
                                      message + '\r\n\r\n').encode())
                with patch.object(gateway.secrets, 'token_hex', return_value='synthetic'):
                    with self.assertRaises(gateway.Refused) as caught:
                        ami.action(action, Secret='synthetic-private-value')
                error = caught.exception
                self.assertEqual(error.reason, 'ami_action_rejected')
                self.assertEqual(error.status, 503)
                self.assertEqual(error.ami_action, action)
                self.assertEqual(error.ami_error, expected)
                self.assertNotIn('synthetic-private-value', str(error))
                self.assertNotIn('synthetic-private-value', repr(error.__dict__))


if __name__ == '__main__':
    unittest.main()
