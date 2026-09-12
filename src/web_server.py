"""
只读 HTTP 浏览服务：在浏览器里查看/试听素材库（assets/ambience、assets/sfx）
与章节最终成片（chapters/*/output/*.mp3）。

不做任何生成/写入操作，只聚合展示 src/asset_gen.py 与 src/status_tracker.py
已有的状态数据；文件服务路由一律先用这些函数返回的"真实登记/存在的条目集合"
做白名单校验，再用 flask.send_from_directory 返回，避免路径遍历读到项目外文件。
"""
import os

from flask import Flask, abort, render_template_string, url_for, send_from_directory

from src.asset_gen import VALID_KINDS, load_asset_specs, get_asset_status_list
from src.status_tracker import get_all_chapters_status
from src.utils import get_project_root, normalize_chapter_id


_NAV_HTML = """
<nav style="margin-bottom:1.5em;">
  <a href="{{ url_for('index') }}">首页</a> ·
  <a href="{{ url_for('assets_page') }}">素材库</a> ·
  <a href="{{ url_for('chapters_page') }}">章节成片</a>
</nav>
"""

_BASE_STYLE = """
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2em; color: #222; }
  table { border-collapse: collapse; width: 100%; margin-top: 1em; }
  th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 14px; }
  th { background: #f5f5f5; }
  audio { height: 32px; }
  .muted { color: #999; }
</style>
"""

_INDEX_TPL = """
<!doctype html><html><head><meta charset="utf-8"><title>novel2audiobook 资源浏览</title>
""" + _BASE_STYLE + """</head><body>
""" + _NAV_HTML + """
<h1>novel2audiobook 资源浏览</h1>
<p>只读浏览界面，用于试听已生成的音效/环境音素材库与章节最终成片，不涉及生成操作。</p>
<ul>
  <li><a href="{{ url_for('assets_page') }}">素材库</a>（{{ asset_count }} 条，其中 {{ asset_ok_count }} 条已生成）</li>
  <li><a href="{{ url_for('chapters_page') }}">章节成片</a>（{{ chapter_count }} 个章节，其中 {{ chapter_mp3_count }} 个已有成片）</li>
</ul>
</body></html>
"""

_ASSETS_TPL = """
<!doctype html><html><head><meta charset="utf-8"><title>素材库 - novel2audiobook</title>
""" + _BASE_STYLE + """</head><body>
""" + _NAV_HTML + """
<h1>素材库</h1>
<table>
  <tr><th>名称</th><th>类型</th><th>说明</th><th>状态</th><th>引擎</th><th>时长(ms)</th><th>试听</th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.name }}</td>
    <td>{{ row.kind }}</td>
    <td>{{ descriptions.get(row.kind, {}).get(row.name, '') }}</td>
    <td>{{ row.status }}</td>
    <td>{{ row.engine }}</td>
    <td>{{ row.duration_ms if row.duration_ms is not none else '-' }}</td>
    <td>
      {% if row.status != 'MISSING' %}
      <audio controls src="{{ url_for('serve_asset', kind=row.kind, name=row.name) }}"></audio>
      {% else %}<span class="muted">—</span>{% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
</body></html>
"""

_CHAPTERS_TPL = """
<!doctype html><html><head><meta charset="utf-8"><title>章节成片 - novel2audiobook</title>
""" + _BASE_STYLE + """</head><body>
""" + _NAV_HTML + """
<h1>章节成片</h1>
<table>
  <tr><th>章节 ID</th><th>状态</th><th>Cache 文件数</th><th>播放</th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.chapter_id }}</td>
    <td>{{ row.status }}</td>
    <td>{{ row.audio_cache_count }}</td>
    <td>
      {% if row.mp3 %}
      <audio controls src="{{ url_for('serve_chapter_audio', chapter_id=row.chapter_id) }}"></audio>
      {% else %}<span class="muted">尚无成片</span>{% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
</body></html>
"""


def create_app(project_root: str = None) -> Flask:
    """构造只读浏览 Flask app；project_root 缺省用项目根目录，测试时可传隔离临时目录"""
    root = project_root or get_project_root()
    assets_dir = os.path.join(root, "assets")
    chapters_dir = os.path.join(root, "chapters")

    app = Flask(__name__)

    @app.route("/")
    def index():
        asset_rows = get_asset_status_list(assets_dir=assets_dir)
        chapter_rows = get_all_chapters_status(chapters_dir=chapters_dir)
        return render_template_string(
            _INDEX_TPL,
            asset_count=len(asset_rows),
            asset_ok_count=sum(1 for r in asset_rows if r["status"].startswith("OK")),
            chapter_count=len(chapter_rows),
            chapter_mp3_count=sum(1 for r in chapter_rows if r["mp3"]),
        )

    @app.route("/assets")
    def assets_page():
        specs = load_asset_specs(os.path.join(assets_dir, "asset_specs.yaml"))
        rows = get_asset_status_list(specs=specs, assets_dir=assets_dir)
        descriptions = {
            kind: {name: spec.get("description", "") for name, spec in kind_specs.items()}
            for kind, kind_specs in specs.items()
        }
        return render_template_string(_ASSETS_TPL, rows=rows, descriptions=descriptions)

    @app.route("/assets/<kind>/<name>.wav")
    def serve_asset(kind, name):
        if kind not in VALID_KINDS:
            abort(404)
        specs = load_asset_specs(os.path.join(assets_dir, "asset_specs.yaml"))
        if name not in specs.get(kind, {}):
            abort(404)
        directory = os.path.join(assets_dir, kind)
        return send_from_directory(directory, f"{name}.wav")

    @app.route("/chapters")
    def chapters_page():
        rows = get_all_chapters_status(chapters_dir=chapters_dir)
        return render_template_string(_CHAPTERS_TPL, rows=rows)

    @app.route("/chapters/<chapter_id>/audio.mp3")
    def serve_chapter_audio(chapter_id):
        ch_id = normalize_chapter_id(chapter_id)
        if not ch_id.startswith("ch_") or "/" in ch_id or ".." in ch_id:
            abort(404)
        output_dir = os.path.join(chapters_dir, ch_id, "output")
        if not os.path.isdir(output_dir):
            abort(404)
        mp3_files = [f for f in os.listdir(output_dir) if f.endswith(".mp3")]
        if not mp3_files:
            abort(404)
        return send_from_directory(output_dir, mp3_files[0])

    return app


def run_server(host: str = "127.0.0.1", port: int = 8090, project_root: str = None):
    """启动本地 HTTP 服务，供 `python cli.py serve` 调用"""
    app = create_app(project_root=project_root)
    app.run(host=host, port=port)
