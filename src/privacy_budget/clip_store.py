"""SQLite storage for the microdata clipping producer and consumer.

The producer database holds the complete committed source state, the currently
selected clipped rows, the contiguous sequence cursor, and every immutable
delivery packet.  The consumer database is physically separate and holds only
applied clipped rows, the chain cursor and receipts; it never reads producer
state.

Both sides commit source/clip/sequence/packet (producer) or rows/sequence/
receipt (consumer) inside one ``BEGIN IMMEDIATE`` transaction.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3

from .clip_model import ClipConfig, ClipError, Row, config_sha256

COMMON_TABLES = """
CREATE TABLE IF NOT EXISTS clip_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    role TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    max_partitions INTEGER NOT NULL,
    max_rows INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    last_packet_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clip_rows (
    id TEXT PRIMARY KEY,
    unit TEXT NOT NULL,
    partition_name TEXT NOT NULL,
    priority INTEGER NOT NULL,
    value INTEGER NOT NULL
);
"""

PRODUCER_TABLES = COMMON_TABLES + """
CREATE TABLE IF NOT EXISTS source_rows (
    id TEXT PRIMARY KEY,
    unit TEXT NOT NULL,
    partition_name TEXT NOT NULL,
    priority INTEGER NOT NULL,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS packets (
    sequence INTEGER PRIMARY KEY,
    batch_sha256 TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL,
    packet_json TEXT NOT NULL
);
"""

CONSUMER_TABLES = COMMON_TABLES + """
CREATE TABLE IF NOT EXISTS receipts (
    sequence INTEGER PRIMARY KEY,
    packet_sha256 TEXT NOT NULL,
    after_rows_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
"""

# Tables owned by the unrelated v2 release catalog and the influence fixture.
# A clipping database must never silently appropriate an existing database.
FOREIGN_TABLES = {
    "limits",
    "domains",
    "measurements",
    "queries",
    "releases",
    "init_marker",
    "source_records",
}

ZERO_DIGEST = "0" * 64


def _symlink_in_chain(path):
    """True if the path itself or any existing ancestor is a symbolic link."""
    path = Path(path)
    if path.is_symlink():
        return True
    for parent in path.parents:
        if parent.is_symlink():
            return True
        if parent == parent.parent:
            break
    return False


def check_paths(database, inputs=(), outputs=()):
    """Reject symlinked database/output paths and any aliasing by real path.

    Inputs may themselves be symlinks (they are only read) but every path is
    compared by real path, so an input can never alias the database or output.
    Database and output paths, including their existing parent directories,
    may not be symbolic links.
    """
    database = Path(database)
    if _symlink_in_chain(database):
        raise ClipError("database path cannot traverse a symlink")
    seen = {database.resolve()}
    for item in inputs:
        item = Path(item)
        if not item.exists():
            raise ClipError("input does not exist: " + str(item))
        resolved = item.resolve()
        if not resolved.is_file():
            raise ClipError("input must be a regular file: " + str(item))
        seen.add(resolved)
    for item in outputs:
        item = Path(item)
        if _symlink_in_chain(item):
            raise ClipError("output path cannot traverse a symlink")
        resolved = item.resolve()
        if resolved in seen:
            raise ClipError("output must differ from inputs and database")
        seen.add(resolved)
    input_paths = [Path(item).resolve() for item in inputs]
    if len(set(input_paths)) != len(input_paths):
        raise ClipError("input paths must be distinct")
    if database.resolve() in input_paths:
        raise ClipError("input and database/output paths must differ")


def check_paths(database, inputs=(), outputs=()):
    """Reject symlinked database/output paths and any aliasing by real path.

    Inputs may themselves be symlinks (they are only read), but every path is
    compared by real path so an input cannot alias the database or an output.
    Database and output paths, including their parent directories, may not be
    symbolic links.
    """
    database = Path(database)
    if _symlink_in_chain(database):
        raise ClipError("database path cannot traverse a symlink")
    seen = {database.resolve()}
    for item in inputs:
        item = Path(item)
        if not item.exists():
            raise ClipError("input does not exist: " + str(item))
        resolved = item.resolve()
        if not resolved.is_file():
            raise ClipError("input must be a regular file: " + str(item))
        seen.add(resolved)
    for item in outputs:
        item = Path(item)
        if _symlink_in_chain(item):
            raise ClipError("output path cannot traverse a symlink")
        resolved = item.resolve()
        if resolved in seen:
            raise ClipError("output must differ from inputs and database")
        seen.add(resolved)
    input_paths = [Path(item).resolve() for item in inputs]
    if len(set(input_paths)) != len(input_paths):
        raise ClipError("input paths must be distinct")
    database_path = database.resolve()
    if database_path in input_paths:
        raise ClipError("input and database/output paths must differ")


def _open_uri(path):
    return "file:" + str(Path(path).resolve()) + "?mode=ro"


def connect(path, role):
    """Open an existing clipping database without altering its config."""
    path = Path(path)
    if _symlink_in_chain(path):
        raise ClipError("database path cannot traverse a symlink")
    if not path.exists():
        raise ClipError("clipping database does not exist")
    connection = sqlite3.connect(str(path.resolve()), timeout=60, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    try:
        meta = connection.execute("SELECT role FROM clip_meta WHERE id=1").fetchone()
    except sqlite3.DatabaseError:
        connection.close()
        raise ClipError("database is not a clipping database")
    if meta is None:
        connection.close()
        raise ClipError("clipping database is not initialised")
    if meta["role"] != role:
        connection.close()
        raise ClipError("database role is %s, expected %s" % (meta["role"], role))
    return connection


def open_readonly(path, role):
    path = Path(path)
    if _symlink_in_chain(path):
        raise ClipError("database path cannot traverse a symlink")
    if not path.exists():
        raise ClipError("clipping database does not exist")
    connection = sqlite3.connect(_open_uri(path), uri=True, timeout=60, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    try:
        meta = connection.execute("SELECT role FROM clip_meta WHERE id=1").fetchone()
    except sqlite3.DatabaseError:
        connection.close()
        raise ClipError("database is not a clipping database")
    if meta is None or meta["role"] != role:
        connection.close()
        raise ClipError("database is not a %s clipping database" % role)
    return connection


def _create_tables(connection, role):
    script = PRODUCER_TABLES if role == "producer" else CONSUMER_TABLES
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def initialise(path, role, config: ClipConfig):
    """Create a fresh clipping database; identical retries are no-ops.

    The inspection, DDL and config insert share one ``BEGIN IMMEDIATE``
    transaction (SQLite DDL is transactional), so two processes racing on a
    fresh path cannot both insert the config, and a rejected config leaves no
    tables behind.
    """
    path = Path(path)
    if _symlink_in_chain(path):
        raise ClipError("database path cannot traverse a symlink")
    connection = sqlite3.connect(str(path.resolve()), timeout=60, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if names & FOREIGN_TABLES:
                raise ClipError("cannot reuse an existing unrelated database")
            stored = None
            if "clip_meta" in names:
                stored = connection.execute("SELECT * FROM clip_meta WHERE id=1").fetchone()
            if stored is None and names:
                raise ClipError("cannot reuse an unrelated existing database")
            if stored is not None:
                expected = (
                    role,
                    config.stream_id,
                    config_sha256(config),
                    config.max_partitions,
                    config.max_rows,
                )
                actual = (
                    stored["role"],
                    stored["stream_id"],
                    stored["config_sha256"],
                    stored["max_partitions"],
                    stored["max_rows"],
                )
                if actual != expected:
                    raise ClipError("clipping configuration is immutable")
                connection.execute("COMMIT")
                return False
            _create_tables(connection, role)
            connection.execute(
                "INSERT INTO clip_meta"
                "(id,role,stream_id,config_sha256,max_partitions,max_rows,sequence,last_packet_sha256)"
                " VALUES (1,?,?,?,?,?,0,?)",
                (
                    role,
                    config.stream_id,
                    config_sha256(config),
                    config.max_partitions,
                    config.max_rows,
                    ZERO_DIGEST,
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return True


def load_meta(connection):
    row = connection.execute("SELECT * FROM clip_meta WHERE id=1").fetchone()
    if row is None:
        raise ClipError("clipping database is not initialised")
    return row


def _load_table(connection, table):
    rows = {}
    for row in connection.execute(
        "SELECT id, unit, partition_name, priority, value FROM " + table
    ):
        rows[row["id"]] = Row(
            id=row["id"],
            unit=row["unit"],
            partition=row["partition_name"],
            priority=row["priority"],
            value=row["value"],
        )
    return rows


def load_rows(connection):
    return _load_table(connection, "clip_rows")


def load_source(connection):
    return _load_table(connection, "source_rows")


def replace_rows(connection, table, rows):
    connection.execute("DELETE FROM " + table)
    connection.executemany(
        "INSERT INTO " + table + "(id,unit,partition_name,priority,value) VALUES (?,?,?,?,?)",
        [
            (row.id, row.unit, row.partition, row.priority, row.value)
            for row in rows.values()
        ],
    )
