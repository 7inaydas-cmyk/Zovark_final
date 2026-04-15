"""
Tests for the healer's synthetic login health check.
Verifies detection of stale nginx DNS cache (502) and auto-restart logic.

Audit 5.13: import `agent.healer` directly and patch urllib/subprocess on that
module so the tests actually exercise production code. The previous version
re-implemented check_synthetic_login locally; any divergence in real healer
logic was invisible to the suite.
"""
import sys
import os
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

import pytest  # noqa: F401 — retained for collection compatibility

# Expose the agent/ directory on sys.path so `import healer` works.
_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.abspath(os.path.join(_HERE, "..", "..", "agent"))
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

# Set test env before importing healer — it reads some globals at import time.
os.environ.setdefault("HEALER_CHECK_INTERVAL", "30")
os.environ.setdefault(
    "SYNTHETIC_LOGIN_URL",
    "http://zovark-dashboard:3000/api/v1/auth/login",
)
os.environ.setdefault("SYNTHETIC_LOGIN_EMAIL", "admin@test.local")
os.environ.setdefault("SYNTHETIC_LOGIN_PASSWORD", "TestPass2026")

import healer  # noqa: E402  — import must follow the env setup above


class TestSyntheticLoginCheck:
    """Tests for agent.healer.check_synthetic_login()."""

    def _call(self):
        """Call the real function under test (no re-implementation)."""
        return healer.check_synthetic_login()

    @patch("healer.urllib.request.urlopen")
    @patch("healer.subprocess.run")
    def test_successful_login(self, mock_subprocess, mock_urlopen):
        """Successful login returns ok=True, no restart triggered."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"token":"eyJ...","user":{"email":"admin@test.local"}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = self._call()

        assert result["ok"] is True
        assert result["status_code"] == 200
        assert result["auto_fixed"] is False
        mock_subprocess.assert_not_called()

    @patch("healer.urllib.request.urlopen")
    @patch("healer.subprocess.run")
    def test_502_triggers_restart(self, mock_subprocess, mock_urlopen):
        """502 Bad Gateway triggers dashboard restart."""
        mock_urlopen.side_effect = HTTPError(
            url="http://test", code=502, msg="Bad Gateway",
            hdrs=None, fp=None,
        )

        result = self._call()

        assert result["ok"] is False
        assert result["status_code"] == 502
        assert result["auto_fixed"] is True
        mock_subprocess.assert_called_once_with(
            ["docker", "restart", "zovark-dashboard"],
            capture_output=True, text=True, timeout=30,
        )

    @patch("healer.urllib.request.urlopen")
    @patch("healer.subprocess.run")
    def test_503_triggers_restart(self, mock_subprocess, mock_urlopen):
        """503 Service Unavailable triggers dashboard restart."""
        mock_urlopen.side_effect = HTTPError(
            url="http://test", code=503, msg="Service Unavailable",
            hdrs=None, fp=None,
        )

        result = self._call()

        assert result["ok"] is False
        assert result["status_code"] == 503
        assert result["auto_fixed"] is True
        mock_subprocess.assert_called_once()

    @patch("healer.urllib.request.urlopen")
    @patch("healer.subprocess.run")
    def test_connection_refused_triggers_restart(self, mock_subprocess, mock_urlopen):
        """Connection refused (status 0) triggers dashboard restart."""
        mock_urlopen.side_effect = URLError("Connection refused")

        result = self._call()

        assert result["ok"] is False
        assert result["status_code"] == 0
        assert result["auto_fixed"] is True
        mock_subprocess.assert_called_once()

    @patch("healer.urllib.request.urlopen")
    @patch("healer.subprocess.run")
    def test_401_does_not_trigger_restart(self, mock_subprocess, mock_urlopen):
        """401 Unauthorized is an API issue, not a proxy issue — no restart."""
        mock_urlopen.side_effect = HTTPError(
            url="http://test", code=401, msg="Unauthorized",
            hdrs=None, fp=None,
        )

        result = self._call()

        assert result["ok"] is False
        assert result["status_code"] == 401
        assert result["auto_fixed"] is False
        mock_subprocess.assert_not_called()

    @patch("healer.urllib.request.urlopen")
    @patch("healer.subprocess.run")
    def test_500_does_not_trigger_restart(self, mock_subprocess, mock_urlopen):
        """500 Internal Server Error is an API bug, not proxy — no restart."""
        mock_urlopen.side_effect = HTTPError(
            url="http://test", code=500, msg="Internal Server Error",
            hdrs=None, fp=None,
        )

        result = self._call()

        assert result["ok"] is False
        assert result["status_code"] == 500
        assert result["auto_fixed"] is False
        mock_subprocess.assert_not_called()

    @patch("healer.urllib.request.urlopen")
    @patch("healer.subprocess.run")
    def test_200_without_token_is_not_ok(self, mock_subprocess, mock_urlopen):
        """200 response without a token field means login didn't actually work."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error":"invalid credentials"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = self._call()

        assert result["ok"] is False
        assert result["status_code"] == 200
        assert "no token" in result["detail"]
        # Not a proxy issue, so no restart
        mock_subprocess.assert_not_called()
