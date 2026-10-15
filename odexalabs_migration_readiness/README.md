# Odoo Upgrade Readiness Scanner

Free Odoo module (AGPL-3) for Odoo 15.0, 16.0, 17.0, 18.0 and 19.0. Run it inside the
database you plan to upgrade. It answers what can be answered
deterministically, reports facts without verdicts, and names what it cannot
answer. It changes nothing and makes no outbound connection.

User guide: https://odexalabs.com/odoo-migration-readiness-scanner

## Output

**Checks** report `fired` (needs attention), `clear` (no problem found) or
`skipped` (could not determine, never a pass). No check condition contains a
number.

| Id | Question | Source |
|---|---|---|
| MIG-001 | Is the target version approaching or past its end of support? | `data/public_checks.json` (vendored) |
| MIG-002 | (not run: only newer targets can be chosen, and a constraint refuses others) | `data/public_checks.json` (vendored) |
| MRS-001 | Is this server's Python below the target's stated minimum? | `data/native_checks.json` |
| MRS-002 | Is PostgreSQL below the target's stated minimum? | `data/native_checks.json` |
| MRS-003 | Is a sequence behind its column's maximum id? | `data/native_checks.json` |
| MRS-004 | Do rows violate a foreign key constraint? | `data/native_checks.json` |
| MRS-005 | Is the current version approaching or past its end of support? | `data/native_checks.json` |

`MIG-*` ids come from the Odexalabs engine's public check export; `MRS-*` ids
are computed by this module and exist in no registry. The ids are internal:
users see the check names only. `data/check_text.json` holds the module's own
wording for the vendored checks (their conditions are untouched).

**Inventories** are facts, ranked and counted, with no severity, colour or
verdict. Each states what it read and whether it could read it.

| Id | What it lists |
|---|---|
| INV-001 | Installed modules that are not Odoo's own (Odoo's = in the Community list, OEEL-1 licence, or author Odoo S.A. when the manifest declares it or the module sits among Odoo's own): author, size, dependents, origin (a labelled heuristic showing its signals; Unclear when they conflict) and what to do for the target, with no constructed links; also stuck states, pending upgrades, older-series manifests and Odoo modules the target no longer has that a listed module depends on |
| INV-002 | Record ID capacity: per record type, IDs issued and share of the int4 maximum |
| INV-003 | Tables by total size, with the planner's row estimate |
| INV-004 | Customisations, one row each: manual fields and models, Studio views, automation rules |
| INV-005 | Views from outside Odoo's own modules that modify a standard view, by module (website copies excluded) |
| INV-006 | Deprecated-code pattern hits in non-standard modules, file and line (pattern matching: false positives possible, cannot prove code clean) |

A further list, **Needs review**, names what no scan can answer from the
database.

## Guarantees and their tests

- **Read-only.** A scan writes its own `odexalabs_migration_*` records only.
  `tests/test_claims.py` runs a full scan and asserts from
  `pg_stat_xact_user_tables`, a catalog fingerprint, every other sequence's
  position and the session settings that nothing else changed.
- **Offline.** The same file runs a scan with sockets and name resolution
  disabled and asserts no attempt was made, and checks that no shipped Python
  imports a network library and that the App Store page loads nothing remote.
- **No thresholds.** Check conditions hold no number; the one stated
  definition (`ENDING_SOON_MONTHS` in `lib/runner.py`, shown to the user) is
  exempt by name; inventory models carry no verdict field and their views no
  decoration.

The bounded probes (each `MAX()` and each foreign-key anti-join) run under a
statement timeout inside a savepoint that is always rolled back, within a time
budget set on the scan form. A probe that does not finish is reported as not
checked, by name.

## Data files

| File | Content |
|---|---|
| `data/public_checks.json` | Vendored export of the engine's public migration checks |
| `data/native_checks.json` | The module's own check definitions |
| `data/support_matrix.json` | Odoo versions: release, end of support, Python and PostgreSQL minimums, Python 3.10 pins. No stored status: it is computed from the dates at scan time |
| `data/standard_modules.json` | Community module names and licences per series (public odoo/odoo and odoo/design-themes). Enterprise modules are recognised by the licence the database records, and are not checked against a target list |
| `data/deprecation_patterns.json` | INV-006 catalogue: regex, file glob, the series it changes in, explanation, Community source citation |

The module never imports the Odexalabs engine.

## Versions

One source, written with Odoo 18.0/19.0 view syntax. The 15.0, 16.0 and 17.0
packages are generated from it by `tools/build_series.py`: `<list>` becomes
`<tree>`, and for 15.0 and 16.0 each `invisible="<expression>"` becomes the
equivalent `attrs` domain (the build stops on an expression it has no rule
for). The manifest version is bare (`1.1.0`); Odoo prefixes the running
series. The Python code keeps to Python 3.7, the oldest Odoo 15.0 and 16.0
accept; a test enforces it.

## Licence

AGPL-3.
