from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.pg_tools import resolve_tool


class PostgreSQLToolResolutionTests(unittest.TestCase):
    def test_pg_bin_takes_precedence_and_does_not_require_machine_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / ("pg_dump.exe" if os.name == "nt" else "pg_dump")
            tool.touch()
            with patch.dict(os.environ, {"PG_BIN": directory}, clear=False), patch("scripts.pg_tools.shutil.which", return_value=None):
                self.assertEqual(resolve_tool("pg_dump"), str(tool))

    def test_path_fallback_is_supported(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("scripts.pg_tools.shutil.which", return_value="/usr/bin/pg_restore"):
            self.assertEqual(resolve_tool("pg_restore"), "/usr/bin/pg_restore")


if __name__ == "__main__":
    unittest.main()
