# -*- coding: utf-8 -*-
"""Normal tests: a real scan inside a test transaction.

Planted conditions are built on throwaway tables created inside the test's
own transaction, so they are rolled back with it: the tests need no
superuser and leave nothing behind.
"""

import re

from odoo.tests import TransactionCase, tagged

from ..lib import runner
from ..models.assessment_items import items_of_kind
from ..models.migration_scan import flush_all

CHECK_IDS = ["MIG-001", "MRS-001", "MRS-002", "MRS-003", "MRS-004", "MRS-005"]
NEWEST = max((e["version"] for e in runner.load_support_matrix()), key=lambda v: int(v.split(".")[0]))
LIVE_HANDBOOK = (
    "https://odexalabs.com/odoo-handbook/upgrading-safely",
    "https://odexalabs.com/odoo-handbook/postgresql-for-odoo",
    "https://odexalabs.com/odoo-handbook/sizing-workers-containers",
)


class ScanCase(TransactionCase):
    def scan(self, target=None, **vals):
        matrix = runner.load_support_matrix()
        versions = sorted(e["version"] for e in matrix)
        vals.setdefault("target_version", target or versions[-1])
        record = self.env["odexalabs.migration.scan"].create(vals)
        record.action_run_scan()
        flush_all(self.env)
        return record

    def check(self, scan, check_id):
        found = scan.check_finding_ids.filtered(lambda f: f.check_id == check_id)
        self.assertEqual(len(found), 1, check_id)
        return found


@tagged("post_install", "-at_install", "odexalabs_mrs")
class TestScan(ScanCase):
    def test_every_check_reports(self):
        scan = self.scan()
        self.assertEqual(scan.state, "done")
        self.assertEqual(sorted(scan.check_finding_ids.mapped("check_id")), CHECK_IDS)
        for finding in scan.check_finding_ids:
            self.assertIn(finding.status, ("fired", "clear", "skipped"))
            self.assertTrue(finding.explanation and finding.what_to_do and finding.technical_detail)
            if finding.status == "skipped":
                self.assertTrue(finding.skip_reason)

    def test_handbook_links_are_live_chapters_only(self):
        scan = self.scan()
        for finding in scan.finding_ids:
            if finding.handbook_url:
                self.assertTrue(finding.handbook_url.startswith(LIVE_HANDBOOK), finding.handbook_url)

    def test_every_inventory_reports(self):
        scan = self.scan()
        self.assertGreater(scan.installed_module_count, 0)
        self.assertGreaterEqual(scan.installed_module_count, scan.third_party_module_count)
        self.assertTrue(scan.inv_code_summary)
        self.assertTrue(scan.table_ids, "a database always has tables")
        self.assertTrue(scan.int4_ids, "Odoo's own tables use int4 ids")
        self.assertTrue(all(r.record_type for r in scan.int4_ids))

    def test_no_internal_ids_in_what_users_read(self):
        scan = self.scan()
        texts = []
        for f in scan.finding_ids:
            texts += [f.name or "", f.explanation or "", f.what_to_do or "", f.evidence_summary or ""]
        texts += [scan.readiness_summary or "", scan.inv_code_summary or "", scan.human_lead or ""]
        self.assertFalse([t for t in texts if re.search(r"\b(MIG|MRS|INV)-\d{3}\b", t)])

    def test_current_version_support_is_checked(self):
        finding = self.check(self.scan(), "MRS-005")
        self.assertIn(finding.status, ("fired", "clear"))
        self.assertRegex(finding.evidence_detail, r"end of support [A-Z][a-z]+ \d{4}")

    def test_tables_ranked_by_size(self):
        sizes = self.scan().table_ids.sorted("sequence").mapped("total_bytes")
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_int4_ranked_by_share(self):
        shares = self.scan().int4_ids.sorted("sequence").mapped("share_used")
        self.assertEqual(shares, sorted(shares, reverse=True))

    def test_older_or_same_target_is_refused(self):
        from odoo.exceptions import ValidationError
        current = "%s.%s" % tuple(__import__("odoo").release.version_info[:2])
        older = [e["version"] for e in runner.load_support_matrix()
                 if int(e["version"].split(".")[0]) <= int(current.split(".")[0])]
        self.assertIn(current, older)
        for version in older:
            refused = False
            try:
                with self.env.cr.savepoint():
                    self.env["odexalabs.migration.scan"].create({"target_version": version})
            except (ValidationError, ValueError):
                refused = True
            self.assertTrue(refused, "target %s was accepted" % version)

    def test_only_newer_targets_are_offered(self):
        current = int(__import__("odoo").release.version_info[0])
        offered = [k for k, _l in self.env["odexalabs.migration.scan"]._selection_target_version()]
        self.assertTrue(offered)
        self.assertTrue(all(int(v.split(".")[0]) > current for v in offered), offered)

    def test_app_icon_on_home_menu(self):
        root = self.env.ref("odexalabs_migration_readiness.odexalabs_migration_readiness_menu_root")
        self.assertEqual(root.web_icon, "odexalabs_migration_readiness,static/description/icon.png")
        self.assertTrue(root.web_icon_data, "the icon file was not loaded into the menu")

    def test_mig001_shows_month_and_window(self):
        detail = self.check(self.scan(), "MIG-001").evidence_detail
        self.assertRegex(detail, r"end of support [A-Z][a-z]+ \d{4}")
        self.assertIn("%d months" % runner.ENDING_SOON_MONTHS, detail)
        self.assertNotRegex(detail, r"\d{4}-\d{2}-\d{2}", "never the placeholder day")

    def test_mrs001_states_the_pins_as_a_separate_fact(self):
        current = int(__import__("odoo").release.version_info[0])
        with_pins = [e["version"] for e in runner.load_support_matrix()
                     if e.get("python_310_pins") and int(e["version"].split(".")[0]) > current]
        if not with_pins:
            self.skipTest("no newer target pins Python 3.10 requirements")
        detail = self.check(self.scan(target=with_pins[0]), "MRS-001").evidence_detail
        self.assertIn("gevent==", detail)
        self.assertIn("greenlet==", detail)
        self.assertIn("(line ", detail)

    def test_mrs002_never_says_will_not_start(self):
        finding = self.check(self.scan(), "MRS-002")
        text = " ".join(filter(None, [finding.explanation, finding.what_to_do, finding.technical_detail]))
        self.assertNotIn("will not start", text.lower())
        self.assertIn("below the stated minimum", text)

    def test_mrs003_names_sequence_max_and_collision(self):
        cr = self.env.cr
        cr.execute("CREATE TABLE odexalabs_mrs_t_seq (id serial PRIMARY KEY)")
        cr.execute("INSERT INTO odexalabs_mrs_t_seq (id) SELECT generate_series(1, 10)")
        cr.execute("SELECT setval('odexalabs_mrs_t_seq_id_seq', 3)")
        cr.execute("CREATE TABLE odexalabs_mrs_t_gap (id serial PRIMARY KEY)")
        cr.execute("INSERT INTO odexalabs_mrs_t_gap (id) VALUES (1), (2), (3), (10)")
        cr.execute("SELECT setval('odexalabs_mrs_t_gap_id_seq', 3)")
        finding = self.check(self.scan(), "MRS-003")
        self.assertEqual(finding.status, "fired")
        self.assertIn("odexalabs_mrs_t_seq_id_seq (odexalabs_mrs_t_seq.id) issues 4 next; MAX(id) is 10.",
                      finding.evidence_detail)
        self.assertIn("The next insert collides: id 4 is already in use.", finding.evidence_detail)
        self.assertIn("Ids 4 to 9 are free, so the next 6 inserts succeed and the one after collides "
                      "with id 10.", finding.evidence_detail)

    def test_mrs004_counts_orphans(self):
        cr = self.env.cr
        cr.execute("CREATE TABLE odexalabs_mrs_t_parent (id int PRIMARY KEY)")
        cr.execute("CREATE TABLE odexalabs_mrs_t_child (id serial PRIMARY KEY, parent_id int)")
        cr.execute("INSERT INTO odexalabs_mrs_t_parent VALUES (1), (2)")
        cr.execute("INSERT INTO odexalabs_mrs_t_child (parent_id) VALUES (1), (2), (7), (8), (9), (NULL)")
        cr.execute("ALTER TABLE odexalabs_mrs_t_child ADD CONSTRAINT odexalabs_mrs_t_child_fk "
                   "FOREIGN KEY (parent_id) REFERENCES odexalabs_mrs_t_parent (id) NOT VALID")
        finding = self.check(self.scan(), "MRS-004")
        self.assertEqual(finding.status, "fired")
        self.assertIn("odexalabs_mrs_t_child.parent_id → odexalabs_mrs_t_parent.id "
                      "(constraint odexalabs_mrs_t_child_fk): 3 rows reference a missing row.",
                      finding.evidence_detail)

    def test_inv001_lists_this_module_with_signals(self):
        scan = self.scan()
        mine = scan.module_ids.filtered(lambda m: m.name == "odexalabs_migration_readiness")
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine.origin, "third_party")
        self.assertIn("named author: Odexalabs", mine.origin_signals)
        for row in scan.module_ids:
            self.assertTrue(row.origin_signals, "%s has no signals" % row.name)
            self.assertTrue(row.target_action)
            self.assertNotIn(row.origin, ("odoo_standard", "odoo_enterprise"),
                             "Odoo's own modules are not listed unless left half-way")

    def test_inv001_lists_a_stuck_module(self):
        module = self.env["ir.module.module"].search([("name", "=", "base_import_module")])
        if not module:
            self.skipTest("base_import_module is not in this module list")
        module.state = "to upgrade"
        row = self.scan().module_ids.filtered(lambda m: m.name == "base_import_module")
        self.assertIn('Left in state "to upgrade"', row.listed_because)

    def test_inv004_finds_a_manual_field(self):
        model = self.env["ir.model"]._get("res.partner")
        self.env["ir.model.fields"].create({
            "name": "x_mrs_test_field", "model_id": model.id, "ttype": "char", "state": "manual",
        })
        rows = self.scan().studio_ids.filtered(lambda r: r.name == "x_mrs_test_field")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows.kind, rows.model), ("field", "res.partner"))
        self.assertTrue(rows.record_type)

    def test_inv005_finds_a_database_view_inheriting_standard(self):
        parent = self.env.ref("base.view_partner_form")
        self.env["ir.ui.view"].create({
            "name": "mrs test inheriting view", "model": "res.partner", "inherit_id": parent.id,
            "arch": '<xpath expr="//field[@name=\'name\']" position="after"/>',
        })
        rows = self.scan().view_ids.filtered(lambda r: r.view_name == "mrs test inheriting view")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.inherited_xmlid, "base.view_partner_form")

    def test_inv006_caveat_always_shown(self):
        self.assertIn("does not prove the code is clean", self.scan().inv_code_summary)

    def test_needs_review_section(self):
        scan = self.scan()
        self.assertEqual(len(scan.assessment_finding_ids), len(items_of_kind("assessment")))
        self.assertFalse(scan.assessment_finding_ids.filtered("status"))
        self.assertTrue(scan.human_lead.startswith(str(len(items_of_kind("assessment")))))
        self.assertFalse(scan.finding_ids.filtered(lambda f: f.kind == "deeper"),
                         "the paid-assessment section is no longer produced")

    def test_no_paid_assessment_text(self):
        scan = self.scan()
        texts = " ".join(filter(None, sum(([f.name, f.explanation, f.what_to_do] for f in scan.finding_ids), [])))
        self.assertNotIn("$", texts)
        self.assertNotIn("assessment", (scan.human_lead or "").lower())

    def test_rescan_replaces_rows(self):
        scan = self.scan()
        first = len(scan.finding_ids), len(scan.table_ids)
        scan.action_run_scan()
        flush_all(self.env)
        self.assertEqual((len(scan.finding_ids), len(scan.table_ids)), first)


@tagged("post_install", "-at_install", "odexalabs_mrs")
class TestScore(ScanCase):
    def test_all_skipped_is_not_100(self):
        scan = self.env["odexalabs.migration.scan"].create({"target_version": NEWEST})
        scan._set_readiness([{"status": "skipped"}] * len(CHECK_IDS))
        self.assertEqual(scan.readiness_score, 0)
        self.assertIn("%d could not be determined" % len(CHECK_IDS), scan.readiness_summary)

    def test_skipped_never_counts_as_pass(self):
        scan = self.scan(probe_budget_seconds=0)
        mrs3, mrs4 = self.check(scan, "MRS-003"), self.check(scan, "MRS-004")
        self.assertEqual(mrs3.status, "skipped")
        self.assertEqual(mrs4.status, "skipped")
        self.assertIn("could not be checked", mrs4.skip_reason)
        self.assertIn("not probed", mrs4.evidence_detail)
        passed = len(scan.check_finding_ids.filtered(lambda f: f.status == "clear"))
        self.assertEqual(scan.readiness_score, round(100.0 * passed / len(CHECK_IDS)))
        self.assertLess(scan.readiness_score, 100)
