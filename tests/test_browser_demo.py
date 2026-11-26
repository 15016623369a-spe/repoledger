import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from examples import browse_demo


class BrowserDemoTests(unittest.TestCase):
    def test_exit_status_tracks_browser_calls(self):
        server = Path(__file__).with_name("fake_mcp_server.py").resolve()
        for host, expected_status in (("docs.python.org", 0), ("elsewhere.example", 1)):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "mcp.json"
                config.write_text(json.dumps({"mcpServers": {"playwright": {
                    "command": sys.executable,
                    "args": [str(server), "--playwright"],
                    "allowedTools": ["browser_navigate", "browser_snapshot"],
                    "stateful": True,
                    "replaySafeTools": ["browser_navigate"],
                    "urlArguments": {"browser_navigate": "url"},
                    "allowedDomains": ["docs.python.org"],
                }}}), encoding="utf-8")
                with mock.patch.multiple(browse_demo, CONFIG=config, OUT=root / "out",
                                         URL=f"https://{host}/example"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(browse_demo.main(), expected_status)
