"""Bounded subprocess runner for the read-only live preflight."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from dashboard.models import PreflightCheckView, PreflightView


class PreflightRunner:
    def __init__(
        self,
        config_dir: str | Path,
        *,
        timeout_seconds: float = 30,
        redactions: list[str] | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._timeout = timeout_seconds
        self._redactions = [value for value in (redactions or []) if value]

    def set_redactions(self, redactions: list[str]) -> None:
        """Refresh in-memory scrub values after credentials or account changes."""

        self._redactions = [value for value in redactions if value]

    def _redact(self, text: str) -> str:
        redacted = text
        for value in self._redactions:
            redacted = redacted.replace(value, "[REDACTED]")
        return redacted[:500]

    async def run(self) -> PreflightView:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scripts.live_preflight",
            "--config-dir",
            str(self._config_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return PreflightView(
                ok=False,
                status="failed",
                reason="preflight_timeout",
                checked_at=datetime.now(tz=UTC),
            )

        output = stdout.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            error = self._redact(stderr.decode("utf-8", errors="replace"))
            reason = (
                "credentials_incomplete"
                if "requires a private key" in error
                else "preflight_failed"
            )
            return PreflightView(
                ok=False,
                status="failed",
                reason=reason,
                checked_at=datetime.now(tz=UTC),
            )

        checks = [
            PreflightCheckView(
                name=str(item.get("name", "unknown")),
                passed=bool(item.get("passed", False)),
                reason=self._redact(str(item.get("reason", "unknown"))),
            )
            for item in payload.get("checks", [])
            if isinstance(item, dict)
        ]
        ok = bool(payload.get("ok", False)) and process.returncode == 0
        return PreflightView(
            ok=ok,
            status="passed" if ok else "failed",
            reason="preflight_passed" if ok else "preflight_checks_failed",
            checked_at=datetime.now(tz=UTC),
            checks=checks,
        )
