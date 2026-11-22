"""Symbol- and import-aware ranking on local fixtures. No model calls."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agentctl.common import load_config
from agentctl.context import build_packet, excerpt
from agentctl.project import definitions, goal_identifiers, imports, initialize, inventory, rank_files


def entry(path, text):
    return {"path": path, "sha256": str(hash((path, text))), "text": text}


class Extraction(unittest.TestCase):
    def test_python_definitions_have_spans_methods_and_constants(self):
        text = "LIMIT = 3\n\nclass Store:\n    def save(self):\n        return 1\n\n\ndef helper():\n    pass\n"
        found = {name: (start, end) for name, start, end in definitions(entry("a.py", text))}
        self.assertEqual(found["LIMIT"], (1, 1))
        self.assertEqual(found["Store"], (3, 5))
        self.assertEqual(found["Store.save"], (4, 5))
        self.assertEqual(found["helper"], (8, 9))

    def test_unparsable_python_falls_back_to_line_patterns(self):
        found = [d[:2] for d in definitions(entry("b.py", "def ok():\n    pass\ndef broken(:\n"))]
        self.assertIn(("ok", 1), found)
        self.assertIn(("broken", 3), found)

    def test_other_language_definitions_end_before_next_definition(self):
        js = "export function loadRows(a) {\n  return a;\n}\nexport const saveRows = async (x) => x;\n"
        found = {name: (start, end) for name, start, end in definitions(entry("x.ts", js))}
        self.assertEqual(found["loadRows"], (1, 3))
        self.assertEqual(found["saveRows"][0], 4)
        c = "int parse_header(const char *p) {\n  return 0;\n}\nif (x) {\n"
        names = [d[0] for d in definitions(entry("x.c", c))]
        self.assertIn("parse_header", names)
        self.assertNotIn("if", names)

    def test_imports_resolve_only_to_scanned_local_files(self):
        known = {"pkg/__init__.py", "pkg/a.py", "pkg/b.py", "pkg/sub/c.py", "web/util.ts", "web/lib/index.js",
                 "src/x.h", "src/x.c"}
        py = entry("pkg/a.py", "import os\nfrom .b import thing\nfrom .sub import c\nfrom pkg import b\n")
        self.assertEqual(imports(py, known), {"pkg/b.py", "pkg/sub/c.py", "pkg/__init__.py"})
        ts = entry("web/main.ts", "import {u} from './util'\nconst l = require('./lib')\nimport 'react'\n")
        self.assertEqual(imports(ts, known), {"web/util.ts", "web/lib/index.js"})
        self.assertEqual(imports(entry("src/x.c", '#include "x.h"\n#include <stdio.h>\n'), known), {"src/x.h"})

    def test_goal_identifiers_mark_code_names(self):
        ids = goal_identifiers("Fix Store.save and `run` so parse_rows handles loadRows; check the rows")
        self.assertTrue(ids["store.save"] and ids["save"] and ids["parse_rows"] and ids["loadrows"] and ids["run"])
        self.assertFalse(ids["check"])
        self.assertNotIn("the", ids)


class Ranking(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(Path(self.temp.name) / "codex-home")})
        self.env.start()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "README.md").write_text("# Tool\n" + "parse_rows parse_rows parse_rows reads CSV rows.\n" * 3)
        (self.root / "docs").mkdir()
        (self.root / "docs/USAGE.md").write_text("Call parse_rows. parse_rows is fast. parse_rows!\n")
        (self.root / "app").mkdir()
        (self.root / "app/__init__.py").write_text("")
        (self.root / "app/rows.py").write_text("def parse_rows(text):\n    return text.split()\n")
        (self.root / "app/cli.py").write_text("from .rows import parse_rows\n\ndef main():\n    return parse_rows('a')\n")
        (self.root / "app/report.py").write_text("def render():\n    return 'report'\n")
        (self.root / "app/check.py").write_text("def check():\n    return True\n")
        initialize(self.root)
        self.cfg = load_config(self.root)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def ranked(self, goal):
        return [(p, reasons) for _, p, reasons, _ in rank_files(inventory(self.root, self.cfg), goal)]

    def test_definition_outranks_documents_that_mention_the_symbol(self):
        ranked = self.ranked("parse_rows drops empty fields")
        self.assertEqual(ranked[0][0], "app/rows.py")
        self.assertIn("defines: parse_rows", ranked[0][1])

    def test_plain_words_do_not_count_as_symbol_definitions(self):
        scores = {p: (s, r) for s, p, r, _ in rank_files(inventory(self.root, self.cfg), "check the export for broken rows")}
        score, reasons = scores["app/check.py"]
        self.assertFalse(any(r.startswith("defines:") for r in reasons))
        self.assertIn("defines common word: check", reasons)
        # Path match (12) + content (1) + weak definition (4): far below a code-name match (+40).
        self.assertLess(score, 40)

    def test_import_neighbours_are_boosted_with_reason(self):
        reasons = dict(self.ranked("parse_rows drops empty fields"))
        self.assertIn("imports app/rows.py", reasons["app/cli.py"])
        self.assertNotIn("app/report.py", reasons)

    def test_widely_imported_module_gets_smaller_neighbour_boost(self):
        (self.root / "app/common.py").write_text("def shared():\n    return 0\n")
        for i in range(8):
            (self.root / f"app/user{i}.py").write_text("from .common import shared\n")
        (self.root / "app/rows.py").write_text("from .common import shared\nfrom .report import render\n\n"
                                               "def parse_rows(text):\n    return text.split()\n")
        scores = {p: s for s, p, _, _ in rank_files(inventory(self.root, self.cfg), "parse_rows drops empty fields")}
        self.assertLess(scores["app/common.py"], scores["app/report.py"])

    def test_packet_contains_whole_named_definition_in_long_file(self):
        filler = "".join(f"def other_{i}():\n    return {i}\n\n" for i in range(120))
        body = "def target_func(value):\n" + "".join(f"    step_{i} = value + {i}\n" for i in range(15)) + "    return value\n"
        (self.root / "app/big.py").write_text(filler + body)
        start = filler.count("\n") + 1
        with tempfile.TemporaryDirectory() as run_dir:
            manifest = build_packet(self.root, self.cfg, "Why does target_func ignore value?", Path(run_dir),
                                    "local", permission="read-only")
        chosen = next(s for s in manifest["selected"] if s["path"] == "app/big.py")
        self.assertTrue(set(range(start, start + 17)) <= set(chosen["line_numbers"]))
        self.assertIn("defines: target_func", chosen["reason"])

    def test_excerpt_respects_line_limit_with_focus(self):
        text = "\n".join(f"line {i}" for i in range(400))
        _, numbers = excerpt(text, "line", 24, focus=[(100, 180)])
        self.assertLessEqual(len(numbers), 24)
        self.assertIn(100, numbers)


if __name__ == "__main__":
    unittest.main()
