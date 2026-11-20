import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

from lightbrain.browser_checks import run_browser, validate_criterion
from lightbrain.settings import configure_temp


class BrowserChecks(unittest.TestCase):
    def test_requires_visible_or_measurable_result(self):
        for rule in [
            {"steps": [{"action":"click","selector":"button"}]},
            {"steps": [{"action":"expect_visible","selector":"button"}], "path":"../outside.html"},
            {"steps": [{"action":"expect_count","selector":"li","value":-1}]},
        ]:
            with self.assertRaises(ValueError):
                validate_criterion(rule)

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "requires the browser worker extra")
    def test_renderer_action_and_negative_assertion(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=project) as d:
            root = Path(d)
            previous = tempfile.tempdir
            # Playwright's Node driver also allocates its process scratch under TEMP.
            from unittest.mock import patch
            with patch.dict("os.environ"):
                configure_temp(root)
                self.addCleanup(setattr, tempfile, "tempdir", previous)
                src = root / "src"
                src.mkdir()
                (src / "index.html").write_text('<input id="x"><button onclick="out.textContent=x.value.toUpperCase()">Apply</button>'
                                               '<output id="out"></output>', encoding="utf-8")
                steps = [{"action":"fill","selector":"#x","value":"blue"},
                         {"action":"click","selector":"button"},
                         {"action":"expect_text","selector":"#out","value":"BLUE"}]
                ctx = {"workdir": str(root / "work"), "artifacts": str(root / "artifacts")}
                ok, observed = run_browser(src, {"steps":steps}, ctx)
                self.assertTrue(ok, observed)
                png = Path(observed["screenshot_path"]).read_bytes()
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(hashlib.sha256(png).hexdigest(), observed["screenshot_sha256"])
                ok, observed = run_browser(src, {"steps":[{**steps[-1],"value":"WRONG"}]}, ctx)
                self.assertFalse(ok)
                self.assertTrue(observed["failure"])
