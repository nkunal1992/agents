"""#257 multi-install sharing — the unit-level half.

Separate IDE installs run separate daemons over the SAME index files, so
the contracts tested here are cross-connection ones: the strict schema
gate (version-skewed install refuses instead of misreading), WAL epoch
visibility (one install's publish is the other's next query), the
job_lease fence (one worker across installs), and writer contention
(BEGIN IMMEDIATE retries must outlast a sibling's publish txn).

The process-level half lives in tests/e2e/sessions/test_multi_install.py
(two real daemons, two daemon homes, one workdir).
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from confluence_knowledge.index import Index
from confluence_knowledge.settings import SetupError


def _fresh_index(tmp_path) -> Index:
    idx = Index(tmp_path / "scope.db")
    idx.meta_set("root_path", "/x")
    return idx


# ---- strict schema gate ------------------------------------------------

def test_schema_match_opens_readonly(tmp_path):
    _fresh_index(tmp_path).close()
    Index(tmp_path / "scope.db", readonly=True).close()


def test_newer_schema_refuses_on_both_paths(tmp_path):
    _fresh_index(tmp_path).close()
    con = sqlite3.connect(tmp_path / "scope.db")
    con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    con.commit()
    con.close()
    for ro in (False, True):
        with pytest.raises(SetupError, match="same package version"):
            Index(tmp_path / "scope.db", readonly=ro)


def test_pre_versioning_schema_refuses(tmp_path):
    _fresh_index(tmp_path).close()
    con = sqlite3.connect(tmp_path / "scope.db")
    con.execute("DELETE FROM meta WHERE key='schema_version'")
    con.commit()
    con.close()
    with pytest.raises(SetupError, match="predates schema versioning"):
        Index(tmp_path / "scope.db")


def test_tables_without_meta_refuses(tmp_path):
    con = sqlite3.connect(tmp_path / "scope.db")
    con.execute("CREATE TABLE junk (x)")
    con.commit()
    con.close()
    for ro in (False, True):
        with pytest.raises(SetupError, match="predates schema versioning"):
            Index(tmp_path / "scope.db", readonly=ro)


def test_empty_file_refuses_readonly(tmp_path):
    (tmp_path / "scope.db").touch()
    with pytest.raises(SetupError, match="holds no schema"):
        Index(tmp_path / "scope.db", readonly=True)


# ---- cross-connection sharing ------------------------------------------

def test_sibling_publish_is_next_query_visible(tmp_path):
    """Index B (the 'other install') must see A's publish on its very next
    search_cache() — the epoch read inside every call is the freshness
    guarantee the multi-IDE story rests on."""
    a = _fresh_index(tmp_path)
    b = Index(tmp_path / "scope.db", readonly=True)
    c1 = b.search_cache()
    a.upsert_page({"page_id": "p1", "title": "t", "url": "u",
                   "version": "1", "content_hash": "h"},
                  [], None, None)
    a.publish({"p1"})
    c2 = b.search_cache()
    assert c2.epoch > c1.epoch
    a.close()
    b.close()


def test_job_lease_one_worker_across_installs(tmp_path):
    a = _fresh_index(tmp_path)
    b = Index(tmp_path / "scope.db", readonly=False)
    import os
    assert a.acquire_lease("job1", "daemonA:host:1:aa", os.getpid())
    # sibling install's daemon must not start a second worker on the job
    assert not b.acquire_lease("job1", "daemonB:host:2:bb", os.getpid())
    a.release_lease("job1", "daemonA:host:1:aa")
    assert b.acquire_lease("job1", "daemonB:host:2:bb", os.getpid())
    a.close()
    b.close()


def test_concurrent_publishers_no_lock_errors(tmp_path):
    """Two writer connections racing upsert+publish — WAL + the widened
    BEGIN IMMEDIATE retry budget must absorb the contention instead of
    surfacing 'database is locked' to the sibling install's user."""
    path = tmp_path / "scope.db"
    errs: list[Exception] = []

    def hammer(tag: int) -> None:
        idx = Index(path)
        try:
            for i in range(8):
                pid = f"p{tag}:{i}"
                idx.upsert_page({"page_id": pid, "title": "t", "url": "u",
                                 "version": "1", "content_hash": "h"},
                                [], None, None)
                idx.meta_set(f"m{tag}:{i}", "1")
                idx.publish({pid})
        except Exception as e:      # noqa: BLE001 — collected for assert
            errs.append(e)
        finally:
            idx.close()

    ts = [threading.Thread(target=hammer, args=(t,)) for t in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert not errs, [f"{type(e).__name__}: {e}" for e in errs]
