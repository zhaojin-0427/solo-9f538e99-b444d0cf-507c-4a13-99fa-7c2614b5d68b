"""Flask application: month fact intake, knowledge versions, reports."""

from __future__ import annotations

import os
from pathlib import Path

from flask import (
    Flask,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import facts, knowledge
from .db import connect, db_path_from_env, init_db
from .errors import ConflictError, NotFoundError, ValidationError
from .timeutil import display_utc


def _raw_intervals_from_form(form):
    zones = form.getlist("zone")
    starts = form.getlist("valid_from")
    ends = form.getlist("valid_to")
    length = max(len(zones), len(starts), len(ends))
    rows = []
    for index in range(length):
        rows.append(
            {
                "zone": zones[index] if index < len(zones) else "",
                "valid_from": starts[index] if index < len(starts) else "",
                "valid_to": ends[index] if index < len(ends) else "",
            }
        )
    return rows


def _intervals_from_form(form):
    rows = _raw_intervals_from_form(form)
    return [row for row in rows if any(str(value).strip() for value in row.values())]


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=False)
    app.config.update(
        SECRET_KEY=os.environ.get("COLDCHAIN_SECRET", "coldchain-ledger-dev"),
        MAX_CONTENT_LENGTH=facts.MAX_UPLOAD_BYTES + 64 * 1024,
        DB_PATH=str(db_path_from_env()),
    )
    if test_config:
        app.config.update(test_config)
    app.json.ensure_ascii = False

    Path(app.config["DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    init_db(app.config["DB_PATH"])

    # ---------- plumbing ----------

    def get_db():
        if "db" not in g:
            g.db = connect(current_app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_helpers():
        return {"display_utc": display_utc}

    @app.errorhandler(ValidationError)
    def on_validation(exc):
        if request.path.startswith("/api/"):
            return (
                jsonify({"ok": False, "error": "validation_error", "message": exc.message,
                         "details": exc.details}),
                400,
            )
        return render_template("error.html", title="输入不合法", message=exc.message,
                               details=exc.details), 400

    @app.errorhandler(ConflictError)
    def on_conflict(exc):
        if request.path.startswith("/api/"):
            return (
                jsonify({"ok": False, "error": "conflict", "message": exc.message,
                         "details": exc.details}),
                409,
            )
        return render_template("error.html", title="操作冲突", message=exc.message,
                               details=exc.details), 409

    @app.errorhandler(NotFoundError)
    def on_not_found(exc):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not_found", "message": exc.message}), 404
        return render_template("error.html", title="未找到", message=exc.message), 404

    @app.errorhandler(413)
    def on_too_large(_exc):
        message = "上传内容超过大小上限"
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "validation_error", "message": message}), 413
        return render_template("error.html", title="上传过大", message=message), 413

    @app.errorhandler(404)
    def on_http_not_found(_exc):
        message = "页面或资源不存在"
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not_found", "message": message}), 404
        return render_template("error.html", title="未找到", message=message), 404

    @app.errorhandler(405)
    def on_method_not_allowed(_exc):
        message = "不支持该请求方法"
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "method_not_allowed", "message": message}), 405
        return render_template("error.html", title="方法不允许", message=message), 405

    @app.errorhandler(500)
    def on_server_error(_exc):
        message = "服务器内部错误，操作已回滚，未产生任何部分写入"
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "internal_error", "message": message}), 500
        return render_template("error.html", title="服务器错误", message=message), 500

    @app.get("/healthz")
    def healthz():
        db = get_db()
        db.execute("SELECT 1").fetchone()
        return jsonify({"ok": True})

    # ---------- pages ----------

    @app.get("/")
    def index():
        db = get_db()
        months = facts.list_months(db)
        versions = knowledge.list_versions(db)
        return render_template(
            "index.html",
            months=months,
            versions=versions,
            latest=versions[0] if versions else None,
        )

    @app.get("/upload")
    def upload_page():
        return render_template("upload.html")

    @app.post("/upload")
    def upload_submit():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            raise ValidationError("请选择要上传的 CSV 文件")
        raw = upload.read()
        result = facts.ingest_month(
            get_db(),
            raw,
            filename=upload.filename,
            expected_month=request.form.get("month") or None,
        )
        flash(
            f"{result['month']} 已固定：{result['row_count']} 行，共 {result['total_boxes']} 箱。"
            "该月事实此后不可修改。",
            "success",
        )
        return redirect(url_for("report_page", month=result["month"]))

    @app.get("/knowledge")
    def knowledge_page():
        db = get_db()
        versions = knowledge.list_versions(db)
        selected = knowledge.resolve_version(db, request.args.get("version") or None)
        latest = knowledge.current_version(db)
        return render_template(
            "knowledge.html",
            versions=versions,
            selected=selected,
            latest=latest,
            base_version=latest["version_no"] if latest else "",
            groups=knowledge.version_ranges_for_display(db, selected["version_no"])
            if selected
            else [],
            form=None,
            error=None,
            error_details=[],
        )

    @app.post("/knowledge")
    def knowledge_submit():
        db = get_db()
        try:
            intervals = _intervals_from_form(request.form)
            result = knowledge.register_ranges(
                db,
                sku=request.form.get("sku"),
                intervals=intervals,
                reason=request.form.get("reason"),
                base_version=request.form.get("base_version") or None,
            )
        except (ValidationError, ConflictError, NotFoundError) as exc:
            versions = knowledge.list_versions(db)
            latest = knowledge.current_version(db)
            return (
                render_template(
                    "knowledge.html",
                    versions=versions,
                    selected=latest,
                    latest=latest,
                    base_version=latest["version_no"] if latest else "",
                    groups=knowledge.version_ranges_for_display(db, latest["version_no"])
                    if latest
                    else [],
                    form={
                        "sku": request.form.get("sku", ""),
                        "reason": request.form.get("reason", ""),
                        "intervals": _raw_intervals_from_form(request.form),
                    },
                    error=exc.message,
                    error_details=getattr(exc, "details", []),
                ),
                400 if isinstance(exc, ValidationError) else 409,
            )
        flash(
            f"已生成知识版号 v{result['version_no']}（基准 v{result['based_on_version']}），"
            f"SKU {result['sku']} 共 {result['range_count']} 段区间。旧版号口径保持不变。",
            "success",
        )
        return redirect(url_for("knowledge_page", version=result["version_no"]))

    @app.get("/report")
    def report_page():
        db = get_db()
        months = facts.list_months(db)
        versions = knowledge.list_versions(db)
        month = request.args.get("month") or (months[0]["month"] if months else None)
        version_param = request.args.get("version") or None
        report = None
        if month:
            report = knowledge.build_report(db, month, version_param)
        return render_template(
            "report.html",
            months=months,
            versions=versions,
            report=report,
            selected_month=month,
            selected_version=version_param or "",
        )

    # ---------- API ----------

    @app.get("/api/months")
    def api_months():
        db = get_db()
        return jsonify(
            {
                "ok": True,
                "months": [dict(row) for row in facts.list_months(db)],
            }
        )

    @app.post("/api/months")
    def api_upload_month():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            raise ValidationError("请以 multipart/form-data 上传字段 file")
        result = facts.ingest_month(
            get_db(),
            upload.read(),
            filename=upload.filename,
            expected_month=request.form.get("month") or None,
        )
        result["ok"] = True
        return jsonify(result), 201

    @app.get("/api/versions")
    def api_versions():
        db = get_db()
        return jsonify(
            {"ok": True, "versions": [dict(row) for row in knowledge.list_versions(db)]}
        )

    @app.get("/api/versions/<int:version_no>")
    def api_version(version_no):
        db = get_db()
        row = knowledge.get_version(db, version_no)
        if row is None:
            raise NotFoundError(f"知识版号 v{version_no} 不存在")
        return jsonify(
            {
                "ok": True,
                "version": dict(row),
                "skus": knowledge.version_ranges_for_display(db, version_no),
            }
        )

    @app.post("/api/versions")
    def api_register_version():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        result = knowledge.register_ranges(
            get_db(),
            sku=payload.get("sku"),
            intervals=payload.get("intervals"),
            reason=payload.get("reason"),
            base_version=payload.get("base_version"),
        )
        result["ok"] = True
        return jsonify(result), 201

    @app.get("/api/report")
    def api_report():
        db = get_db()
        month = request.args.get("month")
        if not month:
            raise ValidationError("缺少 month 参数（YYYY-MM）")
        report = knowledge.build_report(db, month, request.args.get("version") or None)
        return jsonify({"ok": True, **knowledge.report_json(report)})

    @app.get("/api/audit")
    def api_audit():
        db = get_db()
        rows = db.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return jsonify({"ok": True, "entries": [dict(row) for row in rows]})

    return app
