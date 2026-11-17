"""SQLite-backed catalog and budget ledger for joint release planning.

The store is the single source of truth for

* per-(dataset, principal) privacy limits (immutable once initialised),
* published raw measurements, including their full trusted declaration,
* the fixed coordinate domain of every (dataset, principal, snapshot),
* published query aliases together with their signed raw-noise lineage,
* every committed release report, keyed by its global ``release_id``.

Every mutating operation runs inside one ``BEGIN IMMEDIATE`` transaction so
that two independent processes competing for the last unit of budget (or a
publish interleaved with a retire) observe only serial outcomes.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import sqlite3

from .release_model import (
    Measurement,
    PublishedQuery,
    ReleaseValidationError,
    ScopeBudget,
    SnapshotView,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS limits (
    dataset TEXT NOT NULL,
    principal TEXT NOT NULL,
    epsilon TEXT NOT NULL,
    delta TEXT NOT NULL,
    PRIMARY KEY (dataset, principal)
);
CREATE TABLE IF NOT EXISTS domains (
    dataset TEXT NOT NULL,
    principal TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    dimensions TEXT NOT NULL,
    PRIMARY KEY (dataset, principal, snapshot)
);
CREATE TABLE IF NOT EXISTS measurements (
    dataset TEXT NOT NULL,
    principal TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    id TEXT NOT NULL,
    coefficients TEXT NOT NULL,
    value TEXT NOT NULL,
    variance TEXT NOT NULL,
    epsilon TEXT NOT NULL,
    delta TEXT NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset, principal, snapshot, id)
);
CREATE TABLE IF NOT EXISTS queries (
    dataset TEXT NOT NULL,
    principal TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    release_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    coefficients TEXT NOT NULL,
    value TEXT NOT NULL,
    lineage TEXT NOT NULL,
    PRIMARY KEY (dataset, principal, snapshot, release_id, query_id)
);
CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    principal TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    choice_id TEXT NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS init_marker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initialized INTEGER NOT NULL
);
"""


def to_fraction_string(value):
    return str(Fraction(value))


def parse_fraction_string(text, label="fraction"):
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise ReleaseValidationError("invalid fraction string: " + label)


def connect(path):
    """Open (creating if needed) the writable catalog connection."""
    path = Path(path)
    if path.exists() and path.is_symlink():
        raise ValueError("database cannot be a symlink")
    connection = sqlite3.connect(str(path), timeout=60, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    return connection


def open_readonly(path):
    """Open an existing catalog without creating files or writing state."""
    path = Path(path)
    if not path.exists():
        raise ValueError("catalog database does not exist")
    if path.is_symlink():
        raise ValueError("database cannot be a symlink")
    connection = sqlite3.connect(
        "file:" + str(path.resolve()), uri=True, timeout=60, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


# ---------------------------------------------------------------------------
# serialisation helpers


def encode_vector(vector):
    return ",".join(to_fraction_string(value) for value in vector)


def decode_vector(text, label="vector"):
    if text == "":
        return ()
    return tuple(parse_fraction_string(part, label) for part in text.split(","))


def encode_names(names):
    return "\x1f".join(names)


def decode_names(text):
    return tuple(text.split("\x1f")) if text else ()


def find_measurement_elsewhere(connection, dataset, principal, snapshot, ident):
    """Find a raw measurement identity owned by another snapshot."""
    return connection.execute(
        "SELECT snapshot FROM measurements"
        " WHERE dataset=? AND principal=? AND id=? AND snapshot<>?",
        (dataset, principal, ident, snapshot),
    ).fetchone()


def find_alias_elsewhere(connection, release_id, query_id, dataset, principal, snapshot):
    """Locate an alias outside the requested scope, for a clear conflict error."""
    return connection.execute(
        "SELECT dataset, principal, snapshot FROM queries"
        " WHERE release_id=? AND query_id=?"
        " AND NOT (dataset=? AND principal=? AND snapshot=?)",
        (release_id, query_id, dataset, principal, snapshot),
    ).fetchone()


def encode_lineage(lineage):
    return ",".join("%s=%s" % (raw_id, to_fraction_string(value)) for raw_id, value in lineage)


def decode_lineage(text, label="lineage"):
    if text == "":
        return ()
    result = []
    for part in text.split(","):
        raw_id, _, value = part.partition("=")
        result.append((raw_id, parse_fraction_string(value, label)))
    return tuple(result)


# ---------------------------------------------------------------------------
# init


def initialise(connection, limits):
    """Create the immutable (dataset, principal) limits."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT initialized FROM init_marker WHERE id=1").fetchone()
        if row is not None:
            existing = {
                (r["dataset"], r["principal"]): (r["epsilon"], r["delta"])
                for r in connection.execute("SELECT * FROM limits")
            }
            proposed = {
                key: (to_fraction_string(value[0]), to_fraction_string(value[1]))
                for key, value in limits.items()
            }
            if existing != proposed:
                raise ReleaseValidationError("limits are immutable after initialisation")
            connection.execute("COMMIT")
            return False
        for key, value in sorted(limits.items()):
            connection.execute(
                "INSERT INTO limits(dataset, principal, epsilon, delta) VALUES (?,?,?,?)",
                (key[0], key[1], to_fraction_string(value[0]), to_fraction_string(value[1])),
            )
        connection.execute("INSERT INTO init_marker(id, initialized) VALUES (1, 1)")
        connection.execute("COMMIT")
        return True
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def read_limits(connection):
    return {
        (row["dataset"], row["principal"]): (
            parse_fraction_string(row["epsilon"]),
            parse_fraction_string(row["delta"]),
        )
        for row in connection.execute("SELECT * FROM limits")
    }


# ---------------------------------------------------------------------------
# scope reads


def load_view(connection, dataset, principal, snapshot):
    domain_row = connection.execute(
        "SELECT dimensions FROM domains WHERE dataset=? AND principal=? AND snapshot=?",
        (dataset, principal, snapshot),
    ).fetchone()
    dimensions = None
    if domain_row is not None:
        dimensions = decode_names(domain_row["dimensions"])
    measurements = {}
    retired = set()
    for row in connection.execute(
        "SELECT * FROM measurements WHERE dataset=? AND principal=? AND snapshot=?",
        (dataset, principal, snapshot),
    ):
        measurements[row["id"]] = Measurement(
            id=row["id"],
            coefficients=decode_vector(row["coefficients"]),
            value=parse_fraction_string(row["value"]),
            variance=parse_fraction_string(row["variance"]),
            epsilon=parse_fraction_string(row["epsilon"]),
            delta=parse_fraction_string(row["delta"]),
        )
        if row["retired"]:
            retired.add(row["id"])
    queries = {}
    for row in connection.execute(
        "SELECT * FROM queries WHERE dataset=? AND principal=? AND snapshot=?",
        (dataset, principal, snapshot),
    ):
        queries[(row["release_id"], row["query_id"])] = PublishedQuery(
            id=row["query_id"],
            coefficients=decode_vector(row["coefficients"]),
            value=parse_fraction_string(row["value"]),
            lineage=decode_lineage(row["lineage"]),
        )
    return SnapshotView(
        measurements=measurements,
        retired=frozenset(retired),
        queries=queries,
        dimensions=dimensions,
    )


def load_budget(connection, dataset, principal):
    row = connection.execute(
        "SELECT * FROM limits WHERE dataset=? AND principal=?", (dataset, principal)
    ).fetchone()
    if row is None:
        return None
    spent_epsilon = Fraction(0)
    spent_delta = Fraction(0)
    for measurement in connection.execute(
        "SELECT epsilon, delta FROM measurements WHERE dataset=? AND principal=?",
        (dataset, principal),
    ):
        spent_epsilon += parse_fraction_string(measurement["epsilon"])
        spent_delta += parse_fraction_string(measurement["delta"])
    return ScopeBudget(
        max_epsilon=parse_fraction_string(row["epsilon"]),
        max_delta=parse_fraction_string(row["delta"]),
        spent_epsilon=spent_epsilon,
        spent_delta=spent_delta,
    )


def load_release(connection, release_id):
    return connection.execute(
        "SELECT * FROM releases WHERE release_id=?", (release_id,)
    ).fetchone()


def release_scope(connection, release_id):
    row = load_release(connection, release_id)
    if row is None:
        return None
    return (row["dataset"], row["principal"], row["snapshot"])
