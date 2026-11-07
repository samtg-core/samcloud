"""Focused tests for the port allocator (ports.py)."""

from unittest.mock import patch, MagicMock

import pytest

import db
import ports as port_alloc


class TestAllocateGaps:
    """Allocate should fill gaps in the range."""

    def test_fills_gap_in_forward_range(self, seeded_db):
        """Seeded data has 18001, 18003-18005, 18007-18010.
        18002 should be the first gap allocated."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            port = port_alloc.allocate("forward", "test/gap-service")
        assert port == 18002

    def test_fills_second_gap(self, seeded_db):
        """After filling 18002, 18006 is the next gap."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            port_alloc.allocate("forward", "test/fill-18002")
            port = port_alloc.allocate("forward", "test/fill-18006")
        assert port == 18006

    def test_tunnel_fills_gap(self, seeded_db):
        """Seeded tunnels: 10001-10007. Next available is 10008."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            port = port_alloc.allocate("tunnel", "test-device")
        assert port == 10008


class TestPreferredPort:
    """Preferred port allocation."""

    def test_preferred_port_allocated(self, tmp_db):
        """Preferred port within range is allocated successfully."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            port = port_alloc.allocate("forward", "test/svc", preferred=18050)
        assert port == 18050

        # Verify it was recorded
        record = port_alloc.check(18050, "forward")
        assert record is not None
        assert record["owner_id"] == "test/svc"

    def test_preferred_port_collision(self, tmp_db):
        """Preferred port that is already taken raises RuntimeError."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            port_alloc.allocate("forward", "test/first", preferred=18050)
            with pytest.raises(RuntimeError, match="already allocated"):
                port_alloc.allocate("forward", "test/second", preferred=18050)

    def test_preferred_port_same_owner_returns_idempotent(self, tmp_db):
        """Re-requesting a port by the same owner returns it without error."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            p1 = port_alloc.allocate("forward", "test/svc", preferred=18050)
            p2 = port_alloc.allocate("forward", "test/svc", preferred=18050)
        assert p1 == p2 == 18050

    def test_preferred_port_outside_range_raises(self, tmp_db):
        """Preferred port outside the type's range raises ValueError."""
        with pytest.raises(ValueError, match="outside"):
            port_alloc.allocate("forward", "test/svc", preferred=9999)

    def test_preferred_port_outside_range_high(self, tmp_db):
        """Preferred port above range raises ValueError."""
        with pytest.raises(ValueError, match="outside"):
            port_alloc.allocate("tunnel", "test/svc", preferred=20000)


class TestRangeOverflow:
    """All ports in a range taken."""

    def test_exhausted_range_raises(self, tmp_db):
        """Filling every port in a small range then requesting another raises ValueError."""
        # Monitor range: 19001-19099 (99 ports). Use a smaller range for test speed.
        # Patch RANGES to a tiny range for this test.
        tiny_ranges = {**port_alloc.RANGES, "monitor": (19001, 19005)}
        with patch.object(port_alloc, "RANGES", tiny_ranges), \
             patch.object(port_alloc, "_port_in_use", return_value=False):
            for i in range(5):
                port_alloc.allocate("monitor", f"dev-{i}")
            with pytest.raises(ValueError, match="No available ports"):
                port_alloc.allocate("monitor", "dev-overflow")


class TestLsofDetection:
    """Mock subprocess to simulate lsof detection."""

    def test_skips_port_in_use_by_lsof(self, tmp_db):
        """If lsof reports a port is in use, allocator skips it."""
        def fake_in_use(port):
            return port == 18001  # Simulate 18001 is occupied

        with patch.object(port_alloc, "_port_in_use", side_effect=fake_in_use):
            port = port_alloc.allocate("forward", "test/svc")
        assert port == 18002  # Skipped 18001

    def test_preferred_port_in_use_by_lsof_raises(self, tmp_db):
        """Preferred port that is in use by lsof raises RuntimeError."""
        with patch.object(port_alloc, "_port_in_use", return_value=True):
            with pytest.raises(RuntimeError, match="in use by another process"):
                port_alloc.allocate("forward", "test/svc", preferred=18050)

    def test_lsof_subprocess_called(self, tmp_db):
        """Verify _port_in_use calls subprocess.run with correct args."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("ports.subprocess.run", return_value=mock_result) as mock_run:
            result = port_alloc._port_in_use(18001)
        mock_run.assert_called_once_with(
            ["lsof", "-i", ":18001", "-P", "-n"],
            capture_output=True, text=True, timeout=3,
        )
        assert result is False

    def test_lsof_listen_detected(self, tmp_db):
        """lsof returning LISTEN in output means port is in use."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "COMMAND  PID USER  FD TYPE DEVICE SIZE/OFF NODE NAME\npython  1234 alice  3u  IPv4  LISTEN\n"
        with patch("ports.subprocess.run", return_value=mock_result):
            assert port_alloc._port_in_use(18001) is True

    def test_lsof_timeout_returns_false(self, tmp_db):
        """If lsof times out, treat port as not in use."""
        import subprocess
        with patch("ports.subprocess.run", side_effect=subprocess.TimeoutExpired("lsof", 3)):
            assert port_alloc._port_in_use(18001) is False


class TestReleaseAndReallocate:
    """Release a port and re-allocate it."""

    def test_release_then_reallocate(self, tmp_db):
        """After releasing a port, it becomes available again."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            port = port_alloc.allocate("forward", "test/svc", preferred=18050)
            assert port == 18050

            # Verify it's allocated
            assert port_alloc.check(18050, "forward") is not None

            # Release
            port_alloc.release(18050, "forward")
            assert port_alloc.check(18050, "forward") is None

            # Re-allocate to a different owner
            port2 = port_alloc.allocate("forward", "test/other-svc", preferred=18050)
            assert port2 == 18050

    def test_release_creates_audit_entry(self, tmp_db):
        """Releasing a port creates an audit log entry."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            port_alloc.allocate("forward", "test/svc", preferred=18050)
            port_alloc.release(18050, "forward")

        logs = db.query("SELECT * FROM audit_log WHERE action = 'port.release'")
        assert len(logs) == 1
        assert "18050" in logs[0]["detail"]


class TestInvalidPortType:
    """Invalid port type handling."""

    def test_unknown_type_raises(self, tmp_db):
        with pytest.raises(ValueError, match="Unknown port type"):
            port_alloc.allocate("bogus", "test/svc")


class TestListAll:
    """list_all function."""

    def test_list_all_empty(self, tmp_db):
        result = port_alloc.list_all()
        assert result == []

    def test_list_all_with_filter(self, seeded_db):
        forwards = port_alloc.list_all("forward")
        assert all(p["type"] == "forward" for p in forwards)
        assert len(forwards) > 0

    def test_list_all_no_filter(self, seeded_db):
        all_ports = port_alloc.list_all()
        assert len(all_ports) > 0
        # Should include multiple types
        types = {p["type"] for p in all_ports}
        assert "tunnel" in types
        assert "forward" in types
