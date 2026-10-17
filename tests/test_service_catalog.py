"""Services are enabled routes, not registrations or a new dialing permission."""
import contextlib
import copy
import csv
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco
import export_contacts
import provision
import service_catalog
from test_cisco_directory import inventory
import test_configure_gateway
import test_phone_services
from test_phone_gateway import config, FakeAMI, gateway
import test_sync_directory


def services():
    return [{'number': '708', 'label': 'Deti FM: Toddlers', 'kind': 'radio'},
            {'number': '600', 'label': 'Echo test', 'kind': 'diagnostic'}]


def listing():
    data = inventory()
    data['dial_allowlist'] += ['708']
    data['services'] = services()
    return data


class ServiceCatalogTests(unittest.TestCase):
    def test_disabled_optional_features_do_not_invent_routes(self):
        base = service_catalog.build({}, has_phones=True)
        self.assertEqual([item['number'] for item in base], ['600', '601', '620'])
        self.assertEqual([item['number'] for item in service_catalog.build({}, has_phones=False)], ['600', '601'])
        self.assertEqual(service_catalog.build({}, has_phones=True, allowed_numbers=['600', '630', '701']),
                         [{'number': '600', 'label': 'Echo test', 'kind': 'diagnostic'}])

    def test_radio_selection_and_assist_use_actual_enabled_options(self):
        site = {'node_ipv4': '192.168.50.10',
                'home_assistant': {'enabled': True, 'ami_client_ipv4': '192.168.50.11'},
                'alarms': {'enabled': True}, 'radio': {'enabled': True, 'stations': ['deti-toddlers', 'echo']}}
        entries = service_catalog.build(site, has_phones=True)
        self.assertEqual([item['number'] for item in entries], ['600', '601', '620', '640', '702', '708'])
        site['home_assistant']['assist'] = {'enabled': True, 'host_ipv4': '192.168.50.11'}
        self.assertIn('630', [item['number'] for item in service_catalog.build(site, has_phones=True)])
        site['radio']['stations'].append('unknown-station')
        with self.assertRaises(ValueError): service_catalog.build(site, has_phones=True)

    def test_only_external_accounts_with_a_target_have_shortcuts(self):
        accounts = [{'provider': 'callcentric', 'number': '801', 'target': None},
                    {'provider': 'sip2sip', 'number': '802', 'target': '4444'},
                    {'provider': 'linphone', 'number': '803', 'target': None}]
        with patch.object(provision.external_sip, 'validate', return_value={'accounts': accounts}):
            entries = service_catalog.build({}, has_phones=True)
            external = [item for item in entries if item['kind'] == 'external']
            self.assertEqual(external, [{'number': '802', 'label': 'SIP2SIP echo test', 'kind': 'external'}])
            accounts[-1]['target'] = 'private-peer-name'
            entries = service_catalog.build({}, has_phones=True)
            self.assertEqual(entries[-1], {'number': '803', 'label': 'Linphone configured peer', 'kind': 'external'})
            self.assertNotIn('private-peer-name', json.dumps(entries))
            accounts[-1]['target'] = 'thetestcall'
            entries = service_catalog.build({}, has_phones=True)
            self.assertEqual(entries[-1]['label'], 'The Test Call (English menu)')

    def test_optional_incoming_group_and_saved_alarm_routes_are_explicit(self):
        fixture = test_phone_services.ServiceProvisioning(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        site = copy.deepcopy(fixture.site)
        owners = {'201': '201', '202': '201'}
        entries = service_catalog.build(site, has_phones=True, group_members=['201', '209'], line_owners=owners)
        self.assertIn({'number': '621', 'label': 'Incoming calls group', 'kind': 'group'}, entries)
        self.assertIn({'number': '641', 'label': 'Toggle saved alarm', 'kind': 'automation'}, entries)
        with self.assertRaisesRegex(ValueError, 'primary'):
            service_catalog.build(site, has_phones=True, group_members=['201'], line_owners=owners)
        site['inbound_ring_group']['enabled'] = False
        site['phone_services']['enabled'] = False
        self.assertEqual([entry['number'] for entry in service_catalog.build(site, has_phones=True)],
                         ['600', '601', '620'])

    def test_unprovisionable_alarm_service_refuses_catalog_and_permission_updates(self):
        config_fixture = test_configure_gateway.ConfigureGatewayTests(); config_fixture.setUp()
        sync_fixture = test_sync_directory.SyncDirectoryTests(); sync_fixture.setUp()
        config_fixture.site['phone_services'] = {'enabled': True}
        sync_fixture.site['phone_services'] = {'enabled': True}
        before = copy.deepcopy(sync_fixture.existing)
        with self.assertRaises((ValueError, KeyError)):
            service_catalog.build(sync_fixture.site, has_phones=True,
                                  line_owners={'201': '201', '202': '201'})
        with self.assertRaises((ValueError, KeyError)):
            config_fixture.prepare()
        for sync_services in (False, True):
            with self.subTest(sync_services=sync_services), self.assertRaises((ValueError, KeyError)):
                test_sync_directory.sync_directory.update(
                    sync_fixture.site, sync_fixture.inventory, sync_fixture.existing,
                    sync_services=sync_services)
        self.assertEqual(sync_fixture.existing, before)

    def test_alarm_catalog_requires_valid_credentials_prompts_and_physical_owners(self):
        fixture = test_phone_services.ServiceProvisioning(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owners = {'201': '201', '202': '201'}
        def build():
            return service_catalog.build(fixture.site, has_phones=True, line_owners=owners)
        with self.assertRaisesRegex(ValueError, 'physical Cisco'):
            service_catalog.build(fixture.site, has_phones=True)
        credential = Path(fixture.site['phone_services']['credentials_file'])
        credential.chmod(0o644)
        with self.assertRaises(ValueError): build()
        credential.chmod(0o600)
        prompt = Path(fixture.site['phone_services']['prompts_directory']) / 'enabled.wav'
        prompt.write_bytes(b'not a wave file')
        with self.assertRaises(ValueError): build()

    def test_alarm_settings_receive_only_the_real_physical_line_owner_map(self):
        complete = test_phone_services.ServiceProvisioning(); complete.setUp()
        self.addCleanup(complete.doCleanups)
        config_fixture = test_configure_gateway.ConfigureGatewayTests(); config_fixture.setUp()
        sync_fixture = test_sync_directory.SyncDirectoryTests(); sync_fixture.setUp()
        for fixture in (config_fixture, sync_fixture):
            fixture.site['phone_services'] = complete.site['phone_services']
        with patch.object(provision.phone_services, 'settings', wraps=provision.phone_services.settings) as settings:
            configured = config_fixture.prepare()
            self.assertEqual(settings.call_args.args[1], {'201': '201', '202': '201'})
            synced = test_sync_directory.sync_directory.update(
                sync_fixture.site, sync_fixture.inventory, sync_fixture.existing, sync_services=True)
            self.assertEqual(settings.call_args.args[1], {'201': '201', '202': '201'})
        for result in (configured, synced):
            self.assertIn('641', result['dial_allowlist'])
            self.assertIn({'number': '641', 'label': 'Toggle saved alarm', 'kind': 'automation'}, result['services'])

    def test_catalog_cannot_collide_even_with_an_unallowed_route(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            service_catalog.build({}, has_phones=True, local_numbers=['600'], allowed_numbers=[])
        for entries in (services() + [services()[0]], [{'number': '708', 'kind': 'radio', 'label': 'x\ny'}],
                        [{'number': '708', 'kind': 'radio', 'label': '=formula'}],
                        [{'number': '708', 'kind': 'radio', 'label': 'Name', 'password': 'secret'}]):
            with self.subTest(entries=entries), self.assertRaises(ValueError): service_catalog.validate(entries)

    def test_configure_gateway_derives_dial_and_ring_permissions_from_enabled_routes(self):
        fixture = test_configure_gateway.ConfigureGatewayTests(); fixture.setUp()
        result = fixture.prepare()
        self.assertEqual(result['ring_allowlist'], [])
        self.assertNotIn('630', result['dial_allowlist'])
        self.assertNotIn('640', result['dial_allowlist'])
        fixture.site['alarms'] = {'enabled': True}
        fixture.site['radio'] = {'enabled': True, 'stations': ['deti-lullabies']}
        result = fixture.prepare()
        self.assertEqual(result['ring_allowlist'], ['640'])
        self.assertEqual([x['number'] for x in result['services']], ['600', '601', '620', '640', '709'])
        self.assertIn('709', result['dial_allowlist'])

    def test_sync_never_widens_permissions_or_keeps_disabled_metadata(self):
        fixture = test_sync_directory.SyncDirectoryTests(); fixture.setUp()
        fixture.existing['services'] = [{'number': '701', 'label': 'Previously enabled radio', 'kind': 'radio'}]
        old = copy.deepcopy(fixture.existing)
        result = fixture.update()
        self.assertEqual(result['services'], [{'number': '600', 'label': 'Echo test', 'kind': 'diagnostic'}])
        self.assertEqual(result['dial_allowlist'], old['dial_allowlist'])
        self.assertEqual(result['ring_allowlist'], old['ring_allowlist'])
        self.assertEqual(fixture.existing, old)

    def test_explicit_service_sync_preserves_local_restrictions_and_unknown_routes(self):
        fixture = test_sync_directory.SyncDirectoryTests(); fixture.setUp()
        fixture.existing['dial_allowlist'] = ['201', '209', '600', '630', '701', '880']
        fixture.existing['ring_allowlist'] = ['640', '701', '880']
        fixture.site['radio'] = {'enabled': True, 'stations': ['deti-lullabies']}
        before = copy.deepcopy(fixture.existing)
        result = test_sync_directory.sync_directory.update(
            fixture.site, fixture.inventory, fixture.existing, sync_services=True)
        self.assertEqual(result['dial_allowlist'], ['201', '209', '600', '880', '601', '620', '709'])
        self.assertNotIn('202', result['dial_allowlist'])
        self.assertEqual(result['ring_allowlist'], ['880'])
        self.assertEqual([entry['number'] for entry in result['services']], ['600', '601', '620', '709'])
        self.assertEqual(fixture.existing, before)
        for key in ('api_token', 'api_clients', 'ami', 'bind_ipv4', 'api_port'):
            self.assertEqual(result[key], before[key])

    def test_known_service_codes_are_explicit_and_do_not_include_local_or_arbitrary_numbers(self):
        known = service_catalog.managed_numbers()
        self.assertTrue({'600', '621', '641', '708', '710', '801', '802', '803'} <= known)
        self.assertFalse({'201', '209', '880', '999'} & known)


class GatewayServiceCatalogTests(unittest.TestCase):
    def test_listing_is_metadata_only_and_does_not_add_control_targets(self):
        value = config(); value['services'] = [services()[1]]
        with tempfile.TemporaryDirectory() as temp:
            server = gateway.Gateway(value, temp)
            try:
                with patch.object(gateway, 'AMI', FakeAMI):
                    result = server.listing()
                self.assertEqual(result['services'], [services()[1]])
                self.assertEqual(set(result['services'][0]), {'number', 'label', 'kind'})
                with self.assertRaises(gateway.Refused) as error:
                    server.mutate('600', 'dial', {'line': '600', 'number': '201', 'idempotency_key': 'service-control-test'})
                self.assertEqual(error.exception.reason, 'unknown_phone')
            finally:
                server.db.close()

    def test_gateway_rejects_unsafe_metadata_and_number_collisions(self):
        variants = [None, services(), [services()[1], services()[1]],
                    [{'number': '201', 'label': 'Collision', 'kind': 'group'}],
                    [{'number': '600', 'label': 'bad\x1b[0m', 'kind': 'diagnostic'}],
                    [{'number': '600', 'label': 'Test', 'kind': 'diagnostic', 'target': 'private'}]]
        for entries in variants:
            value = config(); value['services'] = entries
            with self.subTest(entries=entries), self.assertRaises(gateway.Refused): gateway.validate(value)
        value = config(); value['softphones'] = [{'extension': '209'}]
        value['dial_allowlist'] += ['209']
        value['services'] = [{'number': '209', 'label': 'Collision', 'kind': 'group'}]
        with self.assertRaises(gateway.Refused): gateway.validate(value)


class CLIServiceCatalogTests(unittest.TestCase):
    def test_service_section_preserves_existing_phone_directory_schema(self):
        data = listing()
        old = copy.deepcopy(data); old.pop('services')
        result = cisco.directory_result(data, '213')
        self.assertEqual(result['directory'], cisco.directory_result(old, '213')['directory'])
        self.assertEqual([x['number'] for x in result['services']], ['600', '708'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cisco.display(result)
        self.assertIn('708: Deti FM: Toddlers (radio)', output.getvalue())
        self.assertIn('not monitored', output.getvalue())

    def test_unknown_or_service_line_is_never_a_phone_control_target(self):
        with self.assertRaises(cisco.Refused): cisco.validate_inventory(listing(), '708')
        with self.assertRaises(cisco.Refused): cisco.directory_result(listing(), '708')

    def test_invalid_services_fail_before_actions_or_exports(self):
        variants = [None, services() * 2,
                    [{'number': '201', 'label': 'Collision', 'kind': 'group'}],
                    [{'number': '803', 'label': 'Not configured', 'kind': 'external'}],
                    [{'number': '600', 'label': 'Unsafe\ntext', 'kind': 'diagnostic'}],
                    [{'number': '600', 'label': 'Test', 'kind': 'diagnostic', 'token': 'secret'}],
                    [{'number': '600', 'label': 'Test', 'kind': ['diagnostic']}]]
        for entries in variants:
            data = listing(); data['services'] = entries
            with self.subTest(entries=entries):
                with self.assertRaises(cisco.Refused): cisco.validate_inventory(data, '213')
                with self.assertRaises(cisco.Refused): export_contacts.contacts(data)


class ContactExportTests(unittest.TestCase):
    def test_export_includes_both_lines_and_services_without_secrets_or_transient_state(self):
        data = listing()
        data['phones'][0]['password'] = 'secret-value'
        snapshot = export_contacts.contacts(data)
        self.assertEqual(snapshot['schema_version'], 1)
        self.assertEqual(len(snapshot['contacts']), 13)
        self.assertEqual(sum(len(x['numbers']) for x in snapshot['contacts']), 23)
        self.assertEqual(snapshot['contacts'][0], {'id': 'cisco:201', 'label': 'Example Room A',
                                                   'kind': 'cisco', 'numbers': ['201', '202']})
        self.assertEqual(snapshot['contacts'][-1], {'id': 'service:708', 'label': 'Deti FM: Toddlers',
                                                   'kind': 'radio', 'numbers': ['708']})
        encoded = export_contacts.encode(snapshot, 'json').decode()
        for text in ('secret-value', 'online', 'dnd', 'autoanswer', 'capabilities', 'channel'):
            self.assertNotIn(text, encoded)
        rows = list(csv.DictReader(io.StringIO(export_contacts.encode(snapshot, 'csv').decode())))
        self.assertEqual(len(rows), 23)
        self.assertEqual([r['Phone'] for r in rows[:2]], ['201', '202'])
        self.assertEqual(rows[-1]['Name'], 'Deti FM: Toddlers')

    def test_local_write_is_private_exclusive_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'contacts.json'
            before = os.umask(0)
            try:
                export_contacts.write_new(target, b'original')
            finally:
                os.umask(before)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError): export_contacts.write_new(target, b'replace')
            alias = Path(temp) / 'alias'; alias.symlink_to(target)
            with self.assertRaises(FileExistsError): export_contacts.write_new(alias, b'replace')
            self.assertEqual(target.read_bytes(), b'original')

    def test_export_main_only_gets_inventory_and_prints_counts(self):
        calls = []
        class Client:
            def __init__(self, *args): pass
            def request(self, path): calls.append(path); return listing()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'contacts.json'
            with patch.object(cisco, 'Gateway', Client), patch.object(cisco, 'load_config', return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = export_contacts.main(['--output', str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(calls, ['/v1/phones'])
            self.assertIn('13 contacts and 23 numbers', stdout.getvalue())
            self.assertNotIn('Example Room', stdout.getvalue())
            self.assertEqual(len(json.loads(output.read_text())['contacts']), 13)


if __name__ == '__main__':
    unittest.main()
