# -*- coding: utf-8 -*-
"""Claim tests: one per guarantee the module, its listing and its guide make.

Each test tries to falsify a claim rather than exercise a path. Each is
proven to bite by planting the violation in a scratch copy of the module
(tools/prove_claims.py, not shipped) and watching the test fail.

  READ-ONLY   a full scan changes no table outside odexalabs_migration_*,
              runs no DDL, moves no other sequence, and leaves no session
              setting behind.
  OFFLINE     a full scan opens no socket and resolves no name; no shipped
              Python imports a network library; the App Store page loads
              nothing from outside the module.
  NO VERDICT  no check condition contains a number; the one stated
              definition (ENDING_SOON_MONTHS) is exempt by name; inventory
              models carry no severity/verdict field; inventory rows carry
              no verdict wording; views carry no decoration.
"""

import ast
import os
import re
import socket
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..lib import runner
from ..models.migration_scan import flush_all, invalidate_all

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWEST = max((e["version"] for e in runner.load_support_matrix()), key=lambda v: int(v.split(".")[0]))
OWN_TABLE_PREFIX = "odexalabs_migration_"
INVENTORY_MODELS = [
    "odexalabs.migration.module",
    "odexalabs.migration.int4",
    "odexalabs.migration.table",
    "odexalabs.migration.studio",
    "odexalabs.migration.view",
    "odexalabs.migration.code.hit",
]

_STAT_SNAPSHOT = """
SELECT relname, n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd
  FROM pg_stat_xact_user_tables
"""
_CATALOG_FINGERPRINT = """
SELECT md5(string_agg(x, '|' ORDER BY x)) FROM (
    SELECT 'c' || oid || ':' || relname || ':' || relkind::text || ':' || relnatts FROM pg_class
    UNION ALL SELECT 'a' || attrelid || ':' || attname || ':' || atttypid || ':' || attnotnull
                     || ':' || attisdropped FROM pg_attribute
    UNION ALL SELECT 'k' || oid || ':' || conname || ':' || convalidated FROM pg_constraint
    UNION ALL SELECT 'i' || indexrelid || ':' || indisvalid FROM pg_index
    UNION ALL SELECT 't' || oid || ':' || tgname || ':' || tgenabled::text FROM pg_trigger
    UNION ALL SELECT 'p' || oid || ':' || proname FROM pg_proc
    UNION ALL SELECT 'n' || oid || ':' || nspname FROM pg_namespace
) s(x)
"""
_SEQUENCES = """
SELECT schemaname || '.' || sequencename, last_value
  FROM pg_sequences
 WHERE sequencename NOT LIKE 'odexalabs\\_migration\\_%%'
"""


def _shipped_python():
    for root, dirs, files in os.walk(MODULE_ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "tools", "__pycache__")]
        for fname in files:
            if fname.endswith(".py"):
                yield os.path.join(root, fname)


@tagged("post_install", "-at_install", "odexalabs_mrs", "odexalabs_mrs_claims")
class TestClaimReadOnly(TransactionCase):
    def _snapshot(self):
        cr = self.env.cr
        flush_all(self.env)
        cr.execute(_STAT_SNAPSHOT)
        stats = {row[0]: row[1:] for row in cr.fetchall()}
        cr.execute(_CATALOG_FINGERPRINT)
        catalog = cr.fetchone()[0]
        cr.execute(_SEQUENCES)
        sequences = dict(cr.fetchall())
        cr.execute("SHOW statement_timeout")
        timeout = cr.fetchone()[0]
        return stats, catalog, sequences, timeout

    def test_full_scan_writes_nothing_outside_its_own_tables(self):
        scan = self.env["odexalabs.migration.scan"].create({"target_version": NEWEST})
        before = self._snapshot()
        scan.action_run_scan()
        after = self._snapshot()

        changed = sorted(
            name for name, counts in after[0].items()
            if counts != before[0].get(name, (0, 0, 0, 0)) and not name.startswith(OWN_TABLE_PREFIX)
        )
        self.assertEqual(changed, [], "the scan inserted, updated or deleted rows in other tables")
        own = [n for n, c in after[0].items() if n.startswith(OWN_TABLE_PREFIX) and c != before[0].get(n)]
        self.assertTrue(own, "positive control: the scan's own writes must be visible to this snapshot")
        self.assertEqual(after[1], before[1], "the scan changed the catalog (DDL ran)")
        moved = sorted(k for k in after[2] if after[2][k] != before[2].get(k))
        self.assertEqual(moved, [], "the scan moved sequences outside its own tables")
        self.assertEqual(after[3], before[3], "the scan left a session setting changed")


@tagged("post_install", "-at_install", "odexalabs_mrs", "odexalabs_mrs_claims")
class TestClaimOffline(TransactionCase):
    def test_full_scan_opens_no_socket(self):
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(args[:2])
            raise OSError("network disabled by the offline claim test")

        scan = self.env["odexalabs.migration.scan"].create({"target_version": NEWEST})
        with patch.object(socket.socket, "connect", refuse), \
                patch.object(socket.socket, "connect_ex", refuse), \
                patch.object(socket, "create_connection", refuse), \
                patch.object(socket, "getaddrinfo", refuse):
            scan.action_run_scan()
        self.assertEqual(attempts, [], "the scan tried to reach the network")
        self.assertEqual(scan.state, "done")

    def test_no_network_library_in_shipped_python(self):
        forbidden = re.compile(
            r"^(socket|ssl|requests|urllib3?|http|xmlrpc|ftplib|smtplib|telnetlib|httpx|aiohttp"
            r"|websocket|odoo\.addons\.iap)(\.|$)"
        )
        offenders = []
        for path in _shipped_python():
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "__import__":
                    names = ["__import__"]
                for name in names:
                    if name == "__import__" or forbidden.match(name):
                        offenders.append("%s: %s" % (os.path.relpath(path, MODULE_ROOT), name))
        self.assertEqual(offenders, [])

    def test_store_page_loads_nothing_from_outside(self):
        path = os.path.join(MODULE_ROOT, "static", "description", "index.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        external = re.findall(r"""(?:src|srcset)\s*=\s*["'](?:https?:)?//[^"']+""", html, re.I)
        external += re.findall(r"""<link[^>]+href\s*=\s*["'](?:https?:)?//""", html, re.I)
        external += re.findall(r"<script\b", html, re.I)
        external += re.findall(r"url\(\s*['\"]?(?:https?:)?//", html, re.I)
        self.assertEqual(external, [], "the App Store page must not load remote resources")

    def test_no_client_side_code(self):
        found = [
            os.path.join(root, f)
            for root, _d, files in os.walk(MODULE_ROOT)
            for f in files if f.endswith((".js", ".mjs", ".ts"))
        ]
        self.assertEqual(found, [])


@tagged("post_install", "-at_install", "odexalabs_mrs", "odexalabs_mrs_claims")
class TestClaimNoThreshold(TransactionCase):
    VERDICT_FIELDS = re.compile(r"severity|verdict|status|risk|score|level|rating|priority|health")
    VERDICT_WORDS = re.compile(
        r"\btoo (high|large|big|many|old|low|much)\b|\bexceed|\bdanger|\bcritical\b|\bunhealthy\b"
        r"|\bat risk\b|\bhigh risk\b|\bbad\b|\bhealthy\b",
        re.IGNORECASE,
    )

    def _numbers(self, node):
        return [
            n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and type(n.value) in (int, float, complex)
        ]

    def test_check_conditions_hold_no_number(self):
        checks = runner.load_public_checks() + runner.load_native_checks()
        self.assertGreaterEqual(len(checks), 1)
        for check in checks:
            tree = ast.parse(check["condition"].strip(), mode="eval")
            self.assertEqual(self._numbers(tree), [], "%s: %s" % (check["id"], check["condition"]))

    def test_the_one_definition_is_exempt_by_name_only(self):
        self.assertEqual(runner.ENDING_SOON_MONTHS, 6, "the stated definition changed or vanished")
        with open(runner.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        status = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "support_status")
        names = {n.id for n in ast.walk(status) if isinstance(n, ast.Name)}
        self.assertIn("ENDING_SOON_MONTHS", names)
        self.assertEqual(self._numbers(status), [], "a second number appeared in the status rule")

    def test_inventory_models_carry_no_verdict_field(self):
        for model in INVENTORY_MODELS:
            bad = [name for name in self.env[model]._fields if self.VERDICT_FIELDS.search(name)]
            self.assertEqual(bad, [], model)

    def test_inventory_rows_carry_no_verdict_wording(self):
        cr = self.env.cr
        cr.execute("CREATE TABLE odexalabs_mrs_t_big (id serial PRIMARY KEY)")
        cr.execute("SELECT setval('odexalabs_mrs_t_big_id_seq', 2147483000)")
        scan = self.env["odexalabs.migration.scan"].create({"target_version": NEWEST})
        scan.action_run_scan()
        self.assertEqual(scan.int4_ids.sorted("sequence")[0].table_name, "odexalabs_mrs_t_big")
        texts = []
        for model in INVENTORY_MODELS:
            rows = self.env[model].search([("scan_id", "=", scan.id)])
            for name, field in rows._fields.items():
                if field.type in ("char", "text", "selection"):
                    texts += [str(v) for v in rows.mapped(name) if v]
        texts.append(scan.inv_code_summary or "")
        hits = [t for t in texts if self.VERDICT_WORDS.search(t)]
        self.assertEqual(hits, [])

    def test_views_carry_no_decoration(self):
        with open(os.path.join(MODULE_ROOT, "views", "scan_views.xml"), encoding="utf-8") as handle:
            xml = handle.read()
        self.assertEqual(re.findall(r"decoration-\w+\s*=|widget=\"badge\"", xml), [])


@tagged("post_install", "-at_install", "odexalabs_mrs", "odexalabs_mrs_claims")
class TestClaimListing(TransactionCase):
    """Claims the App Store page and the user guide make, one test each."""

    def scan(self):
        record = self.env["odexalabs.migration.scan"].create({"target_version": NEWEST})
        record.action_run_scan()
        flush_all(self.env)
        return record

    def _some_installed_standard(self):
        current = "%s.%s" % tuple(__import__("odoo").release.version_info[:2])
        listed = runner.load_standard_modules()[current]
        module = self.env["ir.module.module"].search(
            [("state", "=", "installed"), ("name", "in", list(listed)), ("name", "!=", "base")], limit=1)
        self.assertTrue(module)
        return module, current

    def _this_module(self):
        return self.env["ir.module.module"].search([("name", "=", "odexalabs_migration_readiness")])

    def test_pending_upgrade_is_listed(self):
        module = self._this_module()
        self.env.cr.execute("UPDATE ir_module_module SET latest_version = %s WHERE id = %s",
                            ("0.0.0.1", module.id))
        invalidate_all(self.env)
        row = self.scan().module_ids.filtered(lambda m: m.name == module.name)
        self.assertIn("an upgrade of this module is pending", row.listed_because)

    def test_removed_odoo_dependency_is_named_on_the_module_using_it(self):
        current = "%s.%s" % tuple(__import__("odoo").release.version_info[:2])
        target = "20.0" if current == "19.0" else "19.0"
        lists = runner.load_standard_modules()
        lists[target] = {k: v for k, v in lists[target].items() if k != "base"}
        with patch.object(runner, "load_standard_modules", return_value=lists):
            scan = self.env["odexalabs.migration.scan"].create({"target_version": target})
            scan.action_run_scan()
            flush_all(self.env)
        row = scan.module_ids.filtered(lambda m: m.name == "odexalabs_migration_readiness")
        self.assertIn("depends on base, which does not exist in Odoo %s" % target, row.target_action)

    def test_declared_odoo_author_is_not_listed(self):
        module = self._this_module()
        self.env.cr.execute("UPDATE ir_module_module SET author = 'Odoo S.A.' WHERE id = %s", (module.id,))
        invalidate_all(self.env)
        scan = self.scan()
        self.assertFalse(scan.module_ids.filtered(lambda m: m.name == module.name),
                         "a module whose manifest declares an author is classified by it")

    def test_enterprise_module_is_not_listed(self):
        module, current = self._some_installed_standard()
        lists = runner.load_standard_modules()
        for series in lists:
            lists[series] = {k: v for k, v in lists[series].items() if k != module.name}
        self.env.cr.execute("UPDATE ir_module_module SET license = 'OEEL-1' WHERE id = %s", (module.id,))
        invalidate_all(self.env)
        with patch.object(runner, "load_standard_modules", return_value=lists):
            scan = self.scan()
        self.assertFalse(scan.module_ids.filtered(lambda m: m.name == module.name),
                         "an Enterprise module is Odoo's own and is not listed")

    def test_enterprise_views_are_not_listed_as_custom(self):
        """An Enterprise module's views inheriting standard views are Odoo's
        own, not customisations, although Enterprise names are not vendored."""
        current = "%s.%s" % tuple(__import__("odoo").release.version_info[:2])
        listed = runner.load_standard_modules()[current]
        self.env.cr.execute("""
            SELECT d.module FROM ir_ui_view v
              JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
              JOIN ir_ui_view p ON p.id = v.inherit_id
              JOIN ir_model_data pd ON pd.model = 'ir.ui.view' AND pd.res_id = p.id
             WHERE d.module <> pd.module AND d.module = ANY(%s) AND pd.module = ANY(%s)
             LIMIT 1""", (list(listed), list(listed)))
        found = self.env.cr.fetchone()
        if not found:
            self.skipTest("no standard module here inherits another module's view")
        module = found[0]
        lists = runner.load_standard_modules()
        lists[current] = {k: v for k, v in lists[current].items() if k != module}
        self.env.cr.execute("UPDATE ir_module_module SET license = 'OEEL-1' WHERE name = %s", (module,))
        invalidate_all(self.env)
        with patch.object(runner, "load_standard_modules", return_value=lists):
            scan = self.scan()
        self.assertFalse(scan.view_ids.filtered(lambda r: r.module == module),
                         "%s is Odoo Enterprise by its licence; its views are not customisations" % module)

    def test_only_administrators_can_read_scans(self):
        Users = self.env["res.users"]
        groups_field = "group_ids" if "group_ids" in Users._fields else "groups" + "_id"
        user = Users.create({
            "name": "mrs plain user", "login": "mrs_plain_user",
            groups_field: [(6, 0, [self.env.ref("base.group_user").id])],
        })
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.env["odexalabs.migration.scan"].with_user(user).search([])

    def test_depends_only_on_base(self):
        with open(os.path.join(MODULE_ROOT, "__manifest__.py"), encoding="utf-8") as handle:
            manifest = ast.literal_eval(handle.read())
        self.assertEqual(manifest["depends"], ["base"])
        self.assertNotIn("external_dependencies", manifest)
        self.assertNotIn("price", manifest)

    def test_targets_come_from_the_published_versions(self):
        current = int(__import__("odoo").release.version_info[0])
        selection = [key for key, _label in self.env["odexalabs.migration.scan"]._selection_target_version()]
        self.assertEqual(selection, [e["version"] for e in runner.load_support_matrix()
                                     if int(e["version"].split(".")[0]) > current])

    def test_every_store_image_exists(self):
        folder = os.path.join(MODULE_ROOT, "static", "description")
        with open(os.path.join(folder, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        missing = [src for src in re.findall(r'src="([^"]+)"', html) if not os.path.isfile(os.path.join(folder, src))]
        self.assertEqual(missing, [])
        with open(os.path.join(MODULE_ROOT, "__manifest__.py"), encoding="utf-8") as handle:
            manifest = ast.literal_eval(handle.read())
        for image in manifest.get("images", []):
            self.assertTrue(os.path.isfile(os.path.join(MODULE_ROOT, image)), image)
        self.assertTrue(os.path.isfile(os.path.join(folder, "icon.png")))
