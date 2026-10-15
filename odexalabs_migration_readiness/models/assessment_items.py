# -*- coding: utf-8 -*-
"""What needs review: questions no scan answers from the instance.

`kind` is the same discriminator as `odexalabs.migration.finding.kind`.
Only "assessment" items are produced now; the value is shown to users as
"Needs review". ("deeper" rows exist only in scans made by earlier versions
of this module and are no longer created.)

Copy rules: counts are derived with len(items_of_kind(...)) wherever they
are shown; no figure saying where a value starts to matter; no prevalence,
severity, effort or duration claim.
"""

_HANDBOOK = "https://odexalabs.com/odoo-handbook/upgrading-safely"

ASSESSMENT_ITEMS = [
    {
        "key": "custom_code_correctness",
        "kind": "assessment",
        "title": "Whether your custom code still behaves correctly on the target",
        "body": (
            "The deprecated-code inventory finds patterns: lines that use something the "
            "target version removes or changes. It cannot tell whether a module still does "
            "the right thing on the target. That needs someone to read the code against the "
            "target's changes and to exercise it on an upgraded copy of the database. No "
            "scan answers it, this one included."
        ),
        "what_to_do": (
            "Upgrade a copy of the database to the target, run each custom module's main "
            "flows on it, and have someone who knows the code read it against the target's "
            "changes, starting from the deprecated-code hits."
        ),
        "handbook": "",
    },
    {
        "key": "downtime_window",
        "kind": "assessment",
        "title": "Whether the numbers fit your downtime window",
        "body": (
            "The inventories give real figures: table sizes, row estimates, sequence "
            "positions, module sizes. Whether they fit the time you can be offline depends "
            "on your hardware and on how much downtime you can tolerate, and that judgement "
            "is yours. The only reliable input to it is a timed rehearsal."
        ),
        "what_to_do": (
            "Rehearse the upgrade on a copy with production volume, on hardware like "
            "production's, and time it end to end."
        ),
        "handbook": _HANDBOOK + "#schema-changes-large-tables",
    },
]


def items_of_kind(kind):
    """The items of one kind, in list order.

    Every count of these lists is derived from len() of this call and is
    never written as a literal anywhere. Adding an item is a one-line change
    here and nowhere else.
    """
    return [item for item in ASSESSMENT_ITEMS if item["kind"] == kind]
