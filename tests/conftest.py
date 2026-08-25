"""Ensure the monitor scripts package is importable before test collection.

Test modules import monitor scripts directly (e.g. ``from diff import ...``)
rather than as a qualified package, so the scripts directory must be on
``sys.path`` before pytest collects any test module. Doing this here,
rather than relying on import order inside each test file, keeps the setup
independent of how isort orders each file's imports.
"""

from __future__ import annotations

from tests import support

__all__ = ["support"]
