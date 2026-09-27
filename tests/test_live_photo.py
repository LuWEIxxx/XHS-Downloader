import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from source.application.download import Download
from source.application.live_photo import (
    atom,
    inspect_live_pair,
    parse_atoms,
    write_live_jpeg,
    write_live_mov,
)


def make_minimal_mov(path: Path) -> None:
    """Build a small structurally valid video-track MOV for metadata tests."""
    ftyp = atom(b"ftyp", b"qt  " + b"\x00\x00\x00\x00" + b"qt  ")
    media = b"\x00\x00\x00\x00"
    mdat = atom(b"mdat", media)
    chunk_offset = len(ftyp) + 8
    mvhd = atom(
        b"mvhd",
        b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, 1000) + b"\x00" * 80,
    )
    tkhd = atom(b"tkhd", b"\x00\x00\x00\x07" + struct.pack(">III", 0, 0, 1) + b"\x00" * 64)
    hdlr = atom(b"hdlr", b"\x00" * 8 + b"vide" + b"\x00" * 16)
    stco = atom(b"stco", b"\x00" * 4 + struct.pack(">II", 1, chunk_offset))
    stbl = atom(b"stbl", stco)
    minf = atom(b"minf", stbl)
    mdia = atom(b"mdia", hdlr + minf)
    trak = atom(b"trak", tkhd + mdia)
    moov = atom(b"moov", mvhd + trak)
    path.write_bytes(ftyp + mdat + moov)


class LivePhotoTests(unittest.TestCase):
    def test_v2_pair_has_matching_identifier_and_front_moov(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_source = root / "source.jpeg"
            image = Image.new("RGB", (32, 24), (20, 100, 180))
            image.save(image_source, format="JPEG")
            movie_source = root / "source.mov"
            make_minimal_mov(movie_source)
            image_output = root / "result.JPG"
            movie_output = root / "result.MOV"
            asset_id = "9D8D3C69-299C-4FEC-9C30-1D8126A8D489"

            write_live_jpeg(image_source, image_output, asset_id)
            write_live_mov(movie_source, movie_output, asset_id)

            self.assertEqual(asset_id, inspect_live_pair(image_output, movie_output))
            top_level = parse_atoms(movie_output.read_bytes())
            atom_types = [item.type for item in top_level]
            self.assertLess(atom_types.index(b"moov"), atom_types.index(b"mdat"))
            self.assertGreater(image_output.stat().st_size, image_source.stat().st_size)
            self.assertGreater(movie_output.stat().st_size, movie_source.stat().st_size)

    def test_conversion_copies_final_pair_to_destination_mount(self):
        """转换中间文件留在临时目录，最终文件在目标目录内完成改名。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "ram-stage"
            target = root / "downloads"
            stage.mkdir()
            target.mkdir()
            cover = stage / "cover.jpeg"
            motion = stage / "motion.mp4"
            Image.new("RGB", (32, 24), (20, 100, 180)).save(cover, format="JPEG")
            make_minimal_mov(motion)

            Download._Download__convert_live_photo(
                cover,
                motion,
                target,
                "pair",
            )

            self.assertTrue((target / "pair.JPG").is_file())
            self.assertTrue((target / "pair.MOV").is_file())
            self.assertFalse((target / "pair.JPG.part").exists())
            self.assertFalse((target / "pair.MOV.part").exists())
            self.assertTrue(inspect_live_pair(target / "pair.JPG", target / "pair.MOV"))


class DownloadTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_image_task_uses_three_tuple_shape(self):
        """普通图文任务应按 (url, name, format) 解包。"""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            downloader = object.__new__(Download)
            downloader.image_download = True
            downloader.live_download = False
            downloader.image_format = "jpeg"
            downloader.image_format_list = ["jpeg", "jpg", "png"]
            calls = []

            async def fake_download(self, *args):
                calls.append(args)
                return True

            with patch.object(Download, "_Download__download", fake_download):
                result = await downloader.run(
                    ["https://cdn.example/image.jpg"],
                    [None],
                    None,
                    target,
                    "work",
                    "图文",
                    0,
                )

            self.assertEqual([True], result)
            self.assertEqual(
                (
                    "https://cdn.example/image.jpg",
                    target,
                    "work_1",
                    "jpeg",
                    0,
                    None,
                    None,
                ),
                calls[0],
            )


if __name__ == "__main__":
    unittest.main()
