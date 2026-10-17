from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest

from bildebank.server_assets import SERVER_CSS, SERVER_JS
from bildebank.server_faces import (
    face_matches_item_html,
    face_overlay_item_html,
    faces_overlay_html,
)


@pytest.mark.parametrize("matches", [False, True])
def test_face_image_and_box_share_a_rotation_inside_a_layout_frame(
    matches: bool,
) -> None:
    item = {"view_rotation_degrees": 90, "media_width": 1600, "media_height": 1000}
    face = {
        "faceId": 1,
        "score": 0.9,
        "left": 20,
        "top": 30,
        "boxWidth": 10,
        "boxHeight": 15,
    }
    if matches:
        body = face_matches_item_html(item, "/file/1", face, [])
    else:
        body = face_overlay_item_html(item, "/file/1", face, [])

    assert '<div class="face-media-frame">' in body
    assert body.index('class="face-media-frame"') < body.index('class="lightbox-media"')
    assert 'data-view-rotation="90"' in body
    assert 'class="face-box"' in body


def test_face_dialogs_keep_images_between_text_and_buttons_in_chromium() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Requires Node.js and Playwright")
    available = subprocess.run(
        [node, "-e", "require.resolve('playwright')"], capture_output=True, check=False
    )
    if available.returncode:
        pytest.skip("Requires Playwright (set NODE_PATH if installed separately)")

    fixtures = []
    for width, height, rotation, orientation in [
        (1600, 1000, 0, 1),
        (1000, 1600, 0, 1),
        (1600, 1000, 90, 1),
        (1600, 1000, 270, 1),
        (1000, 1600, 90, 1),
        (1600, 1000, 180, 1),
        (1600, 1000, 0, 6),
        (1600, 1000, 90, 6),
    ]:
        output = BytesIO()
        exif = Image.Exif()
        exif[274] = orientation
        Image.new("RGB", (width, height), "#738290").save(output, "JPEG", exif=exif)
        image_url = (
            "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()
        )
        item = {
            "target_path": "portrait.jpg",
            "view_rotation_degrees": rotation,
            "media_width": width,
            "media_height": height,
        }
        face = {
            "faceId": 1,
            "score": 0.9,
            "left": 20,
            "top": 30,
            "boxWidth": 10,
            "boxHeight": 15,
            "suggestions": [{"name": "Kari", "score": 0.9}],
        }
        fixtures.append(
            {
                "name": f"{width}x{height}, rotation={rotation}, EXIF={orientation}",
                "rotation": rotation,
                "overlay": faces_overlay_html(item),
                "assignment": face_overlay_item_html(
                    item, image_url, face, [{"name": "Kari"}]
                ),
                "matches": face_matches_item_html(item, image_url, face, []),
            }
        )

    result = subprocess.run(
        [node, str(Path(__file__).with_name("face_overlay_layout.cjs"))],
        input=json.dumps({"css": SERVER_CSS, "js": SERVER_JS, "fixtures": fixtures}),
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
