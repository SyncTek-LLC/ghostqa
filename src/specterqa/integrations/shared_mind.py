"""SpecterQA → BusinessAtlas Shared Mind integration.

Emits a finding signal to the BusinessAtlas neural mesh after each test run,
enabling bidirectional learning between SpecterQA and the broader OS.

This module is fully opt-in and fail-safe:
- If BUSINESSATLAS_ROOT is not set → no-op.
- If signal_emitter.py is missing → no-op.
- If subprocess call fails for any reason → no-op.
- Federation failure NEVER aborts a test run.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("specterqa.integrations.shared_mind")


def emit_run_signal(
    run_id: str,
    passed: bool,
    findings_count: int,
    cost_usd: float,
    initiative_id: str | None = None,
) -> None:
    """Emit a finding signal to BusinessAtlas Shared Mind after a test run.

    All failures are silently swallowed — this integration must never abort
    or interrupt a SpecterQA test run.

    Args:
        run_id: The SpecterQA run identifier (e.g. "GQA-RUN-20260301-143052-a1b2").
        passed: Whether all steps in the run passed.
        findings_count: Total number of findings recorded during the run.
        cost_usd: Total AI cost incurred for this run in USD.
        initiative_id: Optional BusinessAtlas initiative ID to associate the signal with.
    """
    try:
        _emit(
            run_id=run_id,
            passed=passed,
            findings_count=findings_count,
            cost_usd=cost_usd,
            initiative_id=initiative_id,
        )
    except Exception as exc:
        # Broad catch: federation failure must never surface to callers.
        logger.debug("Shared Mind signal suppressed (non-fatal): %s", exc)


def _emit(
    run_id: str,
    passed: bool,
    findings_count: int,
    cost_usd: float,
    initiative_id: str | None,
) -> None:
    """Internal implementation — may raise; caller wraps in try/except."""
    ba_root_str = os.environ.get("BUSINESSATLAS_ROOT", "").strip()
    if not ba_root_str:
        logger.debug("BUSINESSATLAS_ROOT not set — skipping Shared Mind signal")
        return

    ba_root = Path(ba_root_str)
    emitter = ba_root / "scripts" / "signal_emitter.py"
    if not emitter.is_file():
        logger.debug("signal_emitter.py not found at %s — skipping Shared Mind signal", emitter)
        return

    payload: dict = {
        "type": "finding",
        "source": "SpecterQA",
        "initiative_id": initiative_id,
        "priority": "medium",
        "payload": {
            "run_id": run_id,
            "passed": passed,
            "findings_count": findings_count,
            "cost_usd": cost_usd,
        },
    }

    result = subprocess.run(
        ["python3", str(emitter), json.dumps(payload)],
        capture_output=True,
        timeout=5,
        text=True,
    )

    if result.returncode != 0:
        logger.debug(
            "signal_emitter.py exited %d: %s",
            result.returncode,
            result.stderr.strip(),
        )
    else:
        logger.info(
            "Shared Mind signal emitted: run_id=%s passed=%s findings=%d cost=$%.4f",
            run_id,
            passed,
            findings_count,
            cost_usd,
        )
