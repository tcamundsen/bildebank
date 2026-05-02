from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from bilder.media import image_dimensions, media_date


def atom(atom_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + atom_type + payload


def minimal_mp4_with_creation_date(date: dt.date) -> bytes:
    epoch = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
    created = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc)
    seconds = int((created - epoch).total_seconds())
    mvhd_payload = (
        b"\x00\x00\x00\x00"
        + seconds.to_bytes(4, "big")
        + seconds.to_bytes(4, "big")
        + (1000).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
    )
    return atom(b"ftyp", b"isom\x00\x00\x00\x00isom") + atom(b"moov", atom(b"mvhd", mvhd_payload))


def riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) % 2 else b""
    return chunk_id + len(payload).to_bytes(4, "little") + payload + padding


def minimal_avi_with_creation_date(date: dt.date) -> bytes:
    date_text = date.strftime("%Y-%m-%d").encode("ascii") + b"\x00"
    info = b"INFO" + riff_chunk(b"ICRD", date_text)
    body = b"AVI " + riff_chunk(b"LIST", info)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def minimal_avi_with_idit_outside_info() -> bytes:
    idit = riff_chunk(b"IDIT", b"Mon Mar 12 19:54:18 2007\x00")
    strl = riff_chunk(b"LIST", b"strl" + idit)
    body = b"AVI " + riff_chunk(b"LIST", b"hdrl" + strl)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def jpeg_with_xmp_date(date: str) -> bytes:
    xmp = f"""<?xpacket begin=''?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:CreateDate="{date}" />
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>""".encode("utf-8")
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return b"\xff\xd8" + segment + b"\xff\xd9"


def jpeg_with_photoshop_xmp_date(date: str) -> bytes:
    xmp = f"""<?xpacket begin=''?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:CreateDate="{date}" />
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>""".encode("utf-8")
    payload = b"Photoshop 3.0\x00" + xmp
    segment = b"\xff\xed" + (len(payload) + 2).to_bytes(2, "big") + payload
    return b"\xff\xd8" + segment + b"\xff\xd9"


def jpeg_with_exif_datetime(date: str) -> bytes:
    date_bytes = date.encode("ascii") + b"\x00"
    tiff_header = b"MM\x00*\x00\x00\x00\x08"
    ifd0_offset = 8
    ifd0_size = 2 + 12 + 4
    exif_ifd_offset = ifd0_offset + ifd0_size
    exif_ifd_size = 2 + 12 + 4
    date_offset = exif_ifd_offset + exif_ifd_size
    ifd0 = (
        (1).to_bytes(2, "big")
        + (0x8769).to_bytes(2, "big")
        + (4).to_bytes(2, "big")
        + (1).to_bytes(4, "big")
        + exif_ifd_offset.to_bytes(4, "big")
        + (0).to_bytes(4, "big")
    )
    exif_ifd = (
        (1).to_bytes(2, "big")
        + (0x9003).to_bytes(2, "big")
        + (2).to_bytes(2, "big")
        + len(date_bytes).to_bytes(4, "big")
        + date_offset.to_bytes(4, "big")
        + (0).to_bytes(4, "big")
    )
    payload = b"Exif\x00\x00" + tiff_header + ifd0 + exif_ifd + date_bytes
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return b"\xff\xd8" + segment + b"\xff\xd9"


def minimal_png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_payload = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )
    return signature + atom(b"IHDR", ihdr_payload)


class MediaDateTests(unittest.TestCase):
    def test_mp4_creation_date_is_used_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video_without_date_in_name.mp4"
            path.write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            result = media_date(path)

            self.assertEqual(result.date, dt.date(2010, 7, 8))
            self.assertEqual(result.source, "metadata")

    def test_avi_info_date_is_used_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oktnov07 063.avi"
            path.write_bytes(minimal_avi_with_creation_date(dt.date(2007, 10, 31)))

            result = media_date(path)

            self.assertEqual(result.date, dt.date(2007, 10, 31))
            self.assertEqual(result.source, "metadata")

    def test_avi_idit_outside_info_is_used_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "karneval og syden mars07 147.avi"
            path.write_bytes(minimal_avi_with_idit_outside_info())

            result = media_date(path)

            self.assertEqual(result.date, dt.date(2007, 3, 12))
            self.assertEqual(result.source, "metadata")

    def test_jpeg_xmp_date_is_used_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xmp-only.jpg"
            path.write_bytes(jpeg_with_xmp_date("2007-03-12T19:54:18+01:00"))

            result = media_date(path)

            self.assertEqual(result.date, dt.date(2007, 3, 12))
            self.assertEqual(result.source, "metadata")

    def test_jpeg_photoshop_xmp_date_is_used_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photoshop.jpg"
            path.write_bytes(jpeg_with_photoshop_xmp_date("2009:01:29 12:09:44+01:00"))

            result = media_date(path)

            self.assertEqual(result.date, dt.date(2009, 1, 29))
            self.assertEqual(result.source, "metadata")

    def test_jpeg_exif_slash_date_is_used_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "htc.jpg"
            path.write_bytes(jpeg_with_exif_datetime("2011/08/28 15:13:00"))

            result = media_date(path)

            self.assertEqual(result.date, dt.date(2011, 8, 28))
            self.assertEqual(result.source, "metadata")

    def test_jpeg_exif_is_used_when_file_has_png_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "20160129_170511.png"
            path.write_bytes(jpeg_with_exif_datetime("2016:01:29 17:04:27"))

            result = media_date(path)

            self.assertEqual(result.date, dt.date(2016, 1, 29))
            self.assertEqual(result.source, "metadata")

    def test_png_dimensions_are_read_without_external_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.png"
            path.write_bytes(minimal_png(640, 480))

            result = image_dimensions(path)

            self.assertIsNotNone(result)
            self.assertEqual(result.width, 640)
            self.assertEqual(result.height, 480)


if __name__ == "__main__":
    unittest.main()
