"""Source-scoped, manual-answer hotline contracts; no device or network I/O."""
import copy
import unittest

from test_phone_services import s


class HotlineDialplanTests(unittest.TestCase):
    def setUp(self):
        self.site = {'gateway': {'enabled': True}, 'hotline_ring_group': {
            'enabled': True, 'extension': '622', 'source': '201',
            'members': ['203', '209'], 'timeout': 25}}
        self.members = ['201', '203', '209']
        self.owners = {'201': '201', '202': '201', '203': '203', '204': '203'}

    def test_disabled_does_not_require_inventory_or_add_a_route(self):
        for site in ({}, {'hotline_ring_group': {'enabled': False}}):
            self.assertIsNone(s.hotline_settings(site))
            self.assertEqual(s.hotline_dialplan(site), '')

    def test_exact_source_endpoints_manual_answer_parallel_dial_and_bounded_failure(self):
        before = copy.deepcopy(self.site)
        result = s.hotline_settings(self.site, self.members, self.owners)
        self.assertEqual(result['source_lines'], ['201', '202'])
        result['members'].append('205')
        self.assertEqual(self.site, before)
        dialplan = s.hotline_dialplan(self.site, self.members, self.owners)
        self.assertIn('${CHANNEL(channeltype)}', dialplan)
        self.assertIn('${CHANNEL(endpoint)}', dialplan)
        self.assertNotIn('CALLERID', dialplan)
        self.assertIn('"${HOTLINE_SOURCE}" = "201" | "${HOTLINE_SOURCE}" = "202"', dialplan)
        self.assertIn('Set(__CISCO_HOTLINE=1)', dialplan)
        for extension in ('203', '209'):
            self.assertIn('DB(cisco-dnd/' + extension + ')', dialplan)
            self.assertIn('PJSIP/' + extension, dialplan)
        for extension in ('201', '202', '204'):
            self.assertNotIn('PJSIP/' + extension, dialplan)
        self.assertEqual(dialplan.count('Dial('), 1)
        self.assertIn('Dial(${HOTLINE_TARGETS},25,i)', dialplan)
        self.assertIn('n(unavailable),Busy(3)', dialplan)
        self.assertIn('n(denied),Hangup(21)', dialplan)
        for forbidden in ('Answer()', 'Page(', 'Originate(', 'PJSIP_HEADER', 'Goto(home,'):
            self.assertNotIn(forbidden, dialplan)

    def test_unknown_secondary_duplicate_external_and_self_targets_fail_closed(self):
        for members in ([], ['203', '203'], ['204'], ['299'], ['801'], ['201'], ['202'], ['203\n']):
            with self.subTest(members=members):
                self.site['hotline_ring_group']['members'] = members
                with self.assertRaises(ValueError):
                    s.hotline_settings(self.site, self.members, self.owners)

    def test_source_must_be_a_primary_physical_phone_even_if_softphone_is_allowed(self):
        for source in ('202', '209', '299', '801', None, 201, '201\n'):
            with self.subTest(source=source):
                self.site['hotline_ring_group']['source'] = source
                with self.assertRaises(ValueError):
                    s.hotline_settings(self.site, self.members, self.owners)

    def test_unknown_missing_and_wrong_type_configuration_rejected(self):
        value = self.site['hotline_ring_group']
        variants = [None, [], {'enabled': 'true'}, dict(value, extra=True),
                    {k: v for k, v in value.items() if k != 'source'}]
        variants += [dict(value, extension=x) for x in ('620', '621', '630', '911', 622, '622\n')]
        variants += [dict(value, timeout=x) for x in (False, 0, 121, 25.0, '25')]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                self.site['hotline_ring_group'] = candidate
                with self.assertRaises(ValueError):
                    s.hotline_settings(self.site, self.members, self.owners)

    def test_no_render_without_complete_ownership_or_gateway(self):
        for args in ((self.site,), (self.site, self.members), (self.site, None, self.owners)):
            with self.assertRaises(ValueError):
                s.hotline_dialplan(*args)
        for value in (None, {}, {'enabled': False}):
            self.site['gateway'] = value
            with self.assertRaises(ValueError):
                s.hotline_settings(self.site, self.members, self.owners)


if __name__ == '__main__':
    unittest.main()
