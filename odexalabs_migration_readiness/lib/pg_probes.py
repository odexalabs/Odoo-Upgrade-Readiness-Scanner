# -*- coding: utf-8 -*-
"""Read-only PostgreSQL probes for MRS-002..004 and INV-002/003.

Every statement here is a SELECT or a SHOW, plus the SAVEPOINT / SET LOCAL /
ROLLBACK TO SAVEPOINT bracket that bounds a probe. The bracket is always
rolled back, success or failure, so a probe leaves neither data nor session
settings behind. Nothing in this file writes, and nothing imports
`odexalabs`.

Probes that can be expensive (MAX() of a column, a foreign-key anti-join)
run under their own statement timeout and inside an overall time budget.
A probe that does not finish is reported as NOT CHECKED with its reason,
never as clear.

Works with an Odoo cursor or a plain psycopg2 cursor: only execute() and
fetchall() are used.
"""

import time

import psycopg2

INT4_MAX = 2147483647

_SYSTEM_SCHEMA_FILTER = (
    "%(ns)s.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND %(ns)s.nspname NOT LIKE 'pg\\_toast%%' "
    "AND %(ns)s.nspname NOT LIKE 'pg\\_temp%%'"
)


def _system_filter(alias):
    return _SYSTEM_SCHEMA_FILTER % {"ns": alias}


def _execute(cr, query, params=None):
    """Execute without Odoo logging an expected failure as an ERROR."""
    try:
        cr.execute(query, params, log_exceptions=False)
    except TypeError:
        cr.execute(query, params)


class ProbeClock(object):
    """The scan's time budget for bounded probes."""

    def __init__(self, budget_seconds, probe_timeout_ms):
        self.budget_seconds = max(0, int(budget_seconds or 0))
        self.probe_timeout_ms = max(1, int(probe_timeout_ms or 1))
        self.deadline = time.monotonic() + self.budget_seconds

    def remaining_ms(self):
        return int((self.deadline - time.monotonic()) * 1000)

    def timeout_for_next_probe(self):
        """Milliseconds the next probe may take, or 0 when the budget is spent."""
        remaining = self.remaining_ms()
        if remaining <= 0:
            return 0
        return min(self.probe_timeout_ms, remaining)


class ProbeResult(object):
    __slots__ = ("rows", "reason")

    def __init__(self, rows=None, reason=None):
        self.rows = rows
        self.reason = reason

    @property
    def ok(self):
        return self.reason is None


def bounded(cr, query, params=None, timeout_ms=None):
    """Run one read-only query under a statement timeout.

    Always rolls back to its savepoint afterwards, which undoes the SET LOCAL
    too, so the caller's transaction is left exactly as it was.
    """
    _execute(cr, "SAVEPOINT odexalabs_mrs_probe")
    rows = reason = None
    try:
        if timeout_ms:
            _execute(cr, "SET LOCAL statement_timeout = %d" % int(timeout_ms))
        _execute(cr, query, params)
        rows = cr.fetchall()
    except psycopg2.extensions.QueryCanceledError:
        reason = "did not finish within the probe limit (%d ms)" % int(timeout_ms or 0)
    except psycopg2.Error as exc:
        message = (getattr(exc, "pgerror", None) or str(exc)).strip().splitlines()
        reason = "could not be read: %s" % (message[0] if message else type(exc).__name__)
    finally:
        _execute(cr, "ROLLBACK TO SAVEPOINT odexalabs_mrs_probe")
        _execute(cr, "RELEASE SAVEPOINT odexalabs_mrs_probe")
    return ProbeResult(rows, reason)


def bounded_with_clock(cr, clock, query, params=None):
    timeout = clock.timeout_for_next_probe()
    if timeout <= 0:
        return ProbeResult(None, "not probed: the scan's probe time budget (%d s) was used up"
                           % clock.budget_seconds)
    return bounded(cr, query, params, timeout)


def server_version(cr):
    """(server_version_num as int, server_version as text)."""
    _execute(cr, "SELECT current_setting('server_version_num')::int, current_setting('server_version')")
    num, text = cr.fetchone() if hasattr(cr, "fetchone") else cr.fetchall()[0]
    return int(num), text


_SEQUENCE_CATALOG = """
WITH links AS (
    SELECT d.objid AS seq_oid, d.refobjid AS tbl_oid, d.refobjsubid AS attnum
      FROM pg_depend d
     WHERE d.classid = 'pg_class'::regclass
       AND d.refclassid = 'pg_class'::regclass
       AND d.deptype IN ('a', 'i')
       AND d.refobjsubid > 0
    UNION
    SELECT d.refobjid, ad.adrelid, ad.adnum
      FROM pg_attrdef ad
      JOIN pg_depend d ON d.classid = 'pg_attrdef'::regclass
                      AND d.objid = ad.oid
                      AND d.refclassid = 'pg_class'::regclass
)
SELECT DISTINCT ON (s.oid, t.oid, a.attnum)
       s.relname,
       quote_ident(sn.nspname) || '.' || quote_ident(s.relname),
       t.relname,
       quote_ident(tn.nspname) || '.' || quote_ident(t.relname),
       a.attname,
       quote_ident(a.attname),
       format_type(a.atttypid, a.atttypmod),
       has_sequence_privilege(s.oid, 'SELECT'),
       has_table_privilege(t.oid, 'SELECT'),
       sq.seqincrement,
       GREATEST(t.reltuples, 0)
  FROM links l
  JOIN pg_class s ON s.oid = l.seq_oid AND s.relkind = 'S'
  JOIN pg_namespace sn ON sn.oid = s.relnamespace
  JOIN pg_sequence sq ON sq.seqrelid = s.oid
  JOIN pg_class t ON t.oid = l.tbl_oid AND t.relkind IN ('r', 'p')
  JOIN pg_namespace tn ON tn.oid = t.relnamespace
  JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = l.attnum AND NOT a.attisdropped
 WHERE {filter}
 ORDER BY s.oid, t.oid, a.attnum
"""


def sequence_catalog(cr):
    """Every sequence owned by, or defaulting, a column. Catalog only, cheap.

    Returns (rows, reason); reason is set, and rows empty, if it could not be read.
    """
    result = bounded(cr, _SEQUENCE_CATALOG.format(filter=_system_filter("tn")))
    if not result.ok:
        return [], "the sequence catalog %s" % result.reason
    out = []
    for row in result.rows:
        out.append(
            {
                "sequence": row[0],
                "sequence_q": row[1],
                "table": row[2],
                "table_q": row[3],
                "column": row[4],
                "column_q": row[5],
                "column_type": row[6],
                "can_read_sequence": bool(row[7]),
                "can_read_table": bool(row[8]),
                "increment": int(row[9]),
                "est_rows": float(row[10] or 0),
            }
        )
    out.sort(key=lambda r: (r["est_rows"], r["table"], r["column"]))
    return out, None


def read_sequence_position(cr, seq):
    """Add last_value / is_called / next_value to `seq`, or a reason it could not be read."""
    if not seq["can_read_sequence"]:
        seq["position_reason"] = "no privilege to read the sequence"
        return seq
    result = bounded(cr, "SELECT last_value, is_called FROM %s" % seq["sequence_q"])
    if not result.ok:
        seq["position_reason"] = "the sequence %s" % result.reason
        return seq
    last_value, is_called = result.rows[0]
    seq["last_value"] = int(last_value)
    seq["is_called"] = bool(is_called)
    seq["next_value"] = int(last_value) + (seq["increment"] if is_called else 0)
    return seq


def sequence_behind_facts(cr, clock, catalog):
    """MRS-003: each sequence against its column's MAX(), with the first collision.

    Returns (rows, summary). Each row carries `state`: "behind", "ok" or
    "not_checked" (with `reason`).
    """
    rows = []
    for seq in catalog:
        seq = dict(seq)
        if "next_value" not in seq and "position_reason" not in seq:
            read_sequence_position(cr, seq)
        if seq.get("position_reason"):
            seq.update(state="not_checked", reason=seq["position_reason"])
            rows.append(seq)
            continue
        if not seq["can_read_table"]:
            seq.update(state="not_checked", reason="no privilege to read table %s" % seq["table"])
            rows.append(seq)
            continue
        if seq["increment"] <= 0:
            seq.update(state="not_checked", reason="descending sequence; not compared")
            rows.append(seq)
            continue
        result = bounded_with_clock(
            cr, clock, "SELECT MAX(%s) FROM %s" % (seq["column_q"], seq["table_q"])
        )
        if not result.ok:
            seq.update(state="not_checked", reason="MAX(%s) %s" % (seq["column"], result.reason))
            rows.append(seq)
            continue
        maximum = result.rows[0][0]
        seq["max_value"] = maximum
        if maximum is None or seq["next_value"] > maximum:
            seq["state"] = "ok"
            rows.append(seq)
            continue
        seq["state"] = "behind"
        if seq["increment"] == 1:
            first = bounded_with_clock(
                cr,
                clock,
                "SELECT MIN(%s) FROM %s WHERE %s >= %%s"
                % (seq["column_q"], seq["table_q"], seq["column_q"]),
                (seq["next_value"],),
            )
            if first.ok and first.rows[0][0] is not None:
                seq["first_collision"] = first.rows[0][0]
                seq["free_before_collision"] = first.rows[0][0] - seq["next_value"]
        rows.append(seq)
    summary = {
        "sequences_checked": sum(1 for r in rows if r["state"] in ("ok", "behind")),
        "sequences_behind": sum(1 for r in rows if r["state"] == "behind"),
        "sequences_not_checked": sum(1 for r in rows if r["state"] == "not_checked"),
    }
    return rows, summary


def int4_id_columns(cr, catalog):
    """INV-002: integer columns fed by a sequence, with the sequence's position.

    Ranked by the share of the int4 range the position represents. Unreadable
    positions are kept, with their reason, and rank last.
    """
    rows = []
    for seq in catalog:
        if seq["column_type"] != "integer":
            continue
        seq = dict(seq)
        if "next_value" not in seq and "position_reason" not in seq:
            read_sequence_position(cr, seq)
        if "last_value" in seq:
            seq["share"] = 100.0 * seq["last_value"] / INT4_MAX
        rows.append(seq)
    rows.sort(key=lambda r: (r.get("share") is None, -(r.get("share") or 0), r["table"]))
    return rows


_FK_CATALOG = """
SELECT c.conname,
       cl.relname,
       quote_ident(cn.nspname) || '.' || quote_ident(cl.relname),
       pl.relname,
       quote_ident(pn.nspname) || '.' || quote_ident(pl.relname),
       ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(n, o)
               JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.o),
       ARRAY(SELECT quote_ident(a.attname) FROM unnest(c.conkey) WITH ORDINALITY k(n, o)
               JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.o),
       ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY k(n, o)
               JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.o),
       ARRAY(SELECT quote_ident(a.attname) FROM unnest(c.confkey) WITH ORDINALITY k(n, o)
               JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.o),
       c.convalidated,
       has_table_privilege(cl.oid, 'SELECT') AND has_table_privilege(pl.oid, 'SELECT'),
       GREATEST(cl.reltuples, 0)
  FROM pg_constraint c
  JOIN pg_class cl ON cl.oid = c.conrelid
  JOIN pg_namespace cn ON cn.oid = cl.relnamespace
  JOIN pg_class pl ON pl.oid = c.confrelid
  JOIN pg_namespace pn ON pn.oid = pl.relnamespace
 WHERE c.contype = 'f' AND {filter}
 ORDER BY GREATEST(cl.reltuples, 0), cl.relname, c.conname
"""


def foreign_key_catalog(cr):
    """Returns (rows, reason), like sequence_catalog()."""
    result = bounded(cr, _FK_CATALOG.format(filter=_system_filter("cn")))
    if not result.ok:
        return [], "the foreign key catalog %s" % result.reason
    out = []
    for row in result.rows:
        out.append(
            {
                "constraint": row[0],
                "table": row[1],
                "table_q": row[2],
                "ref_table": row[3],
                "ref_table_q": row[4],
                "columns": list(row[5]),
                "columns_q": list(row[6]),
                "ref_columns": list(row[7]),
                "ref_columns_q": list(row[8]),
                "validated": bool(row[9]),
                "can_read": bool(row[10]),
                "est_rows": float(row[11] or 0),
            }
        )
    return out, None


def _orphan_predicate(fk):
    not_null = " AND ".join("c.%s IS NOT NULL" % col for col in fk["columns_q"])
    match = " AND ".join(
        "p.%s = c.%s" % (ref, col) for ref, col in zip(fk["ref_columns_q"], fk["columns_q"])
    )
    return "%s AND NOT EXISTS (SELECT 1 FROM %s p WHERE %s)" % (not_null, fk["ref_table_q"], match)


def foreign_key_violation_facts(cr, clock, catalog):
    """MRS-004: per-FK anti-join, existence first, then the count.

    Each row carries `state`: "violated", "ok" or "not_checked" (with
    `reason`). A violated row carries `count`, or `count_reason` when the
    count did not finish; existence is then still established.
    """
    rows = []
    for fk in catalog:
        fk = dict(fk)
        if not fk["can_read"]:
            fk.update(state="not_checked", reason="no privilege to read %s or %s" % (fk["table"], fk["ref_table"]))
            rows.append(fk)
            continue
        if not fk["columns_q"] or len(fk["columns_q"]) != len(fk["ref_columns_q"]):
            fk.update(state="not_checked", reason="constraint columns could not be resolved")
            rows.append(fk)
            continue
        predicate = _orphan_predicate(fk)
        exists = bounded_with_clock(
            cr, clock, "SELECT EXISTS (SELECT 1 FROM %s c WHERE %s)" % (fk["table_q"], predicate)
        )
        if not exists.ok:
            fk.update(state="not_checked", reason=exists.reason)
            rows.append(fk)
            continue
        if not exists.rows[0][0]:
            fk["state"] = "ok"
            rows.append(fk)
            continue
        fk["state"] = "violated"
        counted = bounded_with_clock(
            cr, clock, "SELECT count(*) FROM %s c WHERE %s" % (fk["table_q"], predicate)
        )
        if counted.ok:
            fk["count"] = int(counted.rows[0][0])
        else:
            fk["count_reason"] = "count %s" % counted.reason
        rows.append(fk)
    summary = {
        "foreign_keys_checked": sum(1 for r in rows if r["state"] in ("ok", "violated")),
        "foreign_keys_violated": sum(1 for r in rows if r["state"] == "violated"),
        "foreign_keys_not_checked": sum(1 for r in rows if r["state"] == "not_checked"),
    }
    return rows, summary


_TABLES = """
SELECT n.nspname, c.relname, c.relkind, c.reltuples,
       pg_total_relation_size(c.oid), pg_relation_size(c.oid)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'p', 'm') AND {filter}
 ORDER BY pg_total_relation_size(c.oid) DESC, c.relname
"""


def largest_tables(cr):
    """INV-003: every user table, ranked by total relation size. Returns (rows, reason).

    reltuples is the planner's ESTIMATE of the row count, not a count. It is
    negative when the table has never been analysed; that is reported as
    "not estimated", never as zero.
    """
    result = bounded(cr, _TABLES.format(filter=_system_filter("n")))
    if not result.ok:
        return [], "the table catalog %s" % result.reason
    out = []
    for schema, name, kind, reltuples, total, heap in result.rows:
        out.append(
            {
                "schema": schema,
                "table": name,
                "kind": kind,
                "est_rows": None if reltuples is None or reltuples < 0 else int(reltuples),
                "total_bytes": int(total or 0),
                "table_bytes": int(heap or 0),
            }
        )
    return out, None
