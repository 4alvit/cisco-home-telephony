"""Station-specific ports must not turn curated radio into an arbitrary proxy."""
import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


radio = load("expanded_radio", "containers/asterisk/radio.py")
provision = load("expanded_provision", "scripts/provision.py")


class ExpandedStations(unittest.TestCase):
    def test_ports_remain_station_and_scheme_specific(self):
        station = radio.catalog()["kavkaz-hit"]
        self.assertEqual(radio.checked_url(station["url"], station).port, 8017)
        for url in (station["url"].replace(":8017", ":443"),
                    station["url"].replace("https:", "http:"),
                    station["url"].replace(":8017", ":8000"),
                    station["url"].replace("kavkazhit.hostingradio.ru", "other.example")):
            with self.subTest(url=url), self.assertRaises(ValueError):
                radio.checked_url(url, station)
        existing = radio.catalog()["echo"]
        parsed = urlsplit(existing["url"])
        for port in (0, 8017):
            with self.subTest(port=port), self.assertRaises(ValueError):
                radio.checked_url(urlunsplit(parsed._replace(netloc=parsed.netloc + f":{port}")), existing)

    def test_invalid_port_policy_is_rejected(self):
        original = radio.catalog()["mega-dance"]
        for policy in ([], {"http": [True]}, {"http": [0]}, {"http": [65536]},
                       {"http": "8000"}, {"http": []}):
            station = dict(original, ports=policy)
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                radio.checked_url(station["url"], station)

    def test_nondefault_port_is_used_for_dns_socket_and_http_host(self):
        station = radio.catalog()["mega-dance"]
        address = ("203.0.113.10", 8000)
        response = Mock(status=200)
        response.getheader.side_effect = lambda key, default=None: {
            "Content-Type": "audio/mpeg"}.get(key, default)
        conn = Mock()
        conn.getresponse.return_value = response
        with patch.object(radio, "public_addresses", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]) as dns, \
                patch.object(radio.http.client, "HTTPConnection", return_value=conn) as http, \
                patch.object(radio.socket, "socket") as sock:
            result = radio.open_stream(station)
        dns.assert_called_once_with("megadanceradio.hopto.org", 8000)
        http.assert_called_once_with("megadanceradio.hopto.org", 8000,
                                     timeout=radio.IO_TIMEOUT)
        sock.return_value.connect.assert_called_once_with(address)
        self.assertEqual(result, (conn, response))

    def test_new_cdn_rules_do_not_accept_similar_external_hosts(self):
        cases = {
            "swiss-classic": "livestreaming-node-3.srg-ssr.ch",
            "mix-1049": "16643.live.streamtheworld.com",
            "rt1-black": "edge07.streamonkey.net",
            "white-noise": "dn710304.ca.archive.org",
        }
        for name, host in cases.items():
            station = radio.catalog()[name]
            radio.checked_url(f"https://{host}/stream", station)
            for bad in (f"{host}.example.org", f"evil-{host}"):
                with self.subTest(name=name, host=bad), self.assertRaises(ValueError):
                    radio.checked_url(f"https://{bad}/stream", station)

    def test_catalog_numbers_and_generated_routes_are_unique(self):
        catalog = radio.catalog()
        extensions = [station["extension"] for station in catalog.values()]
        self.assertEqual(len(extensions), len(set(extensions)))
        expected = {str(extension) for extension in range(711, 723)}
        names = [name for name, station in catalog.items() if station["extension"] in expected]
        self.assertEqual(len(names), 12)
        # Service generation is independent of private line credentials.
        dial, _ = provision.service_dialplan({"radio": {"enabled": True, "stations": names}})
        for name in names:
            station = catalog[name]
            radio.checked_url(station["url"], station)
            self.assertEqual(dial.count(f'exten => {station["extension"]},1,'), 1)
            self.assertIn(f"AGI(radio.py,{name})", dial)


if __name__ == "__main__":
    unittest.main()
