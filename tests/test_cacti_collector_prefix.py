"""Exercise the pure collector parser without loading credentials or starting it."""
import ast
import contextlib
import io
from pathlib import Path
import re
import unittest

from bs4 import BeautifulSoup


source = Path(__file__).resolve().parents[1] / "collector_cacti/cactydown.py"
tree = ast.parse(source.read_text())
parser = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_down_devices")
namespace = {"BeautifulSoup": BeautifulSoup, "re": re}
exec(compile(ast.Module(body=[parser], type_ignores=[]), str(source), "exec"), namespace)


class CollectorPrefixTests(unittest.TestCase):
    def parse(self, name):
        html = f'<table bgcolor="#993333"><tr><td><b>BANK JATIM</b><table id="status_1"><tr><td><b>{name}</b></td></tr><tr><td>ONU Status</td><td>:</td><td>LOS</td></tr></table></td></tr></table>'
        with contextlib.redirect_stdout(io.StringIO()):
            return namespace["parse_down_devices"](html)[0]

    def test_prefix_keeps_original_alarm_identity_and_institution(self):
        legacy = self.parse("4700020-38090_BANK JATIM_CAPEM")
        marked = self.parse("[HD-D] 4700020-38090_BANK JATIM_CAPEM")
        self.assertEqual(marked, legacy)
        self.assertEqual(marked["group"], "BANK JATIM")

    def test_other_markers_are_not_removed(self):
        self.assertEqual(self.parse("[ATCS] 000123_SITE")["name"], "[ATCS] 000123_SITE")


if __name__ == "__main__":
    unittest.main()
