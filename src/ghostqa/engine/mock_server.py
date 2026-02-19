"""GhostQA Mock Server — Lightweight HTTP mock server driven by contract YAML.

Reads mock contracts from YAML files, starts a background HTTP server on the
configured port, and matches incoming requests against scenario rules (path,
method, body_contains).  Returns canned responses with configurable status
codes, JSON bodies, and simulated latency.

Optionally records all requests in JSONL format for debugging and scenario
refinement.

Usage::

    server = MockServer(
        contract_path=Path("mocks/my-api/contract.yaml"),
        base_dir=Path("/path/to/project"),
    )
    server.start()
    print(server.url)   # http://localhost:9090
    # ... run tests against server.url ...
    server.stop()

Or as a context manager::

    with MockServer(contract_path, base_dir) as server:
        # ... run tests ...
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

logger = logging.getLogger("ghostqa.engine.mock_server")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

class MockEndpoint:
    """Parsed endpoint from a contract YAML."""

    __slots__ = ("path", "method", "scenarios", "default_response")

    def __init__(
        self,
        path: str,
        method: str,
        scenarios: list[dict[str, Any]],
        default_response: dict[str, Any] | None,
    ) -> None:
        self.path = path
        self.method = method.upper()
        self.scenarios = scenarios
        self.default_response = default_response


class MockContract:
    """Fully parsed mock contract ready for request matching."""

    def __init__(self, raw: dict[str, Any]) -> None:
        svc = raw.get("mock_service", raw)

        self.service_id: str = svc.get("id", "MOCK-UNKNOWN")
        self.name: str = svc.get("name", "Unknown Mock")
        self.port: int = int(svc.get("mock_port", 9090))
        self.path_prefix: str = svc.get("mock_path_prefix", "")

        # Parse endpoints
        self.endpoints: list[MockEndpoint] = []
        for ep_raw in svc.get("endpoints", []):
            self.endpoints.append(MockEndpoint(
                path=ep_raw.get("path", "/"),
                method=ep_raw.get("method", "POST").upper(),
                scenarios=ep_raw.get("scenarios", []),
                default_response=ep_raw.get("default_response"),
            ))

        # Health check
        hc = svc.get("health_check", {})
        self.health_path: str = hc.get("path", "/health")
        self.health_response: dict[str, Any] = hc.get("response", {}).get(
            "body", {"status": "ok", "mock": True}
        )

        # Recording config
        rec = svc.get("recording", {})
        self.recording_enabled: bool = rec.get("enabled", False)
        self.recording_dir: str = rec.get("output_dir", "")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class MockRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler that matches requests against a MockContract."""

    # Set by MockServer before the HTTPServer is started
    contract: MockContract
    base_dir: Path
    _recording_lock: threading.Lock

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_PATCH(self) -> None:
        self._handle_request("PATCH")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    # Suppress default stderr logging -- we route through Python logging
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("mock_http: %s", format % args)

    # -----------------------------------------------------------------------
    # Core dispatch
    # -----------------------------------------------------------------------

    def _handle_request(self, method: str) -> None:
        contract = self.__class__.contract
        path = self.path.split("?")[0]  # strip query string

        # Read request body (if any)
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        body_str = body_bytes.decode("utf-8", errors="replace")

        # 1) Health check -- always responds regardless of contract endpoints
        if path == contract.health_path:
            self._send_json(200, contract.health_response)
            self._record(method, path, body_str, "_health_check", 200)
            return

        # 2) Match against contract endpoints
        matched_scenario_name: str | None = None
        response_spec: dict[str, Any] | None = None

        for ep in contract.endpoints:
            if ep.path == path and ep.method == method:
                # Try scenario matching
                for scenario in ep.scenarios:
                    match_rule = scenario.get("match", {})
                    if self._matches(match_rule, body_str):
                        matched_scenario_name = scenario.get("name", "unnamed")
                        response_spec = scenario.get("response", {})
                        break

                # Fall back to default_response for this endpoint
                if response_spec is None and ep.default_response is not None:
                    matched_scenario_name = "_default"
                    response_spec = ep.default_response
                break

        # 3) If nothing matched at all, return 404
        if response_spec is None:
            self._send_json(404, {
                "error": "no_matching_mock",
                "message": f"No mock scenario matched {method} {path}",
                "mock": True,
            })
            self._record(method, path, body_str, "_no_match", 404)
            return

        # 4) Simulate latency
        latency_ms = response_spec.get("latency_ms", 0)
        if latency_ms > 0:
            time.sleep(latency_ms / 1000.0)

        # 5) Send response
        status = response_spec.get("status", 200)
        body = response_spec.get("body", {})
        self._send_json(status, body)

        logger.info(
            "Mock matched: %s %s -> scenario=%s status=%d latency=%dms",
            method, path, matched_scenario_name, status, latency_ms,
        )

        # 6) Record
        self._record(method, path, body_str, matched_scenario_name, status)

    # -----------------------------------------------------------------------
    # Matching
    # -----------------------------------------------------------------------

    @staticmethod
    def _matches(match_rule: dict[str, Any], body_str: str) -> bool:
        """Check whether a request body satisfies a scenario match rule.

        Currently supports:
        - ``body_contains``: substring match (case-insensitive) in the raw body.
        """
        if not match_rule:
            return False

        body_contains = match_rule.get("body_contains")
        if body_contains is not None:
            return body_contains.lower() in body_str.lower()

        # No recognized match keys -- treat as non-match
        return False

    # -----------------------------------------------------------------------
    # Response helpers
    # -----------------------------------------------------------------------

    def _send_json(self, status: int, body: Any) -> None:
        """Send a JSON response."""
        payload = json.dumps(body, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Mock-Server", "ghostqa-mock/1.0")
        self.end_headers()
        self.wfile.write(payload)

    # -----------------------------------------------------------------------
    # Recording
    # -----------------------------------------------------------------------

    def _record(
        self,
        method: str,
        path: str,
        body_str: str,
        scenario_name: str | None,
        status: int,
    ) -> None:
        """Append a JSONL line to the recordings directory (if enabled)."""
        contract = self.__class__.contract
        if not contract.recording_enabled or not contract.recording_dir:
            return

        base_dir = self.__class__.base_dir
        rec_dir = base_dir / contract.recording_dir
        rec_dir.mkdir(parents=True, exist_ok=True)

        record = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "method": method,
            "path": path,
            "body_snippet": body_str[:500] if body_str else "",
            "matched_scenario": scenario_name,
            "response_status": status,
        }

        rec_file = rec_dir / "requests.jsonl"
        line = json.dumps(record, separators=(",", ":")) + "\n"

        with self.__class__._recording_lock:
            with open(rec_file, "a", encoding="utf-8") as f:
                f.write(line)


# ---------------------------------------------------------------------------
# MockServer -- public API
# ---------------------------------------------------------------------------

class MockServer:
    """Manages lifecycle of a mock HTTP server backed by a contract YAML.

    Usage::

        server = MockServer(
            contract_path=Path("mocks/my-api/contract.yaml"),
            base_dir=Path("/path/to/project"),
        )
        server.start()
        print(server.url)   # http://localhost:9090
        # ... run tests ...
        server.stop()
    """

    def __init__(self, contract_path: Path, base_dir: Path) -> None:
        """
        Args:
            contract_path: Path to the contract YAML (absolute or relative to base_dir).
            base_dir: Project root -- used for resolving recording output dirs.
        """
        if yaml is None:
            raise ImportError(
                "PyYAML is required for MockServer. Install with: pip install pyyaml"
            )

        self._base_dir = Path(base_dir)
        abs_contract = (
            contract_path if contract_path.is_absolute()
            else self._base_dir / contract_path
        )

        if not abs_contract.exists():
            raise FileNotFoundError(f"Mock contract not found: {abs_contract}")

        with open(abs_contract, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        self._contract = MockContract(raw)
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started = False

        logger.info(
            "MockServer loaded: %s (port=%d, endpoints=%d, recording=%s)",
            self._contract.name,
            self._contract.port,
            len(self._contract.endpoints),
            self._contract.recording_enabled,
        )

    @property
    def contract(self) -> MockContract:
        """Return the parsed contract."""
        return self._contract

    @property
    def port(self) -> int:
        """Return the configured port."""
        return self._contract.port

    @property
    def url(self) -> str:
        """Return the base URL of the running mock server."""
        return f"http://localhost:{self._contract.port}"

    @property
    def is_running(self) -> bool:
        """Return True if the mock server is currently running."""
        return self._started

    def start(self) -> None:
        """Start the mock HTTP server in a background daemon thread.

        Raises:
            RuntimeError: If the server is already running.
            OSError: If the port is already in use.
        """
        if self._started:
            raise RuntimeError(
                f"MockServer already running on port {self._contract.port}"
            )

        # Build a handler class with the contract and base_dir bound
        handler_class = type(
            "BoundMockHandler",
            (MockRequestHandler,),
            {
                "contract": self._contract,
                "base_dir": self._base_dir,
                "_recording_lock": threading.Lock(),
            },
        )

        self._httpd = HTTPServer(("127.0.0.1", self._contract.port), handler_class)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"mock-{self._contract.service_id}",
            daemon=True,
        )
        self._thread.start()
        self._started = True

        logger.info(
            "MockServer started: %s at %s",
            self._contract.name,
            self.url,
        )

    def stop(self) -> None:
        """Gracefully shut down the mock server and join the thread.

        Safe to call multiple times -- subsequent calls are no-ops.
        """
        if not self._started:
            return

        if self._httpd is not None:
            self._httpd.shutdown()  # signals serve_forever() to stop
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning(
                    "MockServer thread did not exit cleanly within 5s: %s",
                    self._contract.service_id,
                )

        self._httpd = None
        self._thread = None
        self._started = False

        logger.info("MockServer stopped: %s", self._contract.name)

    def __enter__(self) -> MockServer:
        """Context manager support: start on enter."""
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        """Context manager support: stop on exit."""
        self.stop()


# ---------------------------------------------------------------------------
# Multi-server helper
# ---------------------------------------------------------------------------

def start_mock_servers(
    registry_path: Path,
    base_dir: Path,
    services: list[str] | None = None,
) -> list[MockServer]:
    """Start mock servers for all (or selected) services from a registry.

    Args:
        registry_path: Path to a ``_registry.yaml`` file.
        base_dir: Project root directory.
        services: Optional list of service names to start.  If None, all
            services with ``status: active`` are started.

    Returns:
        List of running MockServer instances (caller must stop them).
    """
    if yaml is None:
        raise ImportError("PyYAML is required for start_mock_servers")

    abs_registry = (
        registry_path if registry_path.is_absolute()
        else base_dir / registry_path
    )

    with open(abs_registry, "r", encoding="utf-8") as f:
        registry = yaml.safe_load(f)

    servers: list[MockServer] = []
    for svc_name, svc_info in (registry.get("services") or {}).items():
        if services is not None and svc_name not in services:
            continue
        if svc_info.get("status") != "active":
            logger.debug("Skipping inactive mock service: %s", svc_name)
            continue

        contract_rel = svc_info.get("contract", "")
        if not contract_rel:
            logger.warning("Mock service %s has no contract path, skipping", svc_name)
            continue

        try:
            server = MockServer(
                contract_path=Path(contract_rel),
                base_dir=base_dir,
            )
            server.start()
            servers.append(server)
        except Exception:
            logger.exception("Failed to start mock server for %s", svc_name)
            # Stop any servers that were already started
            for s in servers:
                s.stop()
            raise

    return servers
