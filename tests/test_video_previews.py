from __future__ import annotations

import subprocess
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

from bildebank import db
from bildebank.db import init_database
from bildebank.ffmpeg_tools import FFmpegTools
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_files import ByteRange, describe_server_file, parse_byte_range, resolve_video_preview_file
from bildebank.server_pages import item_page_html
from bildebank.static_browser import static_browser_item
from bildebank.video_previews import (
    VideoProbe,
    active_video_preview_candidates,
    ensure_video_preview,
    run_make_video_previews,
    video_preview_absolute_path,
    video_preview_relative_path,
)
from tests.db_test_helpers import register_target_file
from tests.cli_helpers import capture_cli


def make_avi(target: Path, name: str = "film.AVI") -> tuple[dict[str, object], Path]:
    return make_video(target, name, b"unchanged AVI original")


def make_video(target: Path, name: str, content: bytes) -> tuple[dict[str, object], Path]:
    relative = Path("2024/01") / name
    original = target / relative
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(content)
    file_id = register_target_file(target, relative)
    conn = db.connect(target)
    try:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        assert row is not None
        return dict(row), original
    finally:
        conn.close()


def test_preview_path_is_content_addressed() -> None:
    digest = "a" * 64
    assert video_preview_relative_path(digest) == Path("video-previews/v1/aa", f"{digest}.mp4")
    with pytest.raises(ValueError, match="Ugyldig SHA-256"):
        video_preview_relative_path("not-a-hash")


def test_active_candidates_include_avi_and_3gp_but_not_other_videos(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    init_database(target)
    avi, _original = make_avi(target)
    three_gp, _original = make_video(target, "phone.3GP", b"3gp")
    mp4_path = target / "2024/01/other.mp4"
    mp4_path.write_bytes(b"mp4")
    register_target_file(target, Path("2024/01/other.mp4"))

    assert [int(row["id"]) for row in active_video_preview_candidates(target)] == [
        int(avi["id"]),
        int(three_gp["id"]),
    ]


@pytest.mark.parametrize("filename", ["film.AVI", "phone.3GP"])
def test_ensure_preview_uses_browser_profile_and_keeps_original(tmp_path: Path, filename: str) -> None:
    target = tmp_path / "collection"
    init_database(target)
    item, original = make_video(target, filename, b"unchanged video original")
    original_content = original.read_bytes()
    tools = FFmpegTools(Path("ffmpeg.exe"), Path("ffprobe.exe"), "8.1.2", True)
    input_probe = VideoProbe(10.0, 641, 481, "mjpeg", "yuvj420p", "tt", "mp3")
    output_probe = VideoProbe(10.0, 640, 480, "h264", "yuv420p", "progressive", "aac")
    commands: list[list[str]] = []

    def transcode(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"valid mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with (
        patch("bildebank.video_previews.probe_video", side_effect=[input_probe, output_probe]),
        patch("bildebank.video_previews.subprocess.run", side_effect=transcode),
    ):
        preview = ensure_video_preview(target, item, tools)

    assert preview == video_preview_absolute_path(target, str(item["sha256"]))
    assert preview.read_bytes() == b"valid mp4"
    assert original.read_bytes() == original_content
    command = commands[0]
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-crf") + 1] == "20"
    assert command[command.index("-preset") + 1] == "medium"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-b:a") + 1] == "160k"
    video_filter = command[command.index("-vf") + 1]
    assert "bwdif=" in video_filter
    assert "out_range=tv" in video_filter
    assert "format=pix_fmts=yuv420p" in video_filter
    assert command[command.index("-color_range") + 1] == "tv"
    assert command[command.index("-movflags") + 1] == "+faststart"


def test_failed_preview_does_not_replace_cache_or_original(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    init_database(target)
    item, original = make_avi(target)
    original_content = original.read_bytes()
    tools = FFmpegTools(Path("ffmpeg.exe"), Path("ffprobe.exe"), "8.1.2", True)
    input_probe = VideoProbe(10.0, 640, 480, "mpeg4", "yuv420p", "progressive", None)

    with (
        patch("bildebank.video_previews.probe_video", return_value=input_probe),
        patch(
            "bildebank.video_previews.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="conversion failed"),
        ),
        pytest.raises(RuntimeError, match="conversion failed"),
    ):
        ensure_video_preview(target, item, tools)

    assert original.read_bytes() == original_content
    assert not video_preview_absolute_path(target, str(item["sha256"])).exists()
    assert not list(target.rglob("*.partial"))


def test_dry_run_reports_missing_without_tools_or_writes(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    init_database(target)
    item, _original = make_avi(target)

    stats = run_make_video_previews(target, None, dry_run=True)

    assert stats.total == 1
    assert stats.checked == 1
    assert stats.created == 0
    assert not video_preview_absolute_path(target, str(item["sha256"])).exists()


def test_cli_dry_run_does_not_resolve_or_install_ffmpeg(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    init_database(target)
    make_avi(target)

    with patch("bildebank.cli.resolve_or_install_ffmpeg_tools", side_effect=AssertionError("must not install")):
        code, stdout, stderr = capture_cli(
            ["--target", str(target), "make-video-previews", "--dry-run"]
        )

    assert code == 0
    assert "mangler=1" in stdout
    assert stderr == ""


def test_range_parsing_supports_normal_open_and_suffix_ranges() -> None:
    assert parse_byte_range("bytes=2-5", 10) == ByteRange(2, 5)
    assert parse_byte_range("bytes=7-", 10).length == 3  # type: ignore[union-attr]
    assert parse_byte_range("bytes=-4", 10).start == 6  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        parse_byte_range("bytes=20-30", 10)
    with pytest.raises(ValueError):
        parse_byte_range("bytes=0-1,4-5", 10)


@pytest.mark.parametrize(
    ("filename", "format_name"),
    [("film.AVI", "AVI"), ("phone.3GP", "3GP")],
)
def test_server_uses_preview_and_toolbar_downloads_original(
    tmp_path: Path,
    filename: str,
    format_name: str,
) -> None:
    target = tmp_path / "collection"
    init_database(target)
    item, _original = make_video(target, filename, b"video original")
    preview = video_preview_absolute_path(target, str(item["sha256"]))
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"mp4 preview")

    served = resolve_video_preview_file(target, str(item["id"]))
    month_nav = {key: None for key in ("previous_year", "next_year", "previous_month", "next_month")}
    server_html = item_page_html(
        target,
        item,
        None,
        None,
        month_nav,
        face_enabled=False,
        openclip_enabled=False,
        manual_person_controls_enabled=False,
    )
    read_only_html = item_page_html(
        target,
        item,
        None,
        None,
        month_nav,
        face_enabled=False,
        openclip_enabled=False,
        manual_person_controls_enabled=False,
        read_only=True,
    )
    static_item = static_browser_item(item, Path(str(item["target_path"])), target=target)
    controls_start = server_html.index('<nav class="controls"')
    controls_html = server_html[controls_start : server_html.index("</nav>", controls_start)]
    read_only_controls_start = read_only_html.index('<nav class="controls"')
    read_only_controls_html = read_only_html[
        read_only_controls_start : read_only_html.index("</nav>", read_only_controls_start)
    ]

    assert served.path == preview
    assert served.content_type == "video/mp4"
    assert f'/video-preview/{item["id"]}' in server_html
    download_link = f'href="/file/{item["id"]}" download'
    assert download_link in controls_html
    assert f'>{format_name}</a>' in controls_html
    assert f'title="Last ned originalfilen {filename}"' in controls_html
    assert download_link in read_only_controls_html
    assert "Åpne original" not in server_html
    assert "video-original-link" not in server_html
    assert static_item["playbackUrl"] == preview.relative_to(target).as_posix()
    assert static_item["originalUrl"] == str(item["target_path"])


def test_server_keeps_file_card_when_video_preview_is_missing(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    init_database(target)
    item, _original = make_avi(target)
    month_nav = {key: None for key in ("previous_year", "next_year", "previous_month", "next_month")}

    server_html = item_page_html(
        target,
        item,
        None,
        None,
        month_nav,
        face_enabled=False,
        openclip_enabled=False,
        manual_person_controls_enabled=False,
    )
    controls_start = server_html.index('<nav class="controls"')
    controls_html = server_html[controls_start : server_html.index("</nav>", controls_start)]

    assert "AVI-videoen mangler MP4-avspillingskopi" in server_html
    assert "Kjør «Lag videoavspillingskopier» i Bildebank." in server_html
    assert 'class="file-card"' in server_html
    assert f'href="/file/{item["id"]}"' in server_html
    assert f'href="/file/{item["id"]}"' not in controls_html


def test_other_video_formats_do_not_get_original_format_button(tmp_path: Path) -> None:
    target = tmp_path / "collection"
    init_database(target)
    item, _original = make_video(target, "film.mp4", b"mp4 video")
    month_nav = {key: None for key in ("previous_year", "next_year", "previous_month", "next_month")}

    server_html = item_page_html(
        target,
        item,
        None,
        None,
        month_nav,
        face_enabled=False,
        openclip_enabled=False,
        manual_person_controls_enabled=False,
    )

    assert f'<video src="/file/{item["id"]}" controls></video>' in server_html
    assert "Last ned originalfilen" not in server_html
    assert ">MP4</a>" not in server_html


def test_server_file_response_honors_range_without_loading_whole_file(tmp_path: Path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"0123456789")

    class FakeHandler:
        headers = {"Range": "bytes=3-6"}

        def __init__(self) -> None:
            self.status: HTTPStatus | None = None
            self.response_headers: dict[str, str] = {}
            self.wfile = BytesIO()

        def send_response(self, status: HTTPStatus) -> None:
            self.status = status

        def send_header(self, name: str, value: str) -> None:
            self.response_headers[name] = value

        def respond_timing_headers(self) -> None:
            pass

        def end_headers(self) -> None:
            pass

    handler = FakeHandler()
    BildebankRequestHandler.respond_server_file(handler, describe_server_file(media))  # type: ignore[arg-type]

    assert handler.status == HTTPStatus.PARTIAL_CONTENT
    assert handler.response_headers["Content-Range"] == "bytes 3-6/10"
    assert handler.response_headers["Content-Length"] == "4"
    assert handler.response_headers["Accept-Ranges"] == "bytes"
    assert handler.wfile.getvalue() == b"3456"
