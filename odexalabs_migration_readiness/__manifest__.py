# -*- coding: utf-8 -*-
# Copyright 2026 Odexalabs
# License AGPL-3
{
    "name": "Odoo Upgrade Readiness Scanner - Migration Risk Check",
    "version": "1.1.0",
    "category": "Extra Tools",
    "summary": "Read-only pre-upgrade scan: version support, Python and PostgreSQL, broken "
    "sequences and foreign keys, third-party modules, record IDs, tables, Studio and code. Offline.",
    "description": """
Upgrade Readiness Scanner
=========================

A read-only scan you run inside the database before an Odoo upgrade: pick a
newer Odoo version as the target and run it. It answers
what can be answered for certain, reports facts without verdicts, and says
plainly what it cannot answer.

**Checks**: each reports Needs attention, No problem found or Could not
determine; Could not determine is never counted as a pass.

* The target version is approaching or past its end of support
* Your current version is approaching or past its end of support
* The server's Python is below the target's stated minimum
* PostgreSQL is below the target's stated minimum
* A sequence is behind its column's maximum id
* Rows violate a foreign key constraint

**Inventories**: facts and numbers, with no verdict attached.

* Installed modules that do not come from Odoo: author, size, where each comes
  from (the signals are shown) and what it needs for the target
* Record ID capacity: how far each record type's ID counter has gone
* The largest tables by size
* Customisations made in Studio or by hand: fields, models, views, rules
* Odoo screens that other modules modify
* Code to update: lines that use something the target removes or changes

**Guarantees**

* Read-only: the scan writes its own scan records and nothing else.
* No outbound network calls and no telemetry.
* No calibrated thresholds: inventories carry no verdict.
""",
    "author": "Odexalabs",
    "website": "https://odexalabs.com",
    "support": "support@odexalabs.com",
    "license": "AGPL-3",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "views/scan_views.xml",
        "views/menus.xml",
    ],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
