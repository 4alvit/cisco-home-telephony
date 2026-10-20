import os
from pathlib import Path
import tempfile
import unittest

from tools.firmware_lab.storage import create_private_output, read_regular, write_private


class FirmwareLabStorageTest(unittest.TestCase):
    def test_fresh_private_output_and_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = create_private_output(root / ".local/lab/run", root)
            write_private(output, "blob-0001.elf", b"synthetic")
            self.assertEqual(read_regular(output / "blob-0001.elf"), b"synthetic")
            self.assertEqual((output.stat().st_mode & 0o777), 0o700)
            self.assertEqual(((output / "blob-0001.elf").stat().st_mode & 0o777), 0o600)
            with self.assertRaises(ValueError):
                create_private_output(output, root)
            with self.assertRaises(FileExistsError):
                write_private(output, "blob-0001.elf", b"replacement")
            with self.assertRaises(ValueError):
                write_private(output, "../escape", b"bad")

    def test_output_boundary_and_symlink_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                create_private_output(root / "public", root)
            with self.assertRaises(ValueError):
                create_private_output(root / ".local", root)
            (root / "elsewhere").mkdir()
            (root / ".local").symlink_to(root / "elsewhere", target_is_directory=True)
            with self.assertRaises(ValueError):
                create_private_output(root / ".local/run", root)
            (root / ".local").unlink()
            (root / ".local").mkdir()
            (root / ".local/link").symlink_to(root / "elsewhere", target_is_directory=True)
            with self.assertRaises(ValueError):
                create_private_output(root / ".local/link/run", root)

    def test_read_rejects_large_link_and_special(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input").write_bytes(b"0123456789")
            with self.assertRaises(ValueError):
                read_regular(root / "input", limit=9)
            (root / "link").symlink_to(root / "input")
            with self.assertRaises(ValueError):
                read_regular(root / "link")
            if hasattr(os, "mkfifo"):
                os.mkfifo(root / "pipe")
                with self.assertRaises(ValueError):
                    read_regular(root / "pipe")


if __name__ == "__main__":
    unittest.main()
