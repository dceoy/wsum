"""Tests for the dry_run CLI entry point."""

from __future__ import annotations

import json
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import dry_run

_FIXTURE = Path(__file__).parent / "fixtures" / "dry-run.json"


class DryRunCliTest(unittest.TestCase):
    """Tests for dry_run.main's stdout/stderr CLI contract."""

    def test_usage_error_writes_to_stderr(self) -> None:
        """Test that incorrect argc writes a usage message to stderr and returns 2."""
        with patch("sys.stderr", new_callable=StringIO) as stderr:
            code = dry_run.main(["dry_run.py"])
        assert code == 2
        assert "usage" in stderr.getvalue()

    def test_success_writes_json_result_to_stdout(self) -> None:
        """Test that a successful run writes the JSON run_fixture result to stdout."""
        with patch("sys.stdout", new_callable=StringIO) as stdout:
            code = dry_run.main(["dry_run.py", str(_FIXTURE)])
        assert code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["state_target_ids"] == ["product", "terms"]
        assert len(payload["cycles"]) == 2

    def test_invalid_fixture_writes_error_json_to_stdout(self) -> None:
        """Test that a fixture load failure writes {"error": ...} JSON to stdout."""
        missing = _FIXTURE.parent / "does-not-exist.json"
        with patch("sys.stdout", new_callable=StringIO) as stdout:
            code = dry_run.main(["dry_run.py", str(missing)])
        assert code == 1
        payload = json.loads(stdout.getvalue())
        assert payload["error"]["code"] == "fixture_invalid"
