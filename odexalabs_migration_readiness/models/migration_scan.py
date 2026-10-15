# -*- coding: utf-8 -*-
import json
import os
import platform
import sys
from collections import Counter, defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.release import version_info

from ..lib import origin as origin_lib
from ..lib import pg_probes, runner, source_scan
from .assessment_items import items_of_kind

PROBE_TIMEOUT_MS = 5000

_HANDBOOK = "https://odexalabs.com/odoo-handbook/upgrading-safely"
_CHECK_HANDBOOK = {"MIG-001": _HANDBOOK, "MIG-002": _HANDBOOK, "MRS-005": _HANDBOOK}
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

_OWN_TABLE_PREFIX = "odexalabs_migration_"
_INSTALLED = ("installed", "to upgrade", "to remove")
_STUCK = ("to install", "to upgrade", "to remove")
_ANALYTIC_PLAN_RELATIONS = ("account.analytic.account", "account.analytic.plan")
_INHERITING_VIEWS = """
WITH xid AS (
    SELECT DISTINCT ON (res_id) res_id, module, name
      FROM ir_model_data
     WHERE model = 'ir.ui.view'
     ORDER BY res_id, id
)
SELECT v.name, v.model, v.active, cx.module, cx.name, px.module, px.name
  FROM ir_ui_view v
  JOIN ir_ui_view p ON p.id = v.inherit_id
  LEFT JOIN xid cx ON cx.res_id = v.id
  LEFT JOIN xid px ON px.res_id = p.id
 WHERE {website_filter}
 ORDER BY v.id
"""
_DEPENDENCIES = """
SELECT d.name, m.name
  FROM ir_module_module_dependency d
  JOIN ir_module_module m ON m.id = d.module_id
 WHERE m.state IN %s
 ORDER BY d.name, m.name
"""
_EVIDENCE_LABELS = {
    "odoo_version": "Current version",
    "target_version": "Target version",
    "target_version_status": "Target support",
    "current_version_status": "Current version support",
    "python_version": "Python on this server",
    "target_python_min": "Target's minimum Python",
    "postgres_version": "PostgreSQL on this server",
    "target_postgres_min": "Target's minimum PostgreSQL",
    "sequences_checked": "Sequences checked",
    "sequences_behind": "behind",
    "sequences_not_checked": "not checked",
    "foreign_keys_checked": "Foreign keys checked",
    "foreign_keys_violated": "with orphan rows",
    "foreign_keys_not_checked": "not checked",
}
_STATUS_WORDS = {"supported": "supported", "ending_soon": "ending soon", "unsupported": "past end of support"}


def _major(version):
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return 0


_SKIPPED_VENDORED = {"MIG-002"}


def flush_all(env):
    """Write pending changes to the database: env.flush_all() from Odoo 16.0;
    on 15.0, a model's flush() without arguments flushes every model."""
    if hasattr(env, "flush_all"):
        env.flush_all()
    else:
        env["base"].flush()


def invalidate_all(env):
    """Flush, then drop every cached value: env.invalidate_all() from Odoo 16.0;
    on 15.0, flush() then invalidate_cache() without arguments."""
    if hasattr(env, "invalidate_all"):
        env.invalidate_all()
    else:
        env["base"].flush()
        env["base"].invalidate_cache()


def _n(value):
    return "{:,}".format(value)


def _lines(text):
    return len([line for line in (text or "").splitlines() if line.strip()])


def _load_check_text():
    """Module-side wording for the vendored checks (their conditions are not touched)."""
    with open(os.path.join(_DATA_DIR, "check_text.json"), encoding="utf-8") as handle:
        return json.load(handle)


class MigrationScan(models.Model):
    _name = "odexalabs.migration.scan"
    _description = "Upgrade Readiness Scan"
    _order = "scan_date desc, id desc"
    _rec_name = "display_title"

    display_title = fields.Char(compute="_compute_display_title", store=True)
    scan_date = fields.Datetime(default=fields.Datetime.now, readonly=True)

    current_version = fields.Char(readonly=True)
    target_version = fields.Selection(
        selection="_selection_target_version",
        required=True,
        help="The Odoo version you intend to migrate to.",
    )
    python_version = fields.Char("Python on this server", readonly=True)
    postgres_version = fields.Char("PostgreSQL on this server", readonly=True)
    probe_budget_seconds = fields.Integer(
        "Time limit for the database checks (seconds)",
        default=60,
        help="Total time the scan may spend on the sequence and foreign-key checks (%d ms at "
        "most per table). Anything the limit does not reach is listed as not checked, never as "
        "clear. In a multi-worker server a request is stopped after limit_time_real seconds, so "
        "keep this below that." % PROBE_TIMEOUT_MS,
    )

    state = fields.Selection(
        [("draft", "Draft"), ("done", "Scanned")], default="draft", readonly=True
    )

    finding_ids = fields.One2many("odexalabs.migration.finding", "scan_id")
    check_finding_ids = fields.One2many(
        "odexalabs.migration.finding",
        "scan_id",
        domain=[("kind", "=", "check")],
        string="Check results",
    )
    assessment_finding_ids = fields.One2many(
        "odexalabs.migration.finding",
        "scan_id",
        domain=[("kind", "=", "assessment")],
        string="Needs review",
    )

    module_ids = fields.One2many("odexalabs.migration.module", "scan_id", string="Modules")
    int4_ids = fields.One2many("odexalabs.migration.int4", "scan_id", string="Record ID capacity")
    table_ids = fields.One2many("odexalabs.migration.table", "scan_id", string="Largest tables")
    studio_ids = fields.One2many("odexalabs.migration.studio", "scan_id", string="Customisations")
    view_ids = fields.One2many("odexalabs.migration.view", "scan_id", string="Changed standard views")
    code_hit_ids = fields.One2many("odexalabs.migration.code.hit", "scan_id", string="Code to update")

    installed_module_count = fields.Integer("Installed modules", readonly=True)
    third_party_module_count = fields.Integer("Not from Odoo", readonly=True)
    customisation_count = fields.Integer("Number of customisations", readonly=True)
    inv_code_summary = fields.Text(readonly=True)

    readiness_score = fields.Integer(readonly=True)
    readiness_summary = fields.Char(readonly=True)

    human_lead = fields.Text(compute="_compute_section_leads")


    @api.model
    def _selection_target_version(self):
        """Only versions newer than the one this database runs: a migration
        moves forward, so an older or equal target is never offered."""
        current = int(version_info[0])
        return [
            (entry["version"], "Odoo %s" % entry["version"])
            for entry in runner.load_support_matrix()
            if _major(entry["version"]) > current
        ]

    @api.constrains("target_version")
    def _check_target_version(self):
        for scan in self:
            if scan.target_version and _major(scan.target_version) <= int(version_info[0]):
                raise ValidationError(_(
                    "The target must be newer than the version this database runs (Odoo %s)."
                ) % self._current_series())

    @api.depends("target_version", "scan_date")
    def _compute_display_title(self):
        for scan in self:
            if scan.target_version:
                scan.display_title = _("Upgrade readiness → Odoo %s") % scan.target_version
            else:
                scan.display_title = _("Upgrade readiness scan")

    def _compute_section_leads(self):
        count = len(items_of_kind("assessment"))
        for scan in self:
            scan.human_lead = _(
                "%(n)s questions that need someone to look at them: no scan, this one "
                "included, can answer them from the database."
            ) % {"n": count}


    def _current_series(self):
        """The running Odoo series, e.g. "18.0"."""
        return "%s.%s" % (version_info[0], version_info[1])

    def _build_row(self, matrix, current, target, entry, pg_num):
        """The row of facts the checks evaluate against, and what is untrusted.

        `untrusted` maps a field to the reason it could not be established; a
        check reading it reports "could not determine", never a confident pass.
        """
        status = runner.support_status(entry) if entry else None
        current_entry = runner.matrix_entry(current, matrix)
        current_status = runner.support_status(current_entry) if current_entry else None
        try:
            current_major = int(current.split(".")[0])
            target_major = int(target.split(".")[0]) if target else None
        except (ValueError, AttributeError):
            current_major = target_major = None

        row = {
            "database_name": self.env.cr.dbname,
            "odoo_version": current,
            "target_version": target,
            "target_version_major": target_major,
            "target_version_status": status,
            "target_version_supported": None if status is None else status == "supported",
            "current_version_status": current_status,
            "current_version_supported": None if current_status is None else current_status == "supported",
            "target_is_newer_than_current": (
                None
                if (target_major is None or current_major is None)
                else target_major > current_major
            ),
            "python_version": platform.python_version(),
            "target_python_min": (entry or {}).get("python_min"),
            "postgres_version": None,
            "target_postgres_min": (entry or {}).get("postgres_min"),
        }
        untrusted = {}
        if target is None:
            for name in (
                "target_version",
                "target_version_major",
                "target_version_status",
                "target_version_supported",
                "target_is_newer_than_current",
            ):
                untrusted[name] = _("no migration target was stated")
        if current_status is None:
            untrusted["current_version_supported"] = _(
                "no support dates are recorded for Odoo %s"
            ) % current

        py_min = row["target_python_min"]
        if py_min:
            wanted = tuple(int(part) for part in py_min.split("."))
            row["python_below_target_min"] = tuple(sys.version_info[: len(wanted)]) < wanted
        else:
            row["python_below_target_min"] = None
            untrusted["python_below_target_min"] = _("no Python minimum is recorded for the target")

        pg_min = row["target_postgres_min"]
        if pg_num and pg_min:
            row["postgres_below_target_min"] = pg_num < int(pg_min) * 10000
        else:
            row["postgres_below_target_min"] = None
            untrusted["postgres_below_target_min"] = (
                _("no PostgreSQL minimum is recorded for the target")
                if not pg_min
                else _("the PostgreSQL server version could not be read")
            )
        return row, untrusted

    def _mrs_state(self, row, untrusted, field, summary, prefix, found_key, noun):
        """True if any item is affected; None (with the reason) if none is but
        some could not be checked; False only when every item was checked."""
        if summary[found_key]:
            row[field] = True
        elif summary[prefix + "_not_checked"]:
            row[field] = None
            untrusted[field] = _(
                "%(n)s %(noun)s could not be checked (listed in the detail with the reason); "
                "none of the %(checked)s that were checked is affected"
            ) % {
                "n": summary[prefix + "_not_checked"],
                "noun": noun,
                "checked": summary[prefix + "_checked"],
            }
        else:
            row[field] = False

    def _record_types(self):
        """{table: (model, description)} for every model with a table."""
        out = {}
        for name in self.env.registry.models:
            model = self.env[name]
            if model._abstract or not model._auto:
                continue
            out.setdefault(model._table, (name, model._description or name))
        return out


    def action_run_scan(self):
        self.ensure_one()
        if not self.target_version:
            raise UserError(_("Select the Odoo version you are migrating to first."))
        if _major(self.target_version) <= int(version_info[0]):
            raise UserError(_(
                "Choose a target newer than the version this database runs (Odoo %s)."
            ) % self._current_series())
        self.finding_ids.unlink()
        for field in ("module_ids", "int4_ids", "table_ids", "studio_ids", "view_ids", "code_hit_ids"):
            self[field].unlink()
        flush_all(self.env)

        facts = self._collect_facts()
        self._store_results(facts)
        return True

    def _collect_facts(self):
        """Everything the scan reads. Raw SQL is read-only and bounded; this
        method creates no record, so the probes' savepoint brackets can never
        roll back anything of the scan's own."""
        cr = self.env.cr
        matrix = runner.load_support_matrix()
        standard = runner.load_standard_modules()
        current = self._current_series()
        target = runner.normalize_version(self.target_version, matrix)
        entry = runner.matrix_entry(target, matrix) if target else None
        clock = pg_probes.ProbeClock(self.probe_budget_seconds, PROBE_TIMEOUT_MS)

        try:
            pg_num, pg_text = pg_probes.server_version(cr)
        except Exception:
            pg_num, pg_text = None, None
        row, untrusted = self._build_row(matrix, current, target, entry, pg_num)
        row["postgres_version"] = pg_text

        seq_catalog, seq_reason = pg_probes.sequence_catalog(cr)
        for seq in seq_catalog:
            pg_probes.read_sequence_position(cr, seq)
        seq_rows, seq_summary = pg_probes.sequence_behind_facts(cr, clock, seq_catalog)
        fk_catalog, fk_reason = pg_probes.foreign_key_catalog(cr)
        fk_rows, fk_summary = pg_probes.foreign_key_violation_facts(cr, clock, fk_catalog)
        row.update(seq_summary)
        row.update(fk_summary)

        if seq_reason:
            row["sequence_behind_max_found"] = None
            untrusted["sequence_behind_max_found"] = seq_reason
        else:
            self._mrs_state(row, untrusted, "sequence_behind_max_found", seq_summary,
                            "sequences", "sequences_behind", _("sequences"))
        if fk_reason:
            row["fk_violation_found"] = None
            untrusted["fk_violation_found"] = fk_reason
        else:
            self._mrs_state(row, untrusted, "fk_violation_found", fk_summary,
                            "foreign_keys", "foreign_keys_violated", _("foreign keys"))

        record_types = self._record_types()
        modules, counts, code_targets, odoo_modules = self._inventory_modules(current, target, standard)
        hits, code_summary = self._inventory_code(current, target, code_targets)
        tables, _table_reason = pg_probes.largest_tables(cr)

        return {
            "current": current,
            "target": target,
            "row": row,
            "untrusted": untrusted,
            "details": {
                "MIG-001": self._detail_support(target, entry),
                "MRS-005": self._detail_support(current, runner.matrix_entry(current, matrix)),
                "MRS-001": self._detail_python(target, entry),
                "MRS-003": self._detail_sequences(seq_rows, seq_reason),
                "MRS-004": self._detail_foreign_keys(fk_rows, fk_reason),
            },
            "modules": modules,
            "module_counts": counts,
            "int4": pg_probes.int4_id_columns(cr, seq_catalog),
            "tables": tables,
            "record_types": record_types,
            "studio": self._inventory_studio(record_types),
            "views": self._inventory_views(odoo_modules),
            "hits": hits,
            "code_summary": code_summary,
        }


    def _detail_support(self, series, entry):
        if not entry:
            return False
        return _(
            "Odoo %(series)s: end of support %(month)s (Odoo publishes the month only). "
            "Status at this scan: %(status)s. \"Ending soon\" means from %(window)s months "
            "before the end month through that month; \"past end of support\" starts the "
            "month after."
        ) % {
            "series": series,
            "month": runner.support_end_month(entry) or _("not recorded"),
            "status": _STATUS_WORDS.get(runner.support_status(entry), _("not determined")),
            "window": runner.ENDING_SOON_MONTHS,
        }

    def _detail_python(self, target, entry):
        lines = [
            _("This server: Python %(running)s. Odoo %(target)s's stated minimum: %(minimum)s.")
            % {
                "running": platform.python_version(),
                "target": target,
                "minimum": (entry or {}).get("python_min") or _("not recorded"),
            }
        ]
        pins = (entry or {}).get("python_310_pins") or []
        if pins:
            lines.append(
                _("Separate fact: meeting the minimum is not the same as Odoo's pinned "
                  "requirements installing: Odoo %(target)s's requirements.txt pins, for "
                  "Python 3.10 exactly, %(pins)s. Those pins apply only on Python 3.10; "
                  "check that they build on your server before fixing a date.")
                % {
                    "target": target,
                    "pins": ", ".join(
                        "%s==%s (line %s)" % (p["package"], p["version"], p["line"]) for p in pins
                    ),
                }
            )
        return "\n".join(lines)

    def _detail_sequences(self, rows, reason):
        if reason:
            return _("Not checked: %s.") % reason
        lines = []
        for seq in rows:
            if seq["state"] != "behind":
                continue
            where = "%s.%s" % (seq["table"], seq["column"])
            head = _("%(seq)s (%(where)s) issues %(next)s next; MAX(%(col)s) is %(max)s.") % {
                "seq": seq["sequence"],
                "where": where,
                "next": _n(seq["next_value"]),
                "col": seq["column"],
                "max": _n(seq["max_value"]),
            }
            if seq.get("first_collision") is None:
                tail = _("The first id at or above it that is already in use could not be looked up.")
            elif seq["free_before_collision"] == 0:
                tail = _("The next insert collides: id %s is already in use.") % _n(seq["first_collision"])
            else:
                tail = _(
                    "Ids %(a)s to %(b)s are free, so the next %(free)s inserts succeed and the "
                    "one after collides with id %(first)s."
                ) % {
                    "a": _n(seq["next_value"]),
                    "b": _n(seq["first_collision"] - 1),
                    "free": _n(seq["free_before_collision"]),
                    "first": _n(seq["first_collision"]),
                }
            lines.append("%s %s" % (head, tail))
        for seq in rows:
            if seq["state"] == "not_checked":
                lines.append(_("Not checked: %(seq)s (%(table)s.%(col)s). Reason: %(reason)s.") % {
                    "seq": seq["sequence"], "table": seq["table"], "col": seq["column"], "reason": seq["reason"],
                })
        return "\n".join(lines) or False

    def _detail_foreign_keys(self, rows, reason):
        if reason:
            return _("Not checked: %s.") % reason
        lines = []
        for fk in rows:
            if fk["state"] != "violated":
                continue
            count = (
                _("%s rows") % _n(fk["count"]) if "count" in fk
                else _("at least one row (%s)") % fk.get("count_reason", "")
            )
            lines.append(_("%(table)s.%(cols)s → %(ref)s.%(refcols)s (constraint %(name)s): %(count)s "
                           "reference a missing row.") % {
                "table": fk["table"], "cols": ", ".join(fk["columns"]),
                "ref": fk["ref_table"], "refcols": ", ".join(fk["ref_columns"]),
                "name": fk["constraint"], "count": count,
            })
        for fk in rows:
            if fk["state"] == "not_checked":
                lines.append(_("Not checked: %(name)s on %(table)s. Reason: %(reason)s.") % {
                    "name": fk["constraint"], "table": fk["table"], "reason": fk["reason"],
                })
        return "\n".join(lines) or False

    def _evidence_text(self, evidence):
        parts = []
        for key, value in evidence.items():
            label = _EVIDENCE_LABELS.get(key)
            if not label:
                continue
            if key.endswith("_status"):
                value = _STATUS_WORDS.get(value, value)
            known = value is not None and value is not False
            parts.append("%s: %s" % (label, value if known else _("not known")))
        return " · ".join(parts)


    def _inventory_modules(self, current, target, standard):
        """Modules not from Odoo. Returns (rows, counts, code_targets, odoo_modules).

        A module is Odoo's own when its name is in the vendored Community list,
        when the database records the OEEL-1 (Enterprise) licence for it, or
        when its declared author is Odoo S.A. Odoo's own modules are not listed;
        they move with the upgrade. They are listed only when left half-way
        through an install, upgrade or removal.
        """
        cr = self.env.cr
        Module = self.env["ir.module.module"].sudo()
        has_imported = "imported" in Module.fields_get(allfields=["imported"])
        std_cur = standard.get(current) or {}
        std_tgt = standard.get(target)

        companies = self.env["res.company"].sudo().search_read([], ["name", "website", "email"])
        company_names = [c["name"] for c in companies if c.get("name")]
        company_domains = set()
        for company in companies:
            for value in (company.get("website"), company.get("email")):
                domain = origin_lib.domain_of(value or "")
                if domain:
                    company_domains.add(origin_lib.strip_www(domain))

        cr.execute(_DEPENDENCIES, (_INSTALLED,))
        dependents = defaultdict(list)
        depends_on = defaultdict(list)
        for dep_name, mod_name in cr.fetchall():
            dependents[dep_name].append(mod_name)
            depends_on[mod_name].append(dep_name)

        modules = Module.search(
            [("state", "in", list(set(_INSTALLED + _STUCK)))], order="name"
        )
        odoo_dirs = {}

        def in_odoo_directory(path):
            """True when the module sits among Odoo's own modules: a sibling
            directory holds a module from Odoo's list or one licensed OEEL-1."""
            parent = os.path.dirname(path)
            if parent not in odoo_dirs:
                found = False
                try:
                    siblings = sorted(os.listdir(parent))
                except OSError:
                    siblings = []
                for sibling in siblings:
                    if sibling in std_cur:
                        found = True
                        break
                    manifest = source_scan.read_raw_manifest(os.path.join(parent, sibling))
                    if manifest and manifest.get("license") == "OEEL-1":
                        found = True
                        break
                odoo_dirs[parent] = found
            return odoo_dirs[parent]

        rows, code_targets, odoo_modules = [], [], set()
        for module in modules:
            name = module.name
            author = (module.author or "").strip()
            path = get_module_path(name, display_warning=False) or None
            manifest = source_scan.read_raw_manifest(path) if path else None
            defaulted_author = bool(manifest is not None and not manifest.get("author"))
            odoo_author = bool(origin_lib._ODOO_SA_AUTHOR.match(author)) and (
                not defaulted_author or (path and in_odoo_directory(path))
            )
            if defaulted_author and not odoo_author:
                author = ""
            is_odoo = name in std_cur or module.license == "OEEL-1" or odoo_author
            if is_odoo:
                odoo_modules.add(name)
            stuck = module.state in _STUCK
            if is_odoo and not stuck:
                continue

            raw_version = str((manifest or {}).get("version") or "")
            db_version = module.latest_version or ""
            disk_version = module.installed_version if path else ""

            notes = []
            if stuck:
                notes.append(_("Left in state \"%s\": finish or cancel it before migrating.") % module.state)
            if not is_odoo and path and db_version and disk_version and disk_version != db_version:
                notes.append(_("The code on disk is version %(disk)s but the database installed "
                               "%(db)s: an upgrade of this module is pending.") % {
                    "disk": disk_version, "db": db_version})
            parts = raw_version.split(".")
            if len(parts) >= 4 and "%s.%s" % (parts[0], parts[1]) != current:
                notes.append(_("Its manifest version %(v)s still names Odoo %(s)s.") % {
                    "v": raw_version, "s": "%s.%s" % (parts[0], parts[1])})
            removed = sorted(
                dep for dep in depends_on.get(name, [])
                if std_tgt is not None and dep in std_cur and dep not in std_tgt
            )

            readme = source_scan.read_readme(path) if path else None
            remotes = source_scan.git_remotes(path) if path else []
            info = {
                "name": name,
                "author": author,
                "website": module.website,
                "url": module.url,
                "license": module.license,
                "description": module.description,
                "readme": readme,
                "manifest": manifest,
                "git_remotes": remotes,
                "imported": module.imported if has_imported else None,
                "to_buy": module.to_buy,
                "standard": name in std_cur or module.license == "OEEL-1",
                "standard_license": std_cur.get(name) or module.license,
                "standard_by_licence": name not in std_cur and module.license == "OEEL-1",
                "has_store_packaging": bool(path) and source_scan.has_store_packaging(path, manifest),
                "on_disk": bool(path),
            }
            result = origin_lib.classify(info, company_names, company_domains, current, target)
            size, files = source_scan.footprint(path) if path else (None, None)
            fp = {
                "bytes": size,
                "files": files,
                "views": _lines(module.views_by_module),
                "menus": _lines(module.menus_by_module),
                "reports": _lines(module.reports_by_module),
                "dependents": len(dependents.get(name, [])),
            }
            rows.append({
                "name": name,
                "shortdesc": module.shortdesc,
                "author": author or False,
                "module_state": module.state,
                "db_version": db_version,
                "disk_version": disk_version,
                "manifest_version": raw_version,
                "license": module.license,
                "size_bytes": size or 0,
                "size_text": origin_lib.human_size(size) if size is not None else _("no code on disk"),
                "file_count": files or 0,
                "view_count": fp["views"],
                "menu_count": fp["menus"],
                "report_count": fp["reports"],
                "origin": result["bucket"],
                "origin_signals": "\n".join(result["signals"]),
                "target_action": origin_lib.target_action(result, name, target, fp, removed),
                "dependent_count": fp["dependents"],
                "dependents": ", ".join(dependents.get(name, [])) or False,
                "listed_because": "\n".join(notes) or False,
                "path": path or False,
            })
            if path and not is_odoo and not info["imported"] and name != self._module:
                code_targets.append((name, path))

        rows.sort(key=lambda r: (-(r["size_bytes"] or 0), r["name"]))
        counts = {
            "installed": sum(1 for m in modules if m.state in _INSTALLED),
            "third_party": sum(1 for r in rows if r["origin"] not in (
                origin_lib.ODOO_STANDARD, origin_lib.ODOO_ENTERPRISE)),
        }
        return rows, counts, code_targets, odoo_modules

    def _inventory_code(self, current, target, code_targets):
        """Deprecated-code pattern hits in non-Odoo modules."""
        catalogue = source_scan.load_patterns()
        patterns = source_scan.relevant_patterns(catalogue, current, target)
        caveat = _(
            "Pattern matching, not code analysis: it finds source lines that look like code Odoo "
            "%(t)s removes or changes. Some may be false positives, and an empty list does not "
            "prove the code is clean."
        ) % {"t": target}
        gap = source_scan.uncatalogued_gap(catalogue, current, target)
        gap_line = gap and (_(
            "The pattern list has no entries for Odoo %(series)s, so changes made in that "
            "version were not looked for."
        ) % {"series": ", ".join(gap)})
        if not patterns:
            return [], "\n".join(filter(None, [caveat, _(
                "No listed pattern changes between Odoo %(c)s and %(t)s, so no code was read."
            ) % {"c": current, "t": target}, gap_line]))
        hits, skipped = [], []
        for name, path in code_targets:
            h, s, _f = source_scan.scan_module(name, path, patterns)
            hits.extend(h)
            skipped.extend(s)
        explain = {p["id"]: p["explanation"] for p in patterns}
        order = {p["id"]: i for i, p in enumerate(patterns)}
        hits.sort(key=lambda h: (order[h["pattern"]], h["module"], h["file"], h["line"]))
        for hit in hits:
            hit["explanation"] = explain[hit["pattern"]]
        lines = [caveat]
        if gap_line:
            lines.append(gap_line)
        if skipped:
            lines.append(_("Not read (larger than %(mb)s MB, or unreadable): %(files)s") % {
                "mb": source_scan.MAX_SCAN_BYTES // (1024 * 1024), "files": ", ".join(skipped)})
        return hits, "\n".join(lines)

    def _analytic_plan_field_names(self):
        """Field names Odoo's analytic module creates itself as manual fields.

        From Odoo 17.0 each root analytic plan gets a stored field on the
        models that use analytic plans, and from 18.0 each sub-plan level a
        related one. They are named by the plan model's own methods, which
        are asked here; versions without them create no such fields.
        """
        if "account.analytic.plan" not in self.env:
            return set()
        Plan = self.env["account.analytic.plan"].sudo().with_context(active_test=False)
        if not hasattr(Plan, "_strict_column_name"):
            return set()
        names = set()
        for plan in Plan.search([]):
            try:
                if not plan.parent_id:
                    names.add(plan._strict_column_name())
                elif hasattr(plan, "_hierarchy_name"):
                    names.add(plan._hierarchy_name()[1])
            except Exception:
                if not plan.parent_id:
                    names.add("x_plan%d_id" % plan.id)
        return names

    def _inventory_studio(self, record_types):
        """Customisations made in the database, one row per item."""
        cr = self.env.cr
        by_model = {model: desc for (model, desc) in record_types.values()}
        rows = []
        cr.execute("SELECT res_id FROM ir_model_data "
                   "WHERE model = 'ir.model.fields' AND module = 'studio_customization'")
        studio_fields = {res_id for (res_id,) in cr.fetchall()}
        plan_fields = self._analytic_plan_field_names()
        Fields = self.env["ir.model.fields"].sudo()
        for field in Fields.search([("state", "=", "manual")], order="model, name"):
            if field.name in plan_fields and field.relation in _ANALYTIC_PLAN_RELATIONS:
                continue
            studio = field.id in studio_fields or field.name.startswith("x_studio_")
            rows.append({
                "kind": "field",
                "name": field.name,
                "label": field.field_description,
                "field_type": field.ttype,
                "model": field.model,
                "record_type": by_model.get(field.model, field.model),
                "created_with": _("Studio") if studio else _("Settings › Technical (manually)"),
            })
        for model in self.env["ir.model"].sudo().search([("state", "=", "manual")], order="model"):
            rows.append({
                "kind": "model",
                "name": model.model,
                "label": model.name,
                "model": model.model,
                "record_type": model.name,
                "created_with": (_("Studio") if model.model.startswith("x_studio")
                                 else _("Settings › Technical (manually)")),
            })
        cr.execute("""
            SELECT v.name, v.model, v.active FROM ir_ui_view v
             WHERE v.name LIKE 'Odoo Studio:%%'
                OR EXISTS (SELECT 1 FROM ir_model_data d WHERE d.model = 'ir.ui.view'
                           AND d.res_id = v.id AND d.module = 'studio_customization')
             ORDER BY v.model, v.name""")
        for name, model, active in cr.fetchall():
            rows.append({
                "kind": "view", "name": name, "label": name, "model": model,
                "record_type": by_model.get(model, model), "created_with": _("Studio"),
                "item_active": bool(active),
            })
        if "base.automation" in self.env:
            Automation = self.env["base.automation"].sudo().with_context(active_test=False)
            for rule in Automation.search([], order="id"):
                model = rule.model_id.model or ""
                rows.append({
                    "kind": "automation", "name": rule.name or "", "label": rule.name or "",
                    "model": model, "record_type": by_model.get(model, model),
                    "created_with": _("Automation rules"), "item_active": bool(rule.active),
                })
        return rows

    def _inventory_views(self, odoo_modules):
        """Views from outside Odoo's own modules that change a standard view.

        Website copies are excluded: when a theme is applied or a page is edited,
        the website module copies views per website (website_id set, and
        theme_template_id for theme copies). Those are website content, not code
        that changes a standard screen.
        """
        cr = self.env.cr
        cr.execute("SELECT column_name FROM information_schema.columns "
                   "WHERE table_name = 'ir_ui_view' AND column_name IN ('website_id', 'theme_template_id')")
        columns = {c for (c,) in cr.fetchall()}
        conditions = ["v.%s IS NULL" % c for c in sorted(columns)] or ["TRUE"]
        own = set(odoo_modules)
        cr.execute("SELECT name FROM ir_module_module WHERE license = 'OEEL-1'")
        own |= {name for (name,) in cr.fetchall()}
        no_module = _("(created in the database)")
        cr.execute(_INHERITING_VIEWS.format(website_filter=" AND ".join(conditions)))
        rows = []
        for name, model, active, cmod, cname, pmod, pname in cr.fetchall():
            if not pmod or pmod not in own:
                continue
            if cmod and cmod in own:
                continue
            rows.append({
                "module": cmod or no_module,
                "view_name": name,
                "view_xmlid": "%s.%s" % (cmod, cname) if cmod else False,
                "model": model,
                "inherited_xmlid": "%s.%s" % (pmod, pname),
                "inherited_module": pmod,
                "view_active": bool(active),
            })
        per_module = Counter(r["module"] for r in rows)
        for row in rows:
            row["module_view_count"] = per_module[row["module"]]
        rows.sort(key=lambda r: (-r["module_view_count"], r["module"], r["model"] or "", r["view_name"] or ""))
        return rows


    def _store_results(self, facts):
        row, untrusted = facts["row"], facts["untrusted"]
        vendored = [c for c in runner.load_public_checks() if c["id"] not in _SKIPPED_VENDORED]
        results = runner.run_checks(row, untrusted, vendored)
        results += runner.run_checks(row, untrusted, runner.load_native_checks())
        wording = _load_check_text()

        vals = []
        for index, result in enumerate(results):
            text = wording.get(result["id"], {})
            vals.append({
                "scan_id": self.id,
                "sequence": index,
                "kind": "check",
                "check_id": result["id"],
                "name": text.get("name") or result["name"],
                "status": result["status"],
                "severity": False,
                "skip_reason": result["reason"] or False,
                "explanation": text.get("business_impact") or result["business_impact"] or result["symptom"],
                "what_to_do": text.get("recommendation") or result["recommendation"],
                "technical_detail": text.get("technical_explanation") or result["technical_explanation"],
                "evidence_summary": self._evidence_text(result["evidence"]) or False,
                "evidence_detail": facts["details"].get(result["id"]) or False,
                "handbook_url": result["handbook"] or _CHECK_HANDBOOK.get(result["id"]) or False,
            })
        for index, item in enumerate(items_of_kind("assessment")):
            vals.append({
                "scan_id": self.id,
                "sequence": 100 + index,
                "kind": "assessment",
                "name": item["title"],
                "explanation": item["body"],
                "what_to_do": item["what_to_do"],
                "handbook_url": item["handbook"] or False,
            })
        self.env["odexalabs.migration.finding"].create(vals)

        def ranked(model, rows):
            if rows:
                self.env[model].create([
                    dict(r, scan_id=self.id, sequence=i + 1) for i, r in enumerate(rows)
                ])

        types = facts["record_types"]
        ranked("odexalabs.migration.module", facts["modules"])
        ranked("odexalabs.migration.int4", [{
            "record_type": types.get(r["table"], (None, r["table"]))[1],
            "model_name": types.get(r["table"], (False,))[0],
            "table_name": r["table"],
            "column_name": r["column"],
            "sequence_name": r["sequence"],
            "last_value": r.get("last_value") or 0,
            "share_used": r.get("share") or 0,
            "note": r.get("position_reason") or False,
        } for r in facts["int4"]
            if (r.get("position_reason") or r.get("is_called"))
            and not r["table"].startswith(_OWN_TABLE_PREFIX)])
        ranked("odexalabs.migration.table", [{
            "record_type": types.get(r["table"], (None, r["table"]))[1],
            "table_name": r["table"],
            "schema_name": r["schema"],
            "est_rows": r["est_rows"] or 0,
            "total_bytes": r["total_bytes"],
            "table_bytes": r["table_bytes"],
            "total_size": origin_lib.human_size(r["total_bytes"]),
        } for r in facts["tables"] if not r["table"].startswith(_OWN_TABLE_PREFIX)])
        ranked("odexalabs.migration.studio", facts["studio"])
        ranked("odexalabs.migration.view", facts["views"])
        ranked("odexalabs.migration.code.hit", [{
            "pattern_id": h["pattern"],
            "since": h["since"],
            "module": h["module"],
            "file_path": h["file"],
            "line_number": h["line"],
            "line_text": h["text"],
            "explanation": h["explanation"],
        } for h in facts["hits"]])

        self._set_readiness(results)
        self.write({
            "state": "done",
            "current_version": facts["current"],
            "python_version": row["python_version"],
            "postgres_version": row["postgres_version"] or False,
            "installed_module_count": facts["module_counts"]["installed"],
            "third_party_module_count": facts["module_counts"]["third_party"],
            "customisation_count": len(facts["studio"]),
            "inv_code_summary": facts["code_summary"],
        })

    def _set_readiness(self, results):
        """Score the checks only; inventories never enter it.

        The score is passed ÷ all checks. A check that could not be
        determined is not a pass, so it can never raise the score; the
        summary says how many there were, so the number is not read as
        coverage.
        """
        total = len(results)
        passed = sum(1 for r in results if r["status"] == "clear")
        fired = sum(1 for r in results if r["status"] == "fired")
        skipped = total - passed - fired
        self.readiness_score = int(round(100.0 * passed / total)) if total else 0
        parts = [_("%(passed)s of %(total)s checks passed") % {"passed": passed, "total": total}]
        if fired:
            parts.append(_("%s need attention") % fired)
        if skipped:
            parts.append(_("%s could not be determined") % skipped)
        parts.append(_("%s questions to review") % len(items_of_kind("assessment")))
        self.readiness_summary = " · ".join(parts)
