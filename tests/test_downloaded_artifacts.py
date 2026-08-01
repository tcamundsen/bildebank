from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank.downloaded_artifacts import (
    download_https_file,
    download_https_file_resumable,
    ensure_directory_without_links,
    safe_extract_zip,
)


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        url: str = "https://downloads.example/artifact.zip",
        content_length: str | None = None,
        status: int = 200,
        content_range: str | None = None,
    ) -> None:
        self._stream = io.BytesIO(content)
        self._url = url
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        if content_range is not None:
            self.headers["Content-Range"] = content_range

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_download_uses_exclusive_file_and_enforces_streamed_size(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "artifact.zip"
    with (
        patch(
            "bildebank.downloaded_artifacts.urllib.request.urlopen",
            return_value=FakeResponse(b"12345"),
        ),
        pytest.raises(RuntimeError, match="overskred"),
    ):
        download_https_file(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=4,
        )
    assert not destination.exists()

    destination.write_bytes(b"behold")
    with (
        patch(
            "bildebank.downloaded_artifacts.urllib.request.urlopen",
            return_value=FakeResponse(b"new"),
        ) as urlopen,
        pytest.raises(FileExistsError),
    ):
        download_https_file(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=10,
        )
    assert destination.read_bytes() == b"behold"
    urlopen.assert_not_called()


def test_download_rejects_oversized_header_and_https_downgrade(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "artifact.zip"
    with (
        patch(
            "bildebank.downloaded_artifacts.urllib.request.urlopen",
            return_value=FakeResponse(b"", content_length="11"),
        ),
        pytest.raises(RuntimeError, match="større enn"),
    ):
        download_https_file(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=10,
        )
    assert not destination.exists()

    with (
        patch(
            "bildebank.downloaded_artifacts.urllib.request.urlopen",
            return_value=FakeResponse(b"ok", url="http://downloads.example/file"),
        ),
        pytest.raises(RuntimeError, match="uten HTTPS"),
    ):
        download_https_file(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=10,
        )
    assert not destination.exists()


def test_resumable_download_uses_range_and_keeps_partial_on_failure(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "artifact.part"
    destination.write_bytes(b"123")
    responses = [
        FakeResponse(
            b"45",
            status=206,
            content_length="2",
            content_range="bytes 3-4/5",
        ),
    ]
    progress: list[tuple[int, int]] = []

    with patch(
        "bildebank.downloaded_artifacts.urllib.request.urlopen",
        side_effect=responses,
    ) as urlopen:
        assert download_https_file_resumable(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=5,
            expected_size=5,
            progress=lambda current, total: progress.append((current, total)),
        ) == 5

    request = urlopen.call_args.args[0]
    assert request.get_header("Range") == "bytes=3-"
    assert destination.read_bytes() == b"12345"
    assert progress == [(3, 5), (5, 5)]

    destination.write_bytes(b"123")
    with (
        patch(
            "bildebank.downloaded_artifacts.urllib.request.urlopen",
            return_value=FakeResponse(
                b"45",
                status=206,
                content_length="2",
                content_range="bytes 2-4/5",
            ),
        ),
        pytest.raises(RuntimeError, match="Content-Range"),
    ):
        download_https_file_resumable(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=5,
            expected_size=5,
        )
    assert destination.read_bytes() == b"123"


def test_resumable_download_restarts_when_server_ignores_range(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "artifact.part"
    destination.write_bytes(b"old")

    with patch(
        "bildebank.downloaded_artifacts.urllib.request.urlopen",
        return_value=FakeResponse(b"fresh", status=200, content_length="5"),
    ):
        download_https_file_resumable(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=5,
            expected_size=5,
        )

    assert destination.read_bytes() == b"fresh"


def test_resumable_download_preserves_bytes_after_stream_error(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "artifact.part"

    class FailingResponse(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            chunk = super().read(size)
            if chunk:
                return chunk
            raise OSError("forbindelsen forsvant")

    with (
        patch(
            "bildebank.downloaded_artifacts.urllib.request.urlopen",
            return_value=FailingResponse(b"123", content_length="5"),
        ),
        pytest.raises(OSError, match="forsvant"),
    ):
        download_https_file_resumable(
            "https://downloads.example/artifact.zip",
            destination,
            user_agent="test",
            max_bytes=5,
            expected_size=5,
        )

    assert destination.read_bytes() == b"123"

def test_ensure_directory_rejects_linked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Kan ikke opprette symlink: {exc}")

    with pytest.raises(RuntimeError, match="lenker"):
        ensure_directory_without_links(linked / "child", label="testmappe")
    assert not (outside / "child").exists()


def test_safe_extract_rejects_case_collision_special_file_and_size(
    tmp_path: Path,
) -> None:
    collision = tmp_path / "collision.zip"
    with zipfile.ZipFile(collision, "w") as archive:
        archive.writestr("bin/tool.exe", b"one")
        archive.writestr("BIN/TOOL.EXE", b"two")
    with pytest.raises(RuntimeError, match="duplisert filsti"):
        safe_extract_zip(
            collision,
            tmp_path / "collision",
            label="Test",
            max_members=10,
            max_uncompressed_bytes=100,
        )

    special = tmp_path / "special.zip"
    member = zipfile.ZipInfo("bin/device")
    member.create_system = 3
    member.external_attr = (stat.S_IFIFO | 0o600) << 16
    with zipfile.ZipFile(special, "w") as archive:
        archive.writestr(member, b"bad")
    with pytest.raises(RuntimeError, match="utrygg filtype"):
        safe_extract_zip(
            special,
            tmp_path / "special",
            label="Test",
            max_members=10,
            max_uncompressed_bytes=100,
        )

    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("large.bin", b"12345")
    with pytest.raises(RuntimeError, match="større enn"):
        safe_extract_zip(
            oversized,
            tmp_path / "oversized",
            label="Test",
            max_members=10,
            max_uncompressed_bytes=4,
        )


def test_safe_extract_writes_valid_members_exclusively(tmp_path: Path) -> None:
    archive_path = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("folder/tool.exe", b"tool")

    destination = tmp_path / "extract"
    safe_extract_zip(
        archive_path,
        destination,
        label="Test",
        max_members=10,
        max_uncompressed_bytes=100,
    )
    assert (destination / "folder" / "tool.exe").read_bytes() == b"tool"
