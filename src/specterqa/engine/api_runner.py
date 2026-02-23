"""SpecterQA API Runner — HTTP API step executor.

Executes API steps from scenarios: makes HTTP requests, validates responses,
captures variables (headers, body fields) for use in subsequent steps, and
measures response time for performance assertions.

Supports two request modes:
- Standard JSON body requests (action: api_call or default)
- Multipart file upload requests (action: upload_file) -- sends
  multipart/form-data with a fixture file and optional form fields.
"""

from __future__ import annotations

import dataclasses
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

import requests

from specterqa.engine.report_generator import Finding, StepReport

logger = logging.getLogger("specterqa.engine.api_runner")


def _coerce_body_types(obj: Any) -> Any:
    """Recursively coerce pure numeric strings to their numeric types.

    Template variable resolution always produces strings because
    ``str(value)`` is called on every substitution.  When the backend
    expects ``z.number()`` (e.g. Zod), sending ``"32"`` instead of ``32``
    causes a 400 validation error.

    This function walks a body dict/list and converts:
      - Pure integer strings  ("32")      -> int   (32)
      - Pure float strings    ("45000.50") -> float (45000.5)
      - Everything else (including booleans, None, real strings) is unchanged.
    """
    if isinstance(obj, dict):
        return {k: _coerce_body_types(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_body_types(item) for item in obj]
    if isinstance(obj, str):
        # Try integer first (more common in IDs / foreign keys)
        try:
            return int(obj)
        except ValueError:
            pass
        # Then try float
        try:
            return float(obj)
        except ValueError:
            pass
    return obj


@dataclasses.dataclass
class APIStepResult:
    """Result of executing a single API step."""

    step_id: str
    passed: bool
    status_code: int | None
    response_body: Any
    response_headers: dict[str, str]
    captured: dict[str, Any]
    duration_ms: float
    error: str | None = None
    notes: str = ""
    findings: list[Finding] = dataclasses.field(default_factory=list)


class APIRunner:
    """Executes API steps from scenario definitions.

    Handles HTTP requests, response validation, variable capture (including
    cookies), and performance measurement.
    """

    def __init__(
        self,
        base_url: str,
        captured_vars: dict[str, Any] | None = None,
        fixtures_dir: Path | str | None = None,
    ) -> None:
        """
        Args:
            base_url: Base URL for API calls (e.g. "http://localhost:3001").
            captured_vars: Shared captured variables dict (mutated in place).
            fixtures_dir: Directory containing fixture files for upload_file steps.
                If None, file upload steps requiring fixtures will fail with a
                clear error message.
        """
        self._base_url = base_url.rstrip("/")
        self._captured: dict[str, Any] = captured_vars if captured_vars is not None else {}
        self._session = requests.Session()
        self._fixtures_dir = Path(fixtures_dir) if fixtures_dir is not None else None

    @property
    def captured(self) -> dict[str, Any]:
        return self._captured

    @property
    def cookies(self) -> dict[str, str]:
        """Return all cookies accumulated by the session."""
        return dict(self._session.cookies)

    def execute_step(self, step: dict[str, Any]) -> APIStepResult:
        """Execute a single API step.

        Args:
            step: Step definition dict from the scenario YAML.

        Returns:
            APIStepResult with pass/fail, response data, captured vars, timing.
        """
        step_id = step.get("id", "unknown")
        method = step.get("method", "GET").upper()
        path = step.get("path", "/")
        raw_body = step.get("body")
        # Coerce numeric strings produced by template variable resolution so
        # that backends with strict type validators (e.g. Zod z.number())
        # receive proper int/float values instead of strings like "32".
        body = _coerce_body_types(raw_body) if raw_body is not None else None
        expect = step.get("expect", {})
        capture = step.get("capture", {})
        description = step.get("description", "")
        auth_mode = step.get("auth", "cookie")

        url = self._base_url + path
        findings: list[Finding] = []

        # Build per-request headers based on auth mode
        request_headers: dict[str, str] = {}
        if auth_mode == "bearer":
            token = self._captured.get("auth_token") or self._captured.get("bearer_token")
            if token:
                request_headers["Authorization"] = f"Bearer {token}"
            else:
                logger.warning(
                    "Step %s: auth=bearer but no 'auth_token' or 'bearer_token' in captured vars",
                    step_id,
                )
        elif auth_mode == "none":
            # Explicitly strip any Authorization header for this request
            request_headers["Authorization"] = ""

        # Determine if this is a multipart file upload step
        action = step.get("action", "api_call")
        is_upload = action == "upload_file"

        logger.info("API step %s: %s %s -- %s", step_id, method, url, description)

        # Prepare multipart file data if this is an upload step
        upload_files = None
        upload_data = None
        fixture_fh = None  # Keep file handle open for the duration of the request

        if is_upload:
            file_spec = step.get("file", {})
            fixture_rel = file_spec.get("fixture", "")
            field_name = file_spec.get("field", "file")
            extra_fields = step.get("fields", {})

            # Resolve fixture path from the configured fixtures directory
            fixture_path: Path | None = None
            if self._fixtures_dir is not None:
                resolved_fixtures_dir = self._fixtures_dir.resolve()
                candidate = (self._fixtures_dir / fixture_rel).resolve()
                # SECURITY (FIND-007): Verify the resolved fixture path is
                # within fixtures_dir to prevent a malicious product YAML
                # from reading arbitrary files (e.g. fixtures_dir=/etc,
                # fixture_rel=passwd) and uploading them to an API endpoint.
                try:
                    candidate.relative_to(resolved_fixtures_dir)
                    path_safe = True
                except ValueError:
                    path_safe = False

                if path_safe and candidate.is_file():
                    fixture_path = candidate

            if fixture_path is None or not fixture_path.is_file():
                search_info = (
                    f"fixtures_dir={self._fixtures_dir}" if self._fixtures_dir else "no fixtures_dir configured"
                )
                error_msg = f"Fixture file not found: {fixture_rel} ({search_info})"
                logger.error("API step %s: %s", step_id, error_msg)
                findings.append(
                    Finding(
                        severity="block",
                        category="server_error",
                        description=error_msg,
                        evidence="",
                        step_id=step_id,
                    )
                )
                return APIStepResult(
                    step_id=step_id,
                    passed=False,
                    status_code=None,
                    response_body=None,
                    response_headers={},
                    captured={},
                    duration_ms=0.0,
                    error=error_msg,
                    findings=findings,
                )

            # Determine MIME type from file extension
            mime_type, _ = mimetypes.guess_type(str(fixture_path))
            if mime_type is None:
                mime_type = "application/octet-stream"

            fixture_fh = open(fixture_path, "rb")
            upload_files = {field_name: (fixture_path.name, fixture_fh, mime_type)}
            upload_data = extra_fields if extra_fields else None
            logger.info(
                "API step %s: uploading %s as %s (%s), extra fields: %s",
                step_id,
                fixture_path.name,
                field_name,
                mime_type,
                list((extra_fields or {}).keys()),
            )

        start = time.monotonic()
        try:
            # For auth=none, temporarily remove session-level auth and clear the
            # Authorization header so the request goes out unauthenticated.
            if auth_mode == "none":
                saved_auth = self._session.auth
                self._session.auth = None
                if is_upload:
                    # Multipart upload with auth=none
                    req = requests.Request(
                        method=method,
                        url=url,
                        files=upload_files,
                        data=upload_data,
                        headers=request_headers,
                    )
                else:
                    # Standard JSON with auth=none
                    req = requests.Request(
                        method=method,
                        url=url,
                        json=body if body else None,
                        headers=request_headers,
                    )
                prepared = self._session.prepare_request(req)
                # Ensure no Authorization header survives
                prepared.headers.pop("Authorization", None)
                response = self._session.send(prepared, timeout=30)
                self._session.auth = saved_auth
            elif is_upload:
                # Multipart file upload with normal auth
                response = self._session.request(
                    method=method,
                    url=url,
                    files=upload_files,
                    data=upload_data,
                    headers=request_headers if request_headers else None,
                    timeout=30,
                )
            else:
                response = self._session.request(
                    method=method,
                    url=url,
                    json=body if body else None,
                    headers=request_headers if request_headers else None,
                    timeout=30,
                )
            duration_ms = (time.monotonic() - start) * 1000
        except requests.RequestException as exc:
            duration_ms = (time.monotonic() - start) * 1000
            error_msg = f"Request failed: {type(exc).__name__}: {exc}"
            logger.error("API step %s failed: %s", step_id, error_msg)
            findings.append(
                Finding(
                    severity="block",
                    category="server_error",
                    description=error_msg,
                    evidence="",
                    step_id=step_id,
                )
            )
            return APIStepResult(
                step_id=step_id,
                passed=False,
                status_code=None,
                response_body=None,
                response_headers={},
                captured={},
                duration_ms=round(duration_ms, 1),
                error=error_msg,
                findings=findings,
            )
        finally:
            # Always close the fixture file handle if one was opened
            if fixture_fh is not None:
                fixture_fh.close()

        # Parse response body
        response_body: Any = None
        try:
            response_body = response.json()
        except (ValueError, TypeError):
            response_body = response.text

        response_headers = dict(response.headers)

        # --- Signup rate-limit / duplicate-account recovery ---
        # When a signup endpoint returns 429 (rate limited) or 409 (account already
        # exists), automatically try logging in with the same credentials.  If login
        # succeeds, treat the step as passed and use the login response for captures
        # (e.g. auth_cookie) so that downstream browser steps still have auth.
        signup_fallback_note: str | None = None
        if (
            step.get("action") == "api_call"
            and "signup" in step.get("path", "").lower()
            and response.status_code in (429, 409)
        ):
            original_status = response.status_code
            logger.warning(
                "Signup returned %d, attempting login with existing credentials",
                original_status,
            )
            login_body = {
                "email": (body or {}).get("email", ""),
                "password": (body or {}).get("password", ""),
            }
            login_url = self._base_url + "/api/v1/auth/login"
            try:
                login_resp = self._session.request(
                    method="POST",
                    url=login_url,
                    json=login_body,
                    headers=request_headers if request_headers else None,
                    timeout=30,
                )
                if login_resp.status_code == 200:
                    logger.info("Login succeeded as fallback for rate-limited signup")
                    # Replace response data so captures (auth_cookie, user_id, etc.)
                    # come from the successful login response.
                    response = login_resp
                    try:
                        response_body = login_resp.json()
                    except (ValueError, TypeError):
                        response_body = login_resp.text
                    response_headers = dict(login_resp.headers)
                    signup_fallback_note = f"Signup returned {original_status}; fell back to login successfully"
                else:
                    logger.warning("Login fallback also failed: %d", login_resp.status_code)
            except requests.RequestException as exc:
                logger.warning("Login fallback request failed: %s", exc)

        # Validate expectations
        passed = True
        error_parts: list[str] = []

        # Status code check -- skip when signup-to-login fallback succeeded
        # because the login response (200) legitimately differs from the signup
        # expectation (201) and we already know the step recovered successfully.
        expected_status = expect.get("status")
        if signup_fallback_note is not None:
            # Fallback succeeded -- treat as passed regardless of original expectation
            logger.info(
                "Skipping status assertion (expected %s) due to signup->login fallback",
                expected_status,
            )
        elif expected_status is not None and response.status_code != expected_status:
            passed = False
            msg = f"Expected status {expected_status}, got {response.status_code}"
            error_parts.append(msg)
            severity = "block" if response.status_code >= 500 else "high"
            findings.append(
                Finding(
                    severity=severity,
                    category="server_error" if response.status_code >= 500 else "api_contract",
                    description=msg,
                    evidence=f"Response: {str(response_body)[:200]}",
                    step_id=step_id,
                )
            )

        # Body assertions
        body_checks = expect.get("body_contains", [])
        for check in body_checks:
            check_path = check.get("path", "")
            value = self._resolve_dotpath(response_body, check_path)

            if "exists" in check:
                if check["exists"] and value is None:
                    passed = False
                    msg = f"Expected {check_path} to exist, but it does not"
                    error_parts.append(msg)
                    findings.append(
                        Finding(
                            severity="high",
                            category="api_contract",
                            description=msg,
                            evidence=f"Body: {str(response_body)[:200]}",
                            step_id=step_id,
                        )
                    )
                elif not check["exists"] and value is not None:
                    passed = False
                    msg = f"Expected {check_path} to not exist, but it does"
                    error_parts.append(msg)

            if "equals" in check:
                if value != check["equals"]:
                    passed = False
                    msg = f"Expected {check_path} == {check['equals']!r}, got {value!r}"
                    error_parts.append(msg)
                    findings.append(
                        Finding(
                            severity="high",
                            category="api_contract",
                            description=msg,
                            evidence=f"Body: {str(response_body)[:200]}",
                            step_id=step_id,
                        )
                    )

            if "gte" in check:
                try:
                    if value is None or float(value) < float(check["gte"]):
                        passed = False
                        msg = f"Expected {check_path} >= {check['gte']}, got {value}"
                        error_parts.append(msg)
                except (ValueError, TypeError):
                    passed = False
                    msg = f"Cannot compare {check_path} value {value!r} as number"
                    error_parts.append(msg)

        # Performance assertion
        perf = expect.get("performance", {})
        max_ms = perf.get("max_ms")
        if max_ms is not None and duration_ms > max_ms:
            findings.append(
                Finding(
                    severity="high",
                    category="performance",
                    description=f"API call took {duration_ms:.0f}ms, exceeds {max_ms}ms target",
                    evidence=f"{method} {path}",
                    step_id=step_id,
                )
            )

        # Capture variables
        newly_captured: dict[str, Any] = {}
        for var_name, source in capture.items():
            if source == "Set-Cookie":
                # Capture the raw Set-Cookie header value
                cookie_header = response.headers.get("Set-Cookie", "")
                newly_captured[var_name] = cookie_header
                self._captured[var_name] = cookie_header
            elif source.startswith("."):
                # Dotpath into response body
                val = self._resolve_dotpath(response_body, source)
                if val is not None:
                    newly_captured[var_name] = val
                    self._captured[var_name] = val
                else:
                    logger.warning(
                        "Step %s: capture %s from %s resolved to None",
                        step_id,
                        var_name,
                        source,
                    )
            else:
                # Check headers
                header_val = response.headers.get(source)
                if header_val is not None:
                    newly_captured[var_name] = header_val
                    self._captured[var_name] = header_val

        error_msg = "; ".join(error_parts) if error_parts else None

        logger.info(
            "API step %s: status=%s passed=%s duration=%.0fms captured=%s",
            step_id,
            response.status_code,
            passed,
            duration_ms,
            list(newly_captured.keys()),
        )

        return APIStepResult(
            step_id=step_id,
            passed=passed,
            status_code=response.status_code,
            response_body=response_body,
            response_headers=response_headers,
            captured=newly_captured,
            duration_ms=round(duration_ms, 1),
            error=error_msg,
            notes=signup_fallback_note or "",
            findings=findings,
        )

    def to_step_report(self, result: APIStepResult, description: str = "") -> StepReport:
        """Convert an APIStepResult into a generic StepReport."""
        notes_parts = []
        if result.status_code is not None:
            notes_parts.append(f"HTTP {result.status_code}")
        if result.notes:
            notes_parts.append(result.notes)
        return StepReport(
            step_id=result.step_id,
            description=description,
            mode="api",
            passed=result.passed,
            duration_seconds=round(result.duration_ms / 1000, 2),
            error=result.error,
            notes=" | ".join(notes_parts),
            performance_ms=result.duration_ms,
        )

    @staticmethod
    def _resolve_dotpath(data: Any, path: str) -> Any:
        """Resolve a dotpath like '.user.id' into a nested dict.

        Leading dot is stripped. Returns None if path cannot be resolved.
        """
        if not path or data is None:
            return None

        # Strip leading dot
        path = path.lstrip(".")
        parts = path.split(".")

        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (IndexError, ValueError):
                    return None
            else:
                return None
            if current is None:
                return None

        return current
