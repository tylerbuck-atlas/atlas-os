# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Boot sequence: config validation, ordered stages, 'Atlas Ready.'"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from atlas_core.config import CoreConfig
from atlas_core.main import create_app


class TestConfigValidation:
    def test_refuses_missing_bootstrap_token(self):
        with pytest.raises(ValidationError):
            CoreConfig(bootstrap_token="", _env_file=None)

    def test_refuses_short_bootstrap_token(self):
        with pytest.raises(ValidationError):
            CoreConfig(bootstrap_token="short", _env_file=None)

    def test_refuses_placeholder_bootstrap_token(self):
        with pytest.raises(ValidationError):
            CoreConfig(
                bootstrap_token="change-me-to-a-long-random-secret", _env_file=None
            )

    def test_refuses_bad_log_level(self):
        with pytest.raises(ValidationError):
            CoreConfig(
                bootstrap_token="test-bootstrap-token-1234",
                log_level="LOUD",
                _env_file=None,
            )


class TestBootSequence:
    async def test_boot_reaches_ready_and_logs_atlas_ready(self, config):
        # create_app() owns the logging config (basicConfig force=True), so
        # capture with an explicitly attached handler rather than caplog.
        application = create_app(config)
        assert application.state.ready is False

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        core_logger = logging.getLogger("atlas.core")
        handler = _Capture(level=logging.INFO)
        core_logger.addHandler(handler)
        core_logger.setLevel(logging.INFO)
        try:
            async with application.router.lifespan_context(application):
                assert application.state.ready is True
                assert application.state.boot_stage == "8/8 READY"
                assert application.state.instance_id
        finally:
            core_logger.removeHandler(handler)

        messages = [r.getMessage() for r in records]
        assert "Atlas Ready." in messages

        # Stages must appear in order.
        stage_logs = [m for m in messages if m.startswith("boot stage")]
        expected = ["CONFIG", "IDENTITY", "REGISTRY", "AUTH", "PLUGINS", "HEALTH", "API", "READY"]
        assert [s.split(": ")[1] for s in stage_logs] == expected

        # Clean shutdown flips readiness off.
        assert application.state.ready is False

    async def test_liveness_endpoints_require_no_auth(self, client):
        for path in ("/healthz", "/v1/system/health"):
            response = await client.get(path)
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "ok"
            assert body["service"] == "atlas.core"

    async def test_system_status_requires_auth(self, client):
        assert (await client.get("/v1/system/status")).status_code == 401

    async def test_system_status_reports_ready(self, client):
        from .conftest import bootstrap_headers

        response = await client.get("/v1/system/status", headers=bootstrap_headers())
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["boot_stage"] == "8/8 READY"
        assert body["services_registered"] == 0
