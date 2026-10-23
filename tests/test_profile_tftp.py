import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profile_tftp", ROOT / "scripts/profile_tftp.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def site():
    return {"node_ipv4": "192.168.50.10", "lan_cidr": "192.168.50.0/24"}


def inventory():
    return {"phones": [{"model": "CP-7945G", "verified": True,
                        "mac": "02:00:00:00:00:0a", "ipv4": "192.168.50.100",
                        "firmware_load": "SIP45.9-4-2SR4-3S"}]}


PROFILE_NAME = "SEP02000000000A.cnf.xml"


def files():
    return {PROFILE_NAME: """<?xml version="1.0" encoding="UTF-8"?>
<device><loadInformation>SIP45.9-4-2SR4-3S</loadInformation>
<sipProfile><dialTemplate>dialplan.xml</dialTemplate></sipProfile>
<vendorConfig><g722CodecSupport>2</g722CodecSupport></vendorConfig></device>
""", "dialplan.xml": '<DIALTEMPLATE><TEMPLATE MATCH="2.." Timeout="0"/></DIALTEMPLATE>\n'}


class ProfilePolicyTests(unittest.TestCase):
    def test_only_explicit_verified_phone_hosts_are_sorted_without_mutating_inputs(self):
        config, phones = site(), inventory()
        config.update(phone_cidrs=["192.168.50.0/24"],
                      softphones=[{"source_ipv4": "192.168.50.77"}],
                      unrelated_setting={"credentials_file": "/must/not/be/read"})
        phones["phones"].append(dict(phones["phones"][0], mac="02000000000B", ipv4="192.168.50.20"))
        before = copy.deepcopy((config, phones))
        self.assertEqual(profile.policy(config, phones), {
            "version": 1, "bind_ipv4": "192.168.50.10",
            "phone_ipv4": ["192.168.50.100", "192.168.50.20"]})
        self.assertEqual((config, phones), before)

    def test_no_unrelated_provision_fields_are_required(self):
        self.assertEqual(profile.policy({"node_ipv4": "192.168.50.10"}, inventory())["phone_ipv4"],
                         ["192.168.50.100"])

    def test_empty_malformed_or_oversized_inventory_is_rejected(self):
        for value in (None, [], {}, {"phones": []}, {"phones": {}},
                      {"phones": [None]}, {"phones": inventory()["phones"] * 51}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.policy(site(), value)

    def test_unverified_wrong_model_and_invalid_load_are_rejected(self):
        for key, value in (("verified", False), ("verified", 1), ("verified", "true"),
                           ("model", "CP-7965G"), ("model", "softphone"),
                           ("firmware_load", "SIP42.9-4-2"),
                           ("firmware_load", "SIP45.9-4-2.loads"),
                           ("firmware_load", "../SIP45.9-4-2"),
                           ("firmware_load", None)):
            phones = inventory()
            phones["phones"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                profile.policy(site(), phones)

    def test_missing_required_phone_fields_are_rejected(self):
        for key in ("model", "verified", "mac", "ipv4", "firmware_load"):
            phones = inventory()
            del phones["phones"][0][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                profile.policy(site(), phones)

    def test_invalid_and_duplicate_canonical_mac_are_rejected(self):
        for value in (None, 123, "", "000000000000", "01:00:00:00:00:01",
                      "FF:FF:FF:FF:FF:FF", "02000000000G", "02000000000A/evil"):
            phones = inventory()
            phones["phones"][0]["mac"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.policy(site(), phones)
        for value in ("02000000000A", "02-00-00-00-00-0A"):
            phones = inventory()
            phones["phones"].append(dict(phones["phones"][0], mac=value, ipv4="192.168.50.101"))
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.policy(site(), phones)

    def test_nonliteral_public_special_and_cidr_phone_addresses_are_rejected(self):
        for value in (None, 3232248420, True, "phone.local", "192.168.050.100", "192.168.50.100/32",
                      "192.168.50.0/24", "192.168.50.10", "192.168.50.0", "192.168.50.255",
                      "127.0.0.1", "169.254.1.1", "100.64.1.1", "8.8.8.8", "0.0.0.0",
                      "224.0.0.1", "255.255.255.255", "fc00::1", "::ffff:192.168.50.100"):
            phones = inventory()
            phones["phones"][0]["ipv4"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.policy(site(), phones)

    def test_invalid_bind_address_and_duplicate_phone_address_are_rejected(self):
        for value in (None, 123, "0.0.0.0", "127.0.0.1", "8.8.8.8", "192.168.50.0",
                      "192.168.50.255", "192.168.50.10/32"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.policy(dict(site(), node_ipv4=value), inventory())
        phones = inventory()
        phones["phones"].append(dict(phones["phones"][0], mac="02000000000B"))
        with self.assertRaises(ValueError):
            profile.policy(site(), phones)

    def test_routed_private_host_uses_its_actual_mask_not_a_guessed_slash24(self):
        config, phones = site(), inventory()
        config["phone_cidrs"] = ["172.16.2.0/23"]
        phones["phones"][0]["ipv4"] = "172.16.2.255"
        self.assertEqual(profile.policy(config, phones)["phone_ipv4"], ["172.16.2.255"])
        phones["phones"][0]["ipv4"] = "172.16.3.255"
        with self.assertRaises(ValueError):
            profile.policy(config, phones)


class AddressMergeTests(unittest.TestCase):
    def setUp(self):
        self.inventory = inventory()
        self.inventory["phones"][0].pop("ipv4")
        self.inventory["phones"][0]["lines"] = [{"extension": "201", "label": "Office"}]
        self.addresses = {"version": 1, "phones": [{"mac": "02000000000A", "ipv4": "192.168.50.100"}]}

    def test_exact_join_preserves_inventory_and_does_not_alias_or_mutate_inputs(self):
        original, addresses = copy.deepcopy(self.inventory), copy.deepcopy(self.addresses)
        result = profile.merge_addresses(self.inventory, self.addresses)
        self.assertEqual(result["phones"][0], dict(original["phones"][0], ipv4="192.168.50.100"))
        self.assertEqual(self.inventory, original)
        self.assertEqual(self.addresses, addresses)
        self.assertEqual(profile.policy(site(), result)["phone_ipv4"], ["192.168.50.100"])
        result["phones"][0]["lines"][0]["label"] = "changed"
        self.assertEqual(self.inventory, original)

    def test_rows_join_by_canonical_mac_not_position(self):
        self.inventory["phones"].append(dict(self.inventory["phones"][0], mac="02000000000B"))
        self.addresses["phones"].insert(0, {"mac": "02-00-00-00-00-0b", "ipv4": "192.168.50.101"})
        result = profile.merge_addresses(self.inventory, self.addresses)
        self.assertEqual([phone["ipv4"] for phone in result["phones"]],
                         ["192.168.50.100", "192.168.50.101"])

    def test_existing_matching_address_is_preserved_but_conflict_is_rejected(self):
        self.inventory["phones"][0]["ipv4"] = "192.168.50.100"
        self.assertEqual(profile.merge_addresses(self.inventory, self.addresses), self.inventory)
        self.inventory["phones"][0]["ipv4"] = "192.168.50.101"
        with self.assertRaises(ValueError):
            profile.merge_addresses(self.inventory, self.addresses)

    def test_only_versioned_minimal_schema_is_accepted(self):
        for value in (None, [], {}, {"phones": self.addresses["phones"]},
                      dict(self.addresses, version=True), dict(self.addresses, version=2),
                      dict(self.addresses, source="evidence-report"),
                      {"version": 1, "phones": [{"mac": "02000000000A", "ipv4": "192.168.50.100", "primary": "201"}]},
                      {"version": 1, "phones": [{"mac": "02000000000A"}]},
                      {"version": 1, "phones": [None]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.merge_addresses(self.inventory, value)

    def test_missing_extra_uninventoried_and_duplicate_rows_are_rejected(self):
        for rows in ([], self.addresses["phones"] * 2,
                     [{"mac": "02000000000B", "ipv4": "192.168.50.100"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                profile.merge_addresses(self.inventory, {"version": 1, "phones": rows})
        self.inventory["phones"].append(dict(self.inventory["phones"][0], mac="02000000000B"))
        for row in ({"mac": "02:00:00:00:00:0a", "ipv4": "192.168.50.101"},
                    {"mac": "02000000000B", "ipv4": "192.168.50.100"}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                profile.merge_addresses(self.inventory, {"version": 1, "phones": self.addresses["phones"] + [row]})
        self.inventory["phones"][1]["mac"] = "02000000000A"
        with self.assertRaises(ValueError):
            profile.merge_addresses(self.inventory, {"version": 1, "phones": self.addresses["phones"] + [
                {"mac": "02000000000B", "ipv4": "192.168.50.101"}]})

    def test_non_host_or_non_private_addresses_are_rejected_before_merge(self):
        for value in (None, True, "phone.local", "192.168.50.100/32", "8.8.8.8", "127.0.0.1", "fc00::1"):
            self.addresses["phones"][0]["ipv4"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.merge_addresses(self.inventory, self.addresses)


class ProfileFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS's temporary directory may be reached through /var -> /private/var.
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "tftp"
        self.directory.mkdir()
        for name, content in files().items():
            (self.directory / name).write_text(content, encoding="utf-8")

    def read(self, directory=None):
        return profile.read_profiles(self.directory if directory is None else directory, site(), inventory())

    def test_exact_profile_only_directory_round_trips_without_firmware(self):
        # Sibling firmware and wallpaper are not inspected or required.
        (self.root / "firmware").mkdir()
        (self.root / "firmware" / "unrelated.loads").write_bytes(b"not read")
        (self.root / "wallpaper").mkdir()
        self.assertEqual(self.read(), files())
        self.assertEqual(list(self.read()), sorted(files()))

    def test_real_provision_profile_is_accepted_without_generating_or_reading_secrets(self):
        spec = importlib.util.spec_from_file_location("profile_test_provision", ROOT / "scripts/provision.py")
        provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provision)
        phone = dict(inventory()["phones"][0], label="Office", extension="201")
        xml = provision.phone_xml(phone, site()["node_ipv4"], "192.168.50.1", {"201": "synthetic"})
        (self.directory / PROFILE_NAME).write_text(xml, encoding="utf-8")
        self.assertEqual(self.read()[PROFILE_NAME], xml)

    def test_every_extra_entry_is_rejected_including_firmware_and_uninventoried_xml(self):
        for name in ("SIP45.9-4-2.loads", "apps45.sbn", "term45.default.loads", "XMLDefault.cnf.xml",
                     "SEP02000000000B.cnf.xml", ".hidden", "readme.txt", "policy.json"):
            extra = self.directory / name
            extra.write_text("unexpected")
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.read()
            extra.unlink()
        (self.directory / "Desktops").mkdir()
        with self.assertRaises(ValueError):
            self.read()

    def test_missing_profile_or_dialplan_is_rejected(self):
        for name in files():
            path = self.directory / name
            content = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.read()
            path.write_bytes(content)

    def test_symlink_input_directory_and_ancestor_are_rejected(self):
        link = self.root / "linked"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.read(link)
        link.unlink()
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.read(link / "tftp")
        with self.assertRaises(ValueError):
            self.read(self.directory / ".." / "tftp")

    def test_expected_path_cannot_be_a_link_directory_fifo_or_oversized_file(self):
        path = self.directory / PROFILE_NAME
        path.unlink()
        outside = self.root / "outside.xml"
        outside.write_text(files()[PROFILE_NAME])
        for target in (outside, self.root / "absent"):
            path.symlink_to(target)
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.read()
            path.unlink()
        path.mkdir()
        with self.assertRaises(ValueError):
            self.read()
        path.rmdir()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            self.read()
        path.unlink()
        path.write_bytes(b" " * (profile.MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.read()

    def test_symlink_swap_between_stat_and_open_is_rejected(self):
        original_open = os.open
        outside = self.root / "outside.xml"
        outside.write_text(files()[PROFILE_NAME])
        changed = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal changed
            if path == PROFILE_NAME and not changed:
                changed = True
                (self.directory / PROFILE_NAME).unlink()
                (self.directory / PROFILE_NAME).symlink_to(outside)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(profile.os, "open", side_effect=swapping_open), self.assertRaises(ValueError):
            self.read()
        self.assertTrue(changed)

    def test_changed_directory_or_already_read_file_is_rejected(self):
        original_listdir = os.listdir
        for add_extra in (True, False):
            calls = 0

            def changing_listdir(fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    if add_extra:
                        (self.directory / "late.loads").write_text("unexpected")
                    else:
                        (self.directory / PROFILE_NAME).write_text(files()[PROFILE_NAME] + "\n")
                return original_listdir(fd)

            with self.subTest(extra=add_extra), patch.object(profile.os, "listdir", side_effect=changing_listdir):
                with self.assertRaises(ValueError):
                    self.read()
            if add_extra:
                (self.directory / "late.loads").unlink()

    def test_invalid_encoding_and_xml_are_rejected_without_disclosing_input(self):
        for content in (b"\xff", files()[PROFILE_NAME].encode("utf-16"),
                        files()[PROFILE_NAME].encode("utf-16le"),
                        files()[PROFILE_NAME].encode("utf-32le"), b"", b"<private-secret-broken"):
            (self.directory / PROFILE_NAME).write_bytes(content)
            with self.subTest(content=content[:12]), self.assertRaises(ValueError) as caught:
                self.read()
            self.assertNotIn("private-secret", str(caught.exception))

    def test_doctype_and_entity_declarations_are_rejected_in_both_xml_types(self):
        for name in files():
            for declaration in ('<!DOCTYPE device SYSTEM "file:///private/secret">',
                                '<!DOCTYPE device [<!ENTITY secret "private-value">]>',
                                '<!ENTITY secret SYSTEM "https://example.invalid/secret">'):
                values = files()
                values[name] = declaration + values[name]
                with self.subTest(name=name, declaration=declaration), self.assertRaises(ValueError):
                    profile.validate_profiles(values, site(), inventory())

    def test_wrong_missing_duplicate_and_misplaced_profile_fields_are_rejected(self):
        original = files()[PROFILE_NAME]
        for needle, wrong in (("SIP45.9-4-2SR4-3S", "SIP45.other"), (">2<", ">1<"),
                              ("dialplan.xml", "../dialplan.xml"), ("<device>", "<Device>")):
            values = files()
            values[PROFILE_NAME] = original.replace(needle, wrong)
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                profile.validate_profiles(values, site(), inventory())
        for element in ("<loadInformation>SIP45.9-4-2SR4-3S</loadInformation>",
                        "<g722CodecSupport>2</g722CodecSupport>",
                        "<dialTemplate>dialplan.xml</dialTemplate>"):
            for changed in (original.replace(element, ""),
                            original.replace(element, element + element),
                            original.replace(element, "<other>" + element + "</other>"),
                            original.replace(element, element.replace(">", ' extra="x">', 1))):
                values = dict(files(), **{PROFILE_NAME: changed})
                with self.subTest(element=element, changed=changed), self.assertRaises(ValueError):
                    profile.validate_profiles(values, site(), inventory())

    def test_profiles_must_match_each_inventory_firmware_load(self):
        phones, values = inventory(), files()
        phones["phones"].append(dict(phones["phones"][0], mac="02000000000B", ipv4="192.168.50.101",
                                     firmware_load="SIP45.other"))
        second = "SEP02000000000B.cnf.xml"
        values[second] = values[PROFILE_NAME].replace("SIP45.9-4-2SR4-3S", "SIP45.other")
        self.assertEqual(profile.validate_profiles(values, site(), phones), values)
        values[second] = values[PROFILE_NAME]
        with self.assertRaises(ValueError):
            profile.validate_profiles(values, site(), phones)

    def test_wrong_dialplan_root_mapping_type_and_size_limits_are_rejected(self):
        for xml in ("<device/>", "<dialtemplate/>", '<DIALTEMPLATE xmlns="wrong"/>'):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                profile.validate_profiles(dict(files(), **{"dialplan.xml": xml}), site(), inventory())
        for value in (None, [], {"dialplan.xml": files()["dialplan.xml"]},
                      dict(files(), **{PROFILE_NAME: files()[PROFILE_NAME].encode()})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.validate_profiles(value, site(), inventory())
        with patch.object(profile, "MAX_FILE_BYTES", 20), self.assertRaises(ValueError):
            profile.validate_profiles(files(), site(), inventory())
        with patch.object(profile, "MAX_TOTAL_BYTES", 20), self.assertRaises(ValueError):
            profile.validate_profiles(files(), site(), inventory())


if __name__ == "__main__":
    unittest.main()
