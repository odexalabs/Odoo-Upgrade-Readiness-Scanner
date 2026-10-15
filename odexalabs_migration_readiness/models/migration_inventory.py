# -*- coding: utf-8 -*-
"""Inventory rows: facts, ranked and counted, with no verdict attached.

None of these models carries a severity, a status, a score or a risk field,
and none of their views carries a decoration: colour and warning glyphs
assert where a line sits without printing it. Whether a number is
acceptable is the calibrated half, and it is not in this module.

Byte and row figures are Float with no decimals because Odoo's Integer is a
PostgreSQL int4, and a table can be larger than 2,147,483,647 bytes.
"""

from odoo import fields, models

from ..lib.origin import BUCKETS

_BIG = (20, 0)


class MigrationModule(models.Model):
    """Installed modules that do not come from Odoo, and what each needs."""

    _name = "odexalabs.migration.module"
    _description = "Upgrade Readiness: installed module"
    _order = "sequence, id"

    scan_id = fields.Many2one("odexalabs.migration.scan", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer()
    name = fields.Char("Module", required=True)
    shortdesc = fields.Char("Title")
    author = fields.Char()
    module_state = fields.Char("State in the database")
    db_version = fields.Char("Installed in the database", help="latest_version: what the database installed.")
    disk_version = fields.Char("Code on disk", help="installed_version: computed from the manifest on disk.")
    manifest_version = fields.Char("Manifest version as written")
    license = fields.Char("Licence")
    size_bytes = fields.Float("Size on disk (bytes)", digits=_BIG)
    size_text = fields.Char("Size on disk")
    file_count = fields.Integer("Files")
    view_count = fields.Integer("Screens (views)")
    menu_count = fields.Integer("Menus")
    report_count = fields.Integer("Reports")
    origin = fields.Selection(BUCKETS, "Comes from")
    origin_signals = fields.Text("Why we think so")
    target_action = fields.Text("For the target version")
    dependent_count = fields.Integer("Used by")
    dependents = fields.Text("Depended on by")
    listed_because = fields.Text("Notes")
    path = fields.Char("Path on disk")


class MigrationInt4(models.Model):
    """Record ID capacity: how far each record type's ID counter has gone."""

    _name = "odexalabs.migration.int4"
    _description = "Upgrade Readiness: record ID capacity"
    _order = "sequence, id"

    scan_id = fields.Many2one("odexalabs.migration.scan", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer("Rank")
    record_type = fields.Char("Record type")
    model_name = fields.Char("Technical model")
    table_name = fields.Char("Table", required=True)
    column_name = fields.Char("Column")
    sequence_name = fields.Char("Counter (sequence)")
    last_value = fields.Float("IDs used", digits=_BIG)
    share_used = fields.Float("% of maximum", digits=(12, 4))
    note = fields.Char("Not read because")


class MigrationTable(models.Model):
    """Largest tables by total size."""

    _name = "odexalabs.migration.table"
    _description = "Upgrade Readiness: table size"
    _order = "sequence, id"

    scan_id = fields.Many2one("odexalabs.migration.scan", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer("Rank")
    record_type = fields.Char("Record type")
    table_name = fields.Char("Table", required=True)
    schema_name = fields.Char("Schema")
    est_rows = fields.Float("Rows (approx.)", digits=_BIG)
    total_bytes = fields.Float("Total size (bytes)", digits=_BIG)
    table_bytes = fields.Float("Table only (bytes)", digits=_BIG)
    total_size = fields.Char("Total size")


class MigrationStudio(models.Model):
    """One customisation made in the database: a field, model, view or rule."""

    _name = "odexalabs.migration.studio"
    _description = "Upgrade Readiness: customisation"
    _order = "sequence, id"

    scan_id = fields.Many2one("odexalabs.migration.scan", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer()
    kind = fields.Selection(
        [("field", "Field"), ("model", "Model"), ("view", "View"), ("automation", "Automation rule")],
        required=True,
    )
    name = fields.Char("Technical name")
    label = fields.Char()
    field_type = fields.Char("Field type")
    model = fields.Char("Technical model")
    record_type = fields.Char("Record type")
    created_with = fields.Char("Created with")
    item_active = fields.Boolean("Active", default=True)


class MigrationView(models.Model):
    """Views from outside Odoo's own modules that modify a standard view."""

    _name = "odexalabs.migration.view"
    _description = "Upgrade Readiness: changed standard view"
    _order = "sequence, id"

    scan_id = fields.Many2one("odexalabs.migration.scan", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer()
    module = fields.Char("Added by module")
    module_view_count = fields.Integer("Views from this module")
    view_name = fields.Char("View")
    view_xmlid = fields.Char("Its external id")
    model = fields.Char("Technical model")
    inherited_xmlid = fields.Char("Standard view it changes")
    inherited_module = fields.Char("Standard module")
    view_active = fields.Boolean("Active", default=True)


class MigrationCodeHit(models.Model):
    """A source line matching code the target version removes or changes."""

    _name = "odexalabs.migration.code.hit"
    _description = "Upgrade Readiness: code to update"
    _order = "sequence, id"

    scan_id = fields.Many2one("odexalabs.migration.scan", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer()
    pattern_id = fields.Char("Pattern", required=True)
    since = fields.Char("Changes in")
    module = fields.Char()
    file_path = fields.Char("File")
    line_number = fields.Integer("Line")
    line_text = fields.Char("Source line")
    explanation = fields.Char("What changes")
