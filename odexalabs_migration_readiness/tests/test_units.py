# -*- coding: utf-8 -*-
"""Pure-function tests: status dates, origin heuristics, pattern relevance."""

import datetime
import os
import tempfile

from odoo.tests import TransactionCase, tagged

from ..lib import origin, runner, source_scan


@tagged("post_install", "-at_install", "odexalabs_mrs")
class TestSupportStatus(TransactionCase):
    ENTRY = {"version": "18.0", "support_ends": "2027-09-01"}

    def status(self, day):
        return runner.support_status(self.ENTRY, datetime.date.fromisoformat(day))

    def test_month_precision(self):
        self.assertEqual(self.status("2027-02-28"), "supported")
        self.assertEqual(self.status("2027-03-01"), "ending_soon")
        self.assertEqual(self.status("2027-09-30"), "ending_soon")
        self.assertEqual(self.status("2027-10-01"), "unsupported")

    def test_end_shown_as_month(self):
        self.assertEqual(runner.support_end_month(self.ENTRY), "September 2027")

    def test_no_stored_status(self):
        for entry in runner.load_support_matrix():
            self.assertNotIn("status", entry, "status must be computed at scan time, never stored")
            self.assertIsNotNone(runner.support_status(entry))

    def test_no_date_is_not_a_guess(self):
        self.assertIsNone(runner.support_status({"version": "x"}))


@tagged("post_install", "-at_install", "odexalabs_mrs")
class TestOrigin(TransactionCase):
    def classify(self, **info):
        info.setdefault("name", "x_mod")
        info.setdefault("on_disk", True)
        return origin.classify(info, ["Acme Foods Ltd"], {"acmefoods.example"}, "18.0", "19.0")

    def test_standard_and_enterprise(self):
        self.assertEqual(self.classify(standard=True, standard_license="LGPL-3")["bucket"], origin.ODOO_STANDARD)
        self.assertEqual(self.classify(standard=True, standard_license="OEEL-1")["bucket"], origin.ODOO_ENTERPRISE)

    def test_oca_with_repo_link(self):
        result = self.classify(
            author="Camptocamp, Odoo Community Association (OCA)",
            website="https://github.com/OCA/server-tools",
        )
        self.assertEqual(result["bucket"], origin.OCA)
        self.assertEqual(result["oca_repo"], "server-tools")
        text = origin.target_action(result, "x_mod", "19.0")
        self.assertIn("repository server-tools", text)
        self.assertNotIn("http", text, "no constructed link: it cannot be verified offline")

    def test_paid_vendor(self):
        result = self.classify(author="Vendor Co", manifest={"price": 49.0, "currency": "EUR"}, license="OPL-1")
        self.assertEqual(result["bucket"], origin.PAID_VENDOR)
        self.assertIn("Ask Vendor Co for a version for Odoo 19.0", origin.target_action(result, "x_mod", "19.0"))

    def test_odoo_author_is_odoo(self):
        self.assertEqual(self.classify(author="Odoo S.A.", license="LGPL-3")["bucket"], origin.ODOO_STANDARD)
        self.assertEqual(self.classify(author="Odoo S.A.", license="OEEL-1")["bucket"], origin.ODOO_ENTERPRISE)

    def test_removed_dependency_is_named(self):
        result = self.classify(author="Some Integrator")
        text = origin.target_action(result, "x_mod", "20.0", removed_deps=["web_editor"])
        self.assertIn("depends on web_editor, which does not exist in Odoo 20.0", text)

    def test_sizes_are_human(self):
        self.assertEqual(origin.human_size(854), "0.8 kB")
        self.assertEqual(origin.human_size(8966487), "8.6 MB")

    def test_in_house_by_company_and_scaffold(self):
        self.assertEqual(self.classify(author="Acme Foods Ltd")["bucket"], origin.IN_HOUSE)
        self.assertEqual(self.classify(author="My Company", website="https://www.yourcompany.com")["bucket"],
                         origin.IN_HOUSE)
        self.assertEqual(self.classify(website="https://acmefoods.example/odoo")["bucket"], origin.IN_HOUSE)

    def test_studio_and_imported(self):
        self.assertEqual(self.classify(name="studio_customization", author="Acme Foods Ltd")["bucket"],
                         origin.STUDIO_IMPORTED)
        self.assertEqual(self.classify(imported=True, on_disk=False)["bucket"], origin.STUDIO_IMPORTED)

    def test_named_third_party(self):
        result = self.classify(author="Some Integrator", website="https://integrator.example")
        self.assertEqual(result["bucket"], origin.THIRD_PARTY)
        self.assertTrue(result["signals"])

    def test_conflict_reads_unclear(self):
        result = self.classify(author="Odoo Community Association (OCA)",
                               manifest={"price": 10, "currency": "USD"}, license="OPL-1")
        self.assertEqual(result["bucket"], origin.UNCLEAR)
        self.assertTrue(result["conflict"])
        self.assertTrue(any("more than one origin" in s for s in result["signals"]))

    def test_no_signal_reads_unclear(self):
        result = self.classify()
        self.assertEqual(result["bucket"], origin.UNCLEAR)
        self.assertFalse(result["conflict"])
        self.assertTrue(result["signals"], "an unclear row still shows why")

    def test_module_type_is_never_a_signal(self):
        a = self.classify(author="Some Integrator")
        b = self.classify(author="Some Integrator", module_type="official")
        self.assertEqual(a, b)

    def test_remote_credentials_stripped(self):
        self.assertEqual(origin.strip_credentials("https://user:s3cret@github.com/OCA/web.git"),
                         "https://github.com/OCA/web.git")


@tagged("post_install", "-at_install", "odexalabs_mrs")
class TestSourceScan(TransactionCase):
    PATTERNS = [
        {"id": "p18", "regex": r"<tree\b", "glob": ["*.xml"], "since": "18.0", "explanation": "e"},
        {"id": "p19", "regex": r"_sql_constraints\s*=", "glob": ["*.py"], "since": "19.0", "explanation": "e"},
    ]

    def setUp(self):
        super().setUp()
        import re
        for p in self.PATTERNS:
            p["_re"] = re.compile(p["regex"])

    def test_relevance_is_current_to_target(self):
        ids = lambda c, t: [p["id"] for p in source_scan.relevant_patterns(self.PATTERNS, c, t)]
        self.assertEqual(ids("17.0", "19.0"), ["p18", "p19"])
        self.assertEqual(ids("18.0", "19.0"), ["p19"])
        self.assertEqual(ids("19.0", "19.0"), [])
        self.assertEqual(ids("19.0", "17.0"), [])

    def test_hits_at_file_and_line(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "views"))
            with open(os.path.join(root, "views", "v.xml"), "w") as handle:
                handle.write("<odoo>\n  <" + "tree>\n</odoo>\n")
            with open(os.path.join(root, "m.py"), "w") as handle:
                handle.write("x = 1\n_sql_" + "constraints = []\n")
            hits, skipped, files = source_scan.scan_module("demo", root, self.PATTERNS)
        got = sorted((h["pattern"], h["file"], h["line"]) for h in hits)
        self.assertEqual(got, [("p18", os.path.join("views", "v.xml"), 2), ("p19", "m.py", 2)])
        self.assertEqual(files, 2)
        self.assertFalse(skipped)

    def test_skip_path_excludes_client_templates(self):
        pattern = {"id": "srv", "regex": r"t-esc=", "glob": ["*.xml"], "since": "20.0",
                   "explanation": "e", "skip_path": "(^|/)static/"}
        import re
        pattern["_re"] = re.compile(pattern["regex"])
        pattern["_skip"] = re.compile(pattern["skip_path"])
        with tempfile.TemporaryDirectory() as root:
            for rel in ("views/a.xml", "static/src/b.xml"):
                os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w") as handle:
                    handle.write('<t t-esc="x"/>\n')
            hits, _skipped, _files = source_scan.scan_module("demo", root, [pattern])
        self.assertEqual([h["file"] for h in hits], ["views/a.xml"])

    def test_catalogue_is_well_formed(self):
        patterns = source_scan.load_patterns()
        self.assertTrue(patterns, "the deprecation catalogue must not be empty")
        for p in patterns:
            for key in ("id", "regex", "glob", "since", "explanation", "source"):
                self.assertIn(key, p, "%s lacks %s" % (p.get("id"), key))
            self.assertRegex(p["since"], r"^\d+\.0$")
            if p.get("removed"):
                self.assertRegex(p["removed"], r"^\d+\.0$")
                self.assertGreater(float(p["removed"]), float(p["since"]))

    def test_removed_counts_as_a_change_and_gap_is_named(self):
        patterns = self.PATTERNS + [
            {"id": "p18r19", "regex": "x", "glob": ["*.py"], "since": "18.0", "removed": "19.0", "explanation": "e"},
        ]
        ids = [p["id"] for p in source_scan.relevant_patterns(patterns, "18.0", "19.0")]
        self.assertIn("p18r19", ids, "removal inside (current, target] makes a pattern relevant")
        self.assertEqual(source_scan.uncatalogued_gap(patterns, "18.0", "19.0"), [])
        self.assertEqual(source_scan.uncatalogued_gap(patterns, "19.0", "20.0"), ["20.0"])
        self.assertEqual(source_scan.uncatalogued_gap(patterns, "15.0", "19.0"), ["16.0", "17.0"])
        self.assertEqual(source_scan.uncatalogued_gap(patterns, "17.0", "21.0"), ["20.0", "21.0"])

    def test_shipped_catalogue_covers_every_supported_step(self):
        patterns = source_scan.load_patterns()
        newest = max((e["version"] for e in runner.load_support_matrix()), key=float)
        for current in ("15.0", "16.0", "17.0", "18.0", "19.0"):
            self.assertEqual(source_scan.uncatalogued_gap(patterns, current, newest), [],
                             "the catalogue has no entry for a version between %s and %s" % (current, newest))


_NEWER_THAN_37_ATTRS = {
    "removeprefix", "removesuffix",
    "cached_property", "isqrt", "prod",
    "bit_count", "is_relative_to", "with_stem",
}
_NEWER_THAN_37_MODULES = {"zoneinfo", "graphlib", "tomllib", "importlib.metadata"}


@tagged("post_install", "-at_install", "odexalabs_mrs")
class TestPythonFloor(TransactionCase):
    def _sources(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("tools", "__pycache__")]
            for name in filenames:
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as handle:
                        yield os.path.relpath(path, root), handle.read()

    def test_source_parses_as_python_37(self):
        """feature_version alone does not hold the line: Python 3.9.2's parser
        accepts an assignment expression with feature_version=(3, 7) (found by
        planting one, 2026-10-15). The syntax added after 3.7 is named node by
        node instead; the parse with feature_version stays as a second net."""
        import ast

        newer = tuple(getattr(ast, n) for n in ("NamedExpr", "Match", "TryStar", "TypeAlias")
                      if hasattr(ast, n))
        found = []
        for rel, text in self._sources():
            try:
                tree = ast.parse(text, filename=rel, feature_version=(3, 7))
            except SyntaxError as exc:
                self.fail("%s needs a Python newer than 3.7: %s" % (rel, exc))
            for node in ast.walk(tree):
                if isinstance(node, newer):
                    found.append("%s:%s %s" % (rel, node.lineno, type(node).__name__))
                elif isinstance(node, ast.arguments) and getattr(node, "posonlyargs", None):
                    found.append("%s:%s positional-only parameters" % (rel, node.posonlyargs[0].lineno))
        self.assertEqual(found, [])

    def test_no_call_newer_than_python_37(self):
        import ast

        found = []
        for rel, text in self._sources():
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Attribute) and node.attr in _NEWER_THAN_37_ATTRS:
                    found.append("%s:%s .%s" % (rel, node.lineno, node.attr))
                elif isinstance(node, ast.Import):
                    found += ["%s:%s import %s" % (rel, node.lineno, a.name)
                              for a in node.names if a.name in _NEWER_THAN_37_MODULES]
                elif isinstance(node, ast.ImportFrom) and node.module in _NEWER_THAN_37_MODULES:
                    found.append("%s:%s from %s" % (rel, node.lineno, node.module))
                elif isinstance(node, ast.Call) and any(k.arg == "strict" for k in node.keywords) \
                        and getattr(node.func, "id", None) == "zip":
                    found.append("%s:%s zip(strict=)" % (rel, node.lineno))
        self.assertEqual(found, [])
