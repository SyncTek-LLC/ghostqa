"""GhostQA Dashboard Server — Local HTTP server for browsing test run evidence.

Serves a Jinja2-rendered web dashboard on localhost that lets users browse
GhostQA run results, step-by-step details, screenshots, and findings.

Usage:
    from ghostqa.viewer.server import DashboardServer
    server = DashboardServer(evidence_dir=Path(".ghostqa/evidence"), port=8199)
    server.start()   # starts in background thread
    server.stop()
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger("ghostqa.viewer")

# Template directory is relative to this file
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# Run directory prefix
_RUN_DIR_PREFIX = "GQA-RUN-"


def _load_run_result(run_dir: Path) -> dict[str, Any] | None:
    """Load and return run-result.json from a run directory, or None on failure."""
    result_path = run_dir / "run-result.json"
    if not result_path.exists():
        return None
    try:
        with open(result_path) as f:
            data = json.load(f)
        # Attach the directory path for screenshot serving
        data["_evidence_dir"] = str(run_dir)
        data["_run_id"] = run_dir.name
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load %s: %s", result_path, exc)
        return None


def _collect_runs(evidence_dir: Path) -> list[dict[str, Any]]:
    """Scan evidence_dir for GQA-RUN-* directories and load results.

    Returns a list of run result dicts sorted by start_time descending.
    """
    if not evidence_dir.is_dir():
        return []

    runs: list[dict[str, Any]] = []
    for entry in evidence_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(_RUN_DIR_PREFIX):
            result = _load_run_result(entry)
            if result is not None:
                runs.append(result)

    # Sort newest first by start_time (ISO string), falling back to run_id
    runs.sort(key=lambda r: r.get("start_time", r.get("_run_id", "")), reverse=True)
    return runs


def _parse_run_id_from_date(run_id: str) -> str:
    """Extract a human-readable date from a run ID like GQA-RUN-20260219-143022-ab1c."""
    match = re.match(r"GQA-RUN-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", run_id)
    if match:
        y, mo, d, h, mi, s = match.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s} UTC"
    return run_id


class _DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the GhostQA dashboard."""

    # These are set by the server factory
    evidence_dir: Path
    jinja_env: Environment

    def log_message(self, format: str, *args: Any) -> None:
        """Route HTTP logs through the Python logger."""
        logger.debug(format, *args)

    def do_GET(self) -> None:
        """Route GET requests to the appropriate handler."""
        path = unquote(self.path)

        # Route: /static/<filename>
        if path.startswith("/static/"):
            self._serve_static(path[8:])  # strip "/static/"
            return

        # Route: /runs/<run_id>/screenshots/<filename>
        match = re.match(r"^/runs/([^/]+)/screenshots/(.+)$", path)
        if match:
            run_id, filename = match.groups()
            self._serve_screenshot(run_id, filename)
            return

        # Route: /runs/<run_id>/download/report.md
        match = re.match(r"^/runs/([^/]+)/download/report\.md$", path)
        if match:
            run_id = match.group(1)
            self._serve_report_download(run_id)
            return

        # Route: /runs/<run_id>
        match = re.match(r"^/runs/([^/]+)/?$", path)
        if match:
            run_id = match.group(1)
            self._serve_detail(run_id)
            return

        # Route: / (index)
        if path == "/" or path == "":
            self._serve_index()
            return

        # 404
        self._send_error(HTTPStatus.NOT_FOUND, "Page not found")

    def _serve_index(self) -> None:
        """Render the run index page."""
        runs = _collect_runs(self.evidence_dir)

        # Compute summary stats
        total_runs = len(runs)
        pass_count = sum(1 for r in runs if r.get("passed", False))
        fail_count = total_runs - pass_count
        total_cost = sum(r.get("cost_usd", 0.0) for r in runs)

        # Add formatted date to each run
        for run in runs:
            run["_date_display"] = _parse_run_id_from_date(run.get("_run_id", ""))

        template = self.jinja_env.get_template("index.html")
        html = template.render(
            runs=runs,
            total_runs=total_runs,
            pass_count=pass_count,
            fail_count=fail_count,
            total_cost=total_cost,
        )
        self._send_html(html)

    def _serve_detail(self, run_id: str) -> None:
        """Render the run detail page."""
        run_dir = self.evidence_dir / run_id
        if not run_dir.is_dir():
            self._send_error(HTTPStatus.NOT_FOUND, f"Run not found: {run_id}")
            return

        result = _load_run_result(run_dir)
        if result is None:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                f"No run-result.json found for: {run_id}",
            )
            return

        result["_date_display"] = _parse_run_id_from_date(run_id)

        # Collect available screenshot files
        screenshots_available: set[str] = set()
        for f in run_dir.iterdir():
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                screenshots_available.add(f.name)
        result["_screenshots_available"] = screenshots_available

        # Check for report.md
        result["_has_report"] = (run_dir / "report.md").exists()

        # Categorize findings by severity
        findings = result.get("findings", [])
        findings_by_severity: dict[str, list] = {
            "block": [],
            "critical": [],
            "high": [],
            "medium": [],
            "low": [],
        }
        for f in findings:
            severity = f.get("severity", "low").lower()
            if severity not in findings_by_severity:
                findings_by_severity[severity] = []
            findings_by_severity[severity].append(f)
        result["_findings_by_severity"] = {
            k: v for k, v in findings_by_severity.items() if v
        }

        template = self.jinja_env.get_template("detail.html")
        html = template.render(run=result, run_id=run_id)
        self._send_html(html)

    def _serve_screenshot(self, run_id: str, filename: str) -> None:
        """Serve a screenshot file from a run's evidence directory."""
        # Sanitize filename to prevent path traversal
        safe_name = Path(filename).name
        file_path = self.evidence_dir / run_id / safe_name

        if not file_path.exists() or not file_path.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, f"Screenshot not found: {filename}")
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _serve_report_download(self, run_id: str) -> None:
        """Serve report.md as a download."""
        file_path = self.evidence_dir / run_id / "report.md"
        if not file_path.exists():
            self._send_error(HTTPStatus.NOT_FOUND, "report.md not found")
            return

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{run_id}-report.md"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, filename: str) -> None:
        """Serve a static file (CSS, etc.)."""
        safe_name = Path(filename).name
        file_path = _STATIC_DIR / safe_name

        if not file_path.exists() or not file_path.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, f"Static file not found: {filename}")
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        """Send an HTML response."""
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """Send an error page."""
        template = self.jinja_env.get_template("base.html")
        html = template.render(
            error_message=message,
            error_status=status.value,
        )
        # We override the content block inline since base.html expects it
        error_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>GhostQA — {status.value} {status.phrase}</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <header class="site-header">
        <div class="header-content">
            <a href="/" class="logo">
                <span class="logo-icon">&#128123;</span> GhostQA
            </a>
        </div>
    </header>
    <main class="container">
        <div class="error-page">
            <h1>{status.value} {status.phrase}</h1>
            <p>{message}</p>
            <a href="/" class="btn">Back to Dashboard</a>
        </div>
    </main>
    <footer class="site-footer">
        <div class="container">GhostQA v0.1.0 &mdash; AI Behavioral Testing</div>
    </footer>
</body>
</html>"""
        encoded = error_html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class DashboardServer:
    """Manages a local HTTP server for the GhostQA dashboard.

    Runs in a background thread so it can be started/stopped cleanly.

    Usage:
        server = DashboardServer(evidence_dir=Path(".ghostqa/evidence"), port=8199)
        server.start()
        # ... browser opens ...
        server.stop()
    """

    def __init__(self, evidence_dir: Path, port: int = 8199) -> None:
        self.evidence_dir = evidence_dir.resolve()
        self.port = port
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

        # Build Jinja2 environment
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        # Register custom filters
        self._jinja_env.filters["format_cost"] = _filter_format_cost
        self._jinja_env.filters["format_duration"] = _filter_format_duration
        self._jinja_env.filters["status_badge"] = _filter_status_badge
        self._jinja_env.filters["severity_class"] = _filter_severity_class

    def start(self) -> None:
        """Start the dashboard server in a background thread."""
        if self._httpd is not None:
            logger.warning("Server is already running on port %d", self.port)
            return

        # Create a handler class with our config bound
        evidence_dir = self.evidence_dir
        jinja_env = self._jinja_env

        class Handler(_DashboardHandler):
            pass

        Handler.evidence_dir = evidence_dir  # type: ignore[attr-defined]
        Handler.jinja_env = jinja_env  # type: ignore[attr-defined]

        self._httpd = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="ghostqa-dashboard",
            daemon=True,
        )
        self._thread.start()
        logger.info("Dashboard server started at http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        """Stop the dashboard server."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Dashboard server stopped.")

    @property
    def url(self) -> str:
        """The base URL of the running server."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_running(self) -> bool:
        """Whether the server is currently running."""
        return self._httpd is not None


# ── Jinja2 Custom Filters ───────────────────────────────────────────────────


def _filter_format_cost(value: Any) -> str:
    """Format a USD cost value."""
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return "$0.0000"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def _filter_format_duration(value: Any) -> str:
    """Format a duration in seconds to a human-readable string."""
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return "0s"
    if secs < 60:
        return f"{secs:.1f}s"
    minutes = int(secs // 60)
    remaining = secs % 60
    return f"{minutes}m {remaining:.0f}s"


def _filter_status_badge(passed: Any) -> str:
    """Return an HTML badge for pass/fail status."""
    if passed:
        return '<span class="badge badge-pass">PASS</span>'
    return '<span class="badge badge-fail">FAIL</span>'


def _filter_severity_class(severity: str) -> str:
    """Map severity to a CSS class name."""
    mapping = {
        "block": "severity-block",
        "critical": "severity-critical",
        "high": "severity-high",
        "medium": "severity-medium",
        "low": "severity-low",
    }
    return mapping.get(severity.lower(), "severity-low")
