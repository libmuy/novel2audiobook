"""
测试 src/web_server.py：只读 HTTP 浏览服务的路由与安全校验。
全部通过 Flask test_client() 进行，不监听真实端口。
"""
import os
import json

import pytest

from src.web_server import create_app
from src.asset_gen import generate_assets, MockAudioGenBackend


SAMPLE_SPECS = {
    "ambience": {
        "test_rain": {
            "description": "测试环境音",
            "prompt": "rain sound for test",
            "negative_prompt": "",
            "duration_sec": 2.0,
            "seed": 1,
        },
        "missing_ambience": {
            "description": "未生成的环境音",
            "prompt": "wind sound never generated",
            "negative_prompt": "",
            "duration_sec": 2.0,
            "seed": 2,
        },
    },
    "sfx": {
        "test_clang": {
            "description": "测试音效",
            "prompt": "metal clang for test",
            "negative_prompt": "",
            "duration_sec": 1.0,
            "seed": 3,
        },
    },
}


@pytest.fixture
def web_app(tmp_project_dir):
    """构造一个带真实生成素材 + 假章节成片的隔离测试环境，返回 Flask test_client"""
    assets_dir = os.path.join(tmp_project_dir, "assets")
    mock = MockAudioGenBackend()
    generate_assets(
        specs=SAMPLE_SPECS,
        assets_dir=assets_dir,
        only={"test_rain", "test_clang"},  # missing_ambience 故意不生成，用于测试 MISSING 状态
        backend_map={"ambience": mock, "sfx": mock},
    )

    # 一个带成片的章节
    ch_with_mp3 = os.path.join(tmp_project_dir, "chapters", "ch_0001", "output")
    os.makedirs(ch_with_mp3, exist_ok=True)
    with open(os.path.join(ch_with_mp3, "chapter_0001.mp3"), "wb") as f:
        f.write(b"fake mp3 bytes")

    # 一个没有成片的章节
    ch_no_mp3 = os.path.join(tmp_project_dir, "chapters", "ch_0002")
    os.makedirs(ch_no_mp3, exist_ok=True)
    with open(os.path.join(ch_no_mp3, "raw.txt"), "w", encoding="utf-8") as f:
        f.write("占位文本")

    app = create_app(project_root=tmp_project_dir)
    app.config["TESTING"] = True

    # 让视图函数里 load_asset_specs() 能读到本次测试用的 spec（写一份到隔离目录）
    import yaml
    spec_path = os.path.join(assets_dir, "asset_specs.yaml")
    with open(spec_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(SAMPLE_SPECS, f, allow_unicode=True)

    return app.test_client()


def test_index_returns_200_with_nav_links(web_app):
    resp = web_app.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/assets" in body
    assert "/chapters" in body


def test_assets_page_lists_generated_and_missing(web_app):
    resp = web_app.get("/assets")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "test_rain" in body
    assert "test_clang" in body
    assert "missing_ambience" in body
    assert "MISSING" in body


def test_serve_asset_valid_returns_audio(web_app):
    resp = web_app.get("/assets/sfx/test_clang.wav")
    assert resp.status_code == 200
    assert resp.content_type.startswith("audio/")


def test_serve_asset_unknown_kind_404(web_app):
    resp = web_app.get("/assets/not_a_kind/foo.wav")
    assert resp.status_code == 404


def test_serve_asset_unregistered_name_404_even_if_file_exists(web_app, tmp_project_dir):
    # 在磁盘上放一个未登记在 spec 里的 wav，白名单应基于 spec 拒绝它
    rogue_path = os.path.join(tmp_project_dir, "assets", "sfx", "not_registered.wav")
    with open(rogue_path, "wb") as f:
        f.write(b"rogue")
    resp = web_app.get("/assets/sfx/not_registered.wav")
    assert resp.status_code == 404


def test_serve_asset_path_traversal_rejected(web_app):
    resp = web_app.get("/assets/sfx/..%2f..%2f..%2fetc%2fpasswd.wav")
    assert resp.status_code in (404, 400)


def test_chapters_page_shows_mp3_and_no_mp3(web_app):
    resp = web_app.get("/chapters")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ch_0001" in body
    assert "ch_0002" in body
    assert "尚无成片" in body


def test_serve_chapter_audio_valid_returns_mp3(web_app):
    resp = web_app.get("/chapters/ch_0001/audio.mp3")
    assert resp.status_code == 200
    assert resp.content_type == "audio/mpeg"


def test_serve_chapter_audio_missing_mp3_404(web_app):
    resp = web_app.get("/chapters/ch_0002/audio.mp3")
    assert resp.status_code == 404


def test_serve_chapter_audio_unknown_chapter_404(web_app):
    resp = web_app.get("/chapters/ch_9999/audio.mp3")
    assert resp.status_code == 404


def test_serve_chapter_audio_path_traversal_rejected(web_app):
    resp = web_app.get("/chapters/..%2f..%2fetc/audio.mp3")
    assert resp.status_code in (404, 400)
