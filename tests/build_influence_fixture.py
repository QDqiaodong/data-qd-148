"""Build the synthetic SQLite microdata for the influence audit sample.

Run once to (re)create a small, entirely fictional database next to the query
fixture::

    python tests/build_influence_fixture.py fixtures/influence-source.db

The database contains ordinary tables only; the auditor opens it read-only.
"""
import sqlite3
import sys
from pathlib import Path

PEOPLE = [
    # pid, unit (NULL = public row), city, age
    ("p1", "U", "x", 30),
    ("p2", "U", "x", 31),
    ("p3", "V", "y", 40),
    ("p4", None, "x", 25),
]

PURCHASES = [
    # purchase_id, unit, pid, amount, tag
    ("c1", "U", "p1", 10, "a"),
    ("c2", "U", "p1", 20, "a"),
    ("c3", "V", "p3", 50, "b"),
    ("c4", "W", "p2", 7, None),
]


def build(path):
    path = Path(path)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE people ("
            "pid TEXT NOT NULL UNIQUE, unit TEXT, city TEXT NOT NULL, age INTEGER)"
        )
        connection.execute(
            "CREATE TABLE purchases ("
            "purchase_id TEXT NOT NULL UNIQUE, unit TEXT, pid TEXT NOT NULL, "
            "amount INTEGER, tag TEXT)"
        )
        connection.executemany("INSERT INTO people VALUES (?,?,?,?)", PEOPLE)
        connection.executemany("INSERT INTO purchases VALUES (?,?,?,?,?)", PURCHASES)
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "fixtures/influence-source.db"
    build(target)
    print("wrote", target)
