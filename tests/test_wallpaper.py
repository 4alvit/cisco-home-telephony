import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

from test_k3s import ROOT, configs, fixture, render


def chunk(kind, content):
    return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)


def png(width=320, height=212, depth=8, color=2, pixels=None):
    if pixels is None:
        pixels = (b"\0" + b"\x18\x48\x78" * width) * height
    header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def wallpaper():
    return {
        "List.xml": b'<CiscoIPPhoneImageList><ImageItem Image="TFTP:Desktops/320x212x16/home-thumb.png" '
                    b'URL="TFTP:Desktops/320x212x16/home.png"/></CiscoIPPhoneImageList>',
        "home.png": png(),
        "home-thumb.png": png(80, 53),
    }


def write_wallpaper(directory, files=None):
    directory.mkdir()
    for name, content in (wallpaper() if files is None else files).items():
        (directory / name).write_bytes(content)


class WallpaperTests(unittest.TestCase):
    def test_absent_directory_adds_no_wallpaper_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            absent = render.read_wallpaper(Path(temporary) / "wallpaper")
            self.assertIsNone(absent)
            output = render.render_tftp(fixture(), {"SEP000000000001.cnf.xml": "<device/>"}, absent)
            self.assertEqual(output, render.render_tftp(fixture(), {"SEP000000000001.cnf.xml": "<device/>"}))
            self.assertEqual([item["kind"] for item in output["items"]], ["Secret", "Job"])
            pod = output["items"][-1]["spec"]["template"]["spec"]
            self.assertNotIn("wallpaper", [volume["name"] for volume in pod["volumes"]])
            self.assertNotIn("/wallpaper", pod["initContainers"][0]["command"][2])

    def test_exact_regular_files_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "wallpaper"
            write_wallpaper(directory)
            self.assertEqual(render.read_wallpaper(directory), wallpaper())

    def test_symlink_root_and_files_are_rejected_including_broken_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "wallpaper"
            write_wallpaper(directory)
            for target in (directory, root / "does-not-exist"):
                link = root / "linked-wallpaper"
                link.symlink_to(target, target_is_directory=True)
                with self.assertRaises(ValueError):
                    render.read_wallpaper(link)
                link.unlink()
            for name in wallpaper():
                path = directory / name
                path.unlink()
                target = root / name
                target.write_bytes(wallpaper()[name])
                path.symlink_to(target)
                with self.assertRaises(ValueError):
                    render.read_wallpaper(directory)
                target.unlink()
                with self.assertRaises(ValueError):
                    render.read_wallpaper(directory)
                path.unlink()
                path.write_bytes(wallpaper()[name])

    def test_extra_missing_nested_and_nonregular_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "wallpaper"
            write_wallpaper(directory)
            (directory / "other.png").write_bytes(png())
            with self.assertRaises(ValueError):
                render.read_wallpaper(directory)
            (directory / "other.png").unlink()
            (directory / "home.png").unlink()
            with self.assertRaises(ValueError):
                render.read_wallpaper(directory)
            (directory / "home.png").mkdir()
            with self.assertRaises(ValueError):
                render.read_wallpaper(directory)
            (directory / "home.png").rmdir()
            os.mkfifo(directory / "home.png")
            with self.assertRaises(ValueError):
                render.read_wallpaper(directory)

    def test_oversized_input_and_xml_are_rejected(self):
        for name, content in (("home.png", b"x" * (render.WALLPAPER_MAX_BYTES + 1)),
                              ("List.xml", b" " * 4097)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "wallpaper"
                write_wallpaper(directory, dict(wallpaper(), **{name: content}))
                with self.assertRaises(ValueError):
                    render.read_wallpaper(directory)

    def test_wrong_dimensions_and_non_rgb8_are_rejected(self):
        cases = [("home.png", png(321, 212)), ("home.png", png(320, 211)),
                 ("home-thumb.png", png()), ("home.png", png(color=6)),
                 ("home.png", png(color=3)), ("home.png", png(depth=16))]
        for name, content in cases:
            with self.subTest(name=name, header=content[:33]), self.assertRaises(ValueError):
                render.validate_wallpaper(dict(wallpaper(), **{name: content}))

    def test_corruption_truncation_and_invalid_compressed_pixels_are_rejected(self):
        valid = png()
        raw = (b"\0" + b"\x18\x48\x78" * 320) * 212
        cases = [b"not a PNG", valid[:-1], valid + b"trailing", valid[:-1] + bytes([valid[-1] ^ 1]),
                 png(pixels=raw[:-1]), png(pixels=raw + b"x" * 10000),
                 png(pixels=b"\x05" + raw[1:]),
                 valid[:33] + chunk(b"IDAT", b"not zlib") + chunk(b"IEND", b"")]
        for index, content in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                render.validate_wallpaper(dict(wallpaper(), **{"home.png": content}))

    def test_xml_paths_structure_and_entities_are_rejected(self):
        xml = wallpaper()["List.xml"]
        cases = [xml.replace(b"TFTP:Desktops/", b"https://example.invalid/"),
                 xml.replace(b"home.png", b"../home.png"),
                 xml.replace(b"ImageItem ", b'ImageItem extra="no" '),
                 xml.replace(b"<CiscoIPPhoneImageList>", b'<CiscoIPPhoneImageList extra="no">'),
                 xml.replace(b"/></CiscoIPPhoneImageList>", b"/><Other/></CiscoIPPhoneImageList>"),
                 xml.replace(b"/></CiscoIPPhoneImageList>", b">text</ImageItem></CiscoIPPhoneImageList>"),
                 xml.replace(b"<ImageItem", b"<!--extra--><ImageItem"),
                 b'<!DOCTYPE CiscoIPPhoneImageList [<!ENTITY file SYSTEM "file:///etc/passwd">]>' + xml,
                 b"<broken", b"\xff"]
        for index, content in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                render.validate_wallpaper(dict(wallpaper(), **{"List.xml": content}))

    def test_binary_secret_is_content_addressed_immutable_and_mounted_only_in_stage(self):
        files = wallpaper()
        manifest = render.render_tftp(fixture(), {"SEP000000000001.cnf.xml": "<device/>"}, files)
        secret = manifest["items"][1]
        self.assertEqual(secret["kind"], "Secret")
        self.assertTrue(secret["immutable"])
        self.assertEqual({k: base64.b64decode(v) for k, v in secret["data"].items()}, files)
        pod = manifest["items"][-1]["spec"]["template"]["spec"]
        mount = next(m for m in pod["initContainers"][0]["volumeMounts"] if m["name"] == "wallpaper")
        self.assertEqual(mount, {"name": "wallpaper", "mountPath": "/wallpaper", "readOnly": True})
        volume = next(v for v in pod["volumes"] if v["name"] == "wallpaper")
        self.assertEqual(volume["secret"], {"secretName": secret["metadata"]["name"], "defaultMode": 0o440})
        self.assertNotIn("wallpaper", [m["name"] for m in pod["containers"][0]["volumeMounts"]])
        changed = dict(files, **{"home.png": png(pixels=(b"\0" + b"\0\0\0" * 320) * 212)})
        new = render.render_tftp(fixture(), {"SEP000000000001.cnf.xml": "<device/>"}, changed)
        self.assertNotEqual(secret["metadata"]["name"], new["items"][1]["metadata"]["name"])
        with self.assertRaises(ValueError):
            render.render_tftp(fixture(), {"SEP000000000001.cnf.xml": "<device/>"}, dict(files, extra=b"no"))

    def test_stage_copies_only_approved_files_with_public_read_permissions(self):
        files = wallpaper()
        manifest = render.render_tftp(fixture(), {"SEP000000000001.cnf.xml": "<device/>"}, files)
        command = manifest["items"][-1]["spec"]["template"]["spec"]["initContainers"][0]["command"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("firmware", "config", "wallpaper", "served"):
                (root / name).mkdir()
            (root / "firmware/pilot.sbn").write_bytes(b"synthetic firmware")
            (root / "config/SEP000000000001.cnf.xml").write_text("<device/>")
            # Kubernetes projects Secret keys as symlinks; the local input reader
            # rejects symlinks, while the explicit in-Pod copies must follow these.
            write_wallpaper(root / "wallpaper/..data", files)
            for name in files:
                (root / "wallpaper" / name).symlink_to(Path("..data") / name)
            (root / "wallpaper/extra.png").write_bytes(b"must not be copied")
            script = command[2]
            for name in ("firmware", "config", "wallpaper", "served"):
                script = script.replace("/" + name, shlex.quote(str(root / name)))
            result = subprocess.run(command[:2] + [script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            destination = root / "served/root/Desktops/320x212x16"
            self.assertEqual({p.name for p in destination.iterdir()}, set(files))
            for name, content in files.items():
                self.assertEqual((destination / name).read_bytes(), content)
                self.assertEqual((destination / name).stat().st_mode & 0o777, 0o444)
            for directory in (root / "served/root", destination.parent, destination):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o555)
            self.assertEqual((root / "served/root/pilot.sbn").read_bytes(), b"synthetic firmware")
            (root / "firmware/nested").mkdir()
            result = subprocess.run(command[:2] + [script], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Only regular firmware files", result.stderr)

    def test_renderer_and_preflight_both_validate_sibling_wallpaper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "site.json").write_text(json.dumps(fixture()))
            (root / "asterisk").mkdir()
            for name, content in configs().items():
                (root / "asterisk" / name).write_text(content)
            (root / "tftp").mkdir()
            load = "SIP45.test"
            (root / "tftp/SEP020000000001.cnf.xml").write_text(f"<device><loadInformation>{load}</loadInformation></device>")
            (root / "firmware").mkdir()
            firmware = {load + ".loads": b"synthetic manifest", "test.sbn": b"synthetic component"}
            for name, content in firmware.items():
                (root / "firmware" / name).write_bytes(content)
            (root / "firmware-manifest.json").write_text(json.dumps({"model": "CP-7945G", "reviewed": True,
                "load_id": load, "files": {name: hashlib.sha256(data).hexdigest() for name, data in firmware.items()}}))
            (root / "phones.json").write_text(json.dumps({"phones": [{"model": "CP-7945G", "verified": True,
                "mac": "020000000001", "firmware_load": load}]}))
            write_wallpaper(root / "wallpaper")
            for script in ("render_k3s.py", "preflight.py"):
                command = [sys.executable, str(ROOT / "scripts" / script), "--site", str(root / "site.json"), "--with-tftp"]
                with self.subTest(script=script):
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    (root / "wallpaper/home-thumb.png").write_bytes(png())
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("80x53", result.stderr)
                    (root / "wallpaper/home-thumb.png").write_bytes(wallpaper()["home-thumb.png"])
            (root / "firmware/test.sbn").write_bytes(b"tampered")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
