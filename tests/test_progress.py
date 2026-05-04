from __future__ import annotations

import unittest
from io import StringIO

from bilder.progress import ProgressLine


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


class ProgressLineTests(unittest.TestCase):
    def test_overwrites_when_stream_is_tty(self) -> None:
        stream = TtyStringIO()
        progress = ProgressLine(stream)

        progress.write("første")
        progress.write("andre")
        progress.finish()

        self.assertEqual(stream.getvalue(), "\rførste\033[K\randre\033[K\n")

    def test_uses_normal_lines_when_stream_is_not_tty(self) -> None:
        stream = StringIO()
        progress = ProgressLine(stream)

        progress.write("første")
        progress.write("andre")
        progress.finish()

        self.assertEqual(stream.getvalue(), "første\nandre\n")
