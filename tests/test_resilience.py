# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-FileCopyrightText: 2026 Shaan Narendran <shaannaren06@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for resilience patterns: retries, health checks, and timeouts."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Telemetry client retries on ConnectError and raises on failure
# ---------------------------------------------------------------------------


class TestTelemetryClientRetry:
    """The telemetry client retries transport failures, then raises - never returns []."""

    @pytest.mark.asyncio
    async def test_query_retries_on_connect_error_then_succeeds(self):
        from services.telemetry import client as tclient

        ok = MagicMock(status_code=200)
        ok.json.return_value = {"columns": ["one"], "rows": [{"one": 1}], "row_count": 1}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[httpx.ConnectError("refused"), ok])

        with (
            patch.object(tclient, "get_client", return_value=mock_client),
            patch("tenacity.nap.time.sleep"),
        ):
            assert await tclient.query("SELECT 1 AS one") == [{"one": 1}]
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_query_gives_up_and_raises_unavailable(self):
        from services.telemetry import TelemetryUnavailableError
        from services.telemetry import client as tclient

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with (
            patch.object(tclient, "get_client", return_value=mock_client),
            patch("tenacity.nap.time.sleep"),
            pytest.raises(TelemetryUnavailableError),
        ):
            await tclient.query("SELECT 1")
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_query_does_not_retry_on_query_errors(self):
        from services.telemetry import TelemetryQueryError
        from services.telemetry import client as tclient

        bad = MagicMock(status_code=400, text="nope")
        bad.json.return_value = {"error": {"code": "query_error", "message": "boom"}}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=bad)

        with patch.object(tclient, "get_client", return_value=mock_client), pytest.raises(TelemetryQueryError):
            await tclient.query("SELECT broken")
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "code", "exc_name"),
        [
            (504, "query_timeout", "TelemetryTimeoutError"),
            (413, "result_too_large", "TelemetryResultTooLargeError"),
            (429, "telemetry_busy", "TelemetryBusyError"),
            (503, "writer_paused", "TelemetryUnavailableError"),
        ],
    )
    async def test_status_codes_map_to_typed_errors(self, status, code, exc_name):
        import services.telemetry as telemetry
        from services.telemetry import client as tclient

        resp = MagicMock(status_code=status, text="")
        resp.json.return_value = {"error": {"code": code, "message": "x"}}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)

        with patch.object(tclient, "get_client", return_value=mock_client), pytest.raises(getattr(telemetry, exc_name)):
            await tclient.write("/v1/write/append", {"table": "audit_log", "rows": []})


# ---------------------------------------------------------------------------
# Telemetry telemetry_health()
# ---------------------------------------------------------------------------


class TestTelemetryHealth:
    """Verify telemetry_health returns True/False without raising."""

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        from services.telemetry import client as tclient

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "ok"}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        with patch.object(tclient, "get_client", return_value=mock_client):
            assert await tclient.telemetry_health() is True

    @pytest.mark.asyncio
    async def test_returns_false_on_transport_error(self):
        from services.telemetry import client as tclient

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch.object(tclient, "get_client", return_value=mock_client):
            assert await tclient.telemetry_health() is False

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200(self):
        from services.telemetry import client as tclient

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=503))
        with patch.object(tclient, "get_client", return_value=mock_client):
            assert await tclient.telemetry_health() is False


# ---------------------------------------------------------------------------
# Redis publish() retries on ConnectionError
# ---------------------------------------------------------------------------


class TestRedisPublishRetry:
    """Verify publish retries on ConnectionError."""

    @pytest.mark.asyncio
    async def test_publish_retries_on_connection_error(self):
        from services.redis import publish

        mock_redis = MagicMock()
        mock_redis.publish = AsyncMock(side_effect=[ConnectionError("reset"), None])

        with (
            patch("services.redis.get_redis", return_value=mock_redis),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await publish("test-channel", {"msg": "hello"})
            assert mock_redis.publish.call_count == 2

    @pytest.mark.asyncio
    async def test_publish_gives_up_after_max_attempts(self):
        from services.redis import publish

        mock_redis = MagicMock()
        mock_redis.publish = AsyncMock(side_effect=ConnectionError("persistent failure"))

        with (
            patch("services.redis.get_redis", return_value=mock_redis),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await publish("test-channel", {"msg": "hello"})
            assert mock_redis.publish.call_count == 3

    @pytest.mark.asyncio
    async def test_publish_retries_on_os_error(self):
        from services.redis import publish

        mock_redis = MagicMock()
        mock_redis.publish = AsyncMock(side_effect=[OSError("network down"), None])

        with (
            patch("services.redis.get_redis", return_value=mock_redis),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await publish("test-channel", {"msg": "hello"})
            assert mock_redis.publish.call_count == 2


# ---------------------------------------------------------------------------
# CLI _request_with_retry()
# ---------------------------------------------------------------------------


class TestCliRetry:
    """Verify CLI _request_with_retry retries on 429/503/504."""

    def test_retries_on_429(self):
        from observal_cli.client import _request_with_retry

        mock_resp_429 = MagicMock(spec=httpx.Response)
        mock_resp_429.status_code = 429
        mock_resp_429.headers = {}

        mock_resp_200 = MagicMock(spec=httpx.Response)
        mock_resp_200.status_code = 200
        mock_resp_200.headers = {}
        mock_resp_200.raise_for_status = MagicMock()

        with patch("httpx.get", side_effect=[mock_resp_429, mock_resp_200]), patch("time.sleep"):
            r = _request_with_retry("get", "http://test/api", {"Authorization": "Bearer test-token"})
            assert r.status_code == 200

    def test_retries_on_503(self):
        from observal_cli.client import _request_with_retry

        mock_resp_503 = MagicMock(spec=httpx.Response)
        mock_resp_503.status_code = 503
        mock_resp_503.headers = {}

        mock_resp_200 = MagicMock(spec=httpx.Response)
        mock_resp_200.status_code = 200
        mock_resp_200.headers = {}
        mock_resp_200.raise_for_status = MagicMock()

        with patch("httpx.get", side_effect=[mock_resp_503, mock_resp_200]), patch("time.sleep"):
            r = _request_with_retry("get", "http://test/api", {"Authorization": "Bearer test-token"})
            assert r.status_code == 200

    def test_honors_retry_after_header(self):
        from observal_cli.client import _request_with_retry

        mock_resp_429 = MagicMock(spec=httpx.Response)
        mock_resp_429.status_code = 429
        mock_resp_429.headers = {"Retry-After": "3"}

        mock_resp_200 = MagicMock(spec=httpx.Response)
        mock_resp_200.status_code = 200
        mock_resp_200.headers = {}
        mock_resp_200.raise_for_status = MagicMock()

        with (
            patch("httpx.get", side_effect=[mock_resp_429, mock_resp_200]),
            patch("time.sleep") as mock_sleep,
        ):
            r = _request_with_retry("get", "http://test/api", {"Authorization": "Bearer test-token"})
            assert r.status_code == 200
            mock_sleep.assert_called_once_with(3.0)

    def test_does_not_retry_on_400(self):
        """Non-retryable status codes should raise immediately."""
        from observal_cli.client import _request_with_retry

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 400
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"detail": "bad request"}
        mock_resp.text = "bad request"
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("bad", request=MagicMock(), response=mock_resp)
        )

        with patch("httpx.get", return_value=mock_resp), pytest.raises(httpx.HTTPStatusError):
            _request_with_retry("get", "http://test/api", {"Authorization": "Bearer test-token"})
