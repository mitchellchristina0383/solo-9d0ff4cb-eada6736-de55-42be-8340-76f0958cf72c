"""Flask 救援页：上传样盘、上报读出扇区、下载同进度镜像与缺失清单。"""

from __future__ import annotations

import base64
import json
import os
from typing import Tuple

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
)
from werkzeug.utils import secure_filename

from .storage import (
    MAX_IMAGE_SIZE,
    SECTOR_SIZE,
    JobExistsError,
    JobStorage,
    StorageError,
)


def create_app(data_dir: str | None = None) -> Flask:
    template_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "templates")
    )
    app = Flask(__name__, template_folder=template_dir)
    data_dir = data_dir or os.environ.get("RESCUE_DATA_DIR", "/data")
    app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_SIZE + 1 * 1024 * 1024
    storage = JobStorage(data_dir)
    app.extensions["rescue_storage"] = storage

    # ---------------------------------------------------------------- #
    # 页面
    # ---------------------------------------------------------------- #
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            max_mib=MAX_IMAGE_SIZE // (1024 * 1024),
            sector_size=SECTOR_SIZE,
        )

    # ---------------------------------------------------------------- #
    # 任务
    # ---------------------------------------------------------------- #
    @app.post("/api/job")
    def upload_sample():
        upload = request.files.get("image")
        if upload is None:
            return _error(400, "缺少 image 上传字段")
        filename = secure_filename(upload.filename or "sample.img")
        if not filename:
            return _error(400, "文件名无效")
        try:
            state = storage.create_job(upload.stream)
        except JobExistsError as exc:
            return _error(409, str(exc))
        except StorageError as exc:
            return _error(400, str(exc))
        return jsonify({"ok": True, "progress": state}), 201

    @app.get("/api/job")
    def job_status():
        if not storage.has_job():
            return _error(404, "尚无救援任务，请先上传样盘")
        return jsonify({"ok": True, "progress": storage.get_state()})

    @app.get("/api/missing")
    def current_missing():
        """当前最新缺失清单（便捷接口；强一致请走 /api/snapshots）。"""
        if not storage.has_job():
            return _error(404, "尚无救援任务")
        state = storage.get_state()
        return jsonify(
            {
                "ok": True,
                "sector_count": state["sector_count"],
                "missing_count": state["missing_count"],
                "missing": state["missing"],
            }
        )

    @app.delete("/api/job")
    def reset_job():
        storage.reset()
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- #
    # 读盘上报
    # ---------------------------------------------------------------- #
    @app.post("/api/sectors")
    def report_sectors():
        """恢复工具上报本轮读出的扇区。

        支持两种载荷：
        1. JSON：{"sectors": [{"sector": 12, "data": "<base64>"}]}
        2. 单扇区二进制：query 参数 ?sector=N，body 恰好 512 字节
        已保存的扇区重复上报幂等返回 already_saved；失败的扇区不上报
        即保持缺失，下一轮继续补读。
        """
        if not storage.has_job():
            return _error(404, "尚无救援任务")

        if request.content_type and request.content_type.startswith("application/json"):
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("sectors"), list
            ):
                return _error(400, "JSON 载荷需为 {\"sectors\": [{\"sector\", \"data\"}]}")
            items = []
            parse_errors = []
            for entry in payload["sectors"]:
                parsed = _parse_json_sector(entry)
                if isinstance(parsed, tuple):
                    items.append(parsed)
                else:
                    # 单条解析失败不拖垮整批，记录后继续处理其他条目
                    bad_sector = entry.get("sector") if isinstance(entry, dict) else "?"
                    parse_errors.append({"sector": bad_sector, "error": parsed})
        else:
            sector_raw = request.args.get("sector", "")
            try:
                index = int(sector_raw)
            except ValueError:
                return _error(400, "query 参数 sector 必须为非负整数")
            data = request.get_data(cache=False)
            if len(data) != SECTOR_SIZE:
                return _error(
                    400,
                    f"单扇区二进制上报长度必须为 {SECTOR_SIZE} 字节，实际 {len(data)}",
                )
            items, parse_errors = [(index, data)], []

        result = storage.save_batch(items)
        result["rejected"] = parse_errors + result["rejected"]
        return jsonify({"ok": True, **result})

    @app.get("/api/sectors/<int:index>")
    def get_sector(index: int):
        """取回某个已救出的原始扇区；缺失返回 409。"""
        try:
            data = storage.read_saved_sector(index)
        except StorageError as exc:
            return _error(400, str(exc))
        if data is None:
            return _error(409, f"扇区 {index} 仍缺失")
        return Response(data, mimetype="application/octet-stream")

    # ---------------------------------------------------------------- #
    # 同进度快照下载
    # ---------------------------------------------------------------- #
    @app.post("/api/snapshots")
    def create_snapshot():
        try:
            snapshot = storage.create_snapshot()
        except StorageError as exc:
            return _error(404, str(exc))
        return jsonify({"ok": True, "snapshot": snapshot}), 201

    @app.get("/api/snapshots/<snapshot_id>/image.bin")
    def download_image(snapshot_id: str):
        generator = storage.open_snapshot_image(snapshot_id)
        if generator is None:
            abort(404, description="快照不存在或已过期")
        state = storage.get_state()
        resp = Response(generator, mimetype="application/octet-stream")
        resp.headers["Content-Length"] = str(
            state["sector_count"] * SECTOR_SIZE
        )
        resp.headers["X-Snapshot-Id"] = snapshot_id
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="rescue-{snapshot_id}.img"'
        )
        return resp

    @app.get("/api/snapshots/<snapshot_id>/missing.json")
    def download_missing(snapshot_id: str):
        payload = storage.snapshot_missing_list(snapshot_id)
        if payload is None:
            abort(404, description="快照不存在或已过期")
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        resp = Response(text, mimetype="application/json")
        resp.headers["X-Snapshot-Id"] = snapshot_id
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="missing-{snapshot_id}.json"'
        )
        return resp

    @app.errorhandler(413)
    def too_large(_err):
        return _error(413, f"请求体超过上限（样盘最大 {MAX_IMAGE_SIZE} 字节）")

    @app.errorhandler(404)
    def not_found(err):
        return _error(404, getattr(err, "description", "资源不存在"))

    return app


def _parse_json_sector(entry) -> Tuple[int, bytes] | str:
    if not isinstance(entry, dict) or "sector" not in entry or "data" not in entry:
        return "每个扇区需包含 sector 与 data 字段"
    try:
        index = int(entry["sector"])
    except (TypeError, ValueError):
        return f"扇区号非法：{entry.get('sector')!r}"
    try:
        data = base64.b64decode(entry["data"], validate=True)
    except (ValueError, TypeError):
        return f"扇区 {index} 的 data 不是合法 base64"
    return index, data


def _error(status: int, message: str):
    return jsonify({"ok": False, "error": message}), status
