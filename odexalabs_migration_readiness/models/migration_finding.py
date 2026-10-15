# -*- coding: utf-8 -*-
from odoo import fields, models


class MigrationFinding(models.Model):
    _name = "odexalabs.migration.finding"
    _description = "Upgrade Readiness Finding"
    _order = "sequence, id"

    scan_id = fields.Many2one(
        "odexalabs.migration.scan",
        string="Scan",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)

    kind = fields.Selection(
        [
            ("check", "Check result"),
            ("assessment", "Needs review"),
            ("deeper", "Earlier versions only"),
        ],
        required=True,
        default="check",
        help="Check result rows carry a status because something was "
        "evaluated. Needs review rows do not: no scan can answer them from "
        "the instance.",
    )

    check_id = fields.Char(string="Check", readonly=True)
    name = fields.Char(required=True, readonly=True)

    status = fields.Selection(
        [
            ("fired", "Needs attention"),
            ("clear", "No problem found"),
            ("skipped", "Could not determine"),
        ],
        readonly=True,
        help="Could not determine is not a pass. It means the scan had no "
        "trustworthy basis to answer, and the question is still open.",
    )
    severity = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")],
        readonly=True,
    )
    skip_reason = fields.Char(readonly=True)

    explanation = fields.Text(
        readonly=True,
        help="Plain-language explanation of what this means.",
    )
    what_to_do = fields.Text(readonly=True)
    technical_detail = fields.Text(readonly=True)
    evidence_summary = fields.Char(readonly=True)
    evidence_detail = fields.Text(
        readonly=True,
        help="Item by item: each sequence, each foreign key, each pinned "
        "requirement, and anything that could not be checked, with the reason.",
    )
    handbook_url = fields.Char(readonly=True)
