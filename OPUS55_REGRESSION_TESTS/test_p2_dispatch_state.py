"""PRIORITY 2 - two-phase dispatch, cancellation, crash and quota edge cases (dummy only).

`test_matrix_*` record observed behaviour (sender calls, SQLite state, quota, retry).
`test_regress_*` assert the SAFE behaviour; a failure on the baseline is the defect proof.
"""
import asyncio
import json
import os
import sqlite3
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

from act_manager import policy
from conftest import CHAT_ADS, CHAT_ADS_2, CHAT_ADS_3, NOW
from opus55_helpers import (SendRecorder, dispatch, human_approve, later, prep, raw_db,
                            run_child, status)

MATRIX = []


def _record(name, **kw):
    MATRIX.append({"case": name, **kw})
    out = os.environ.get("OPUS55_MATRIX_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"case": name, **kw}, default=str) + "\n")


def _ad_slots_used(paths):
    con = raw_db(paths)
    n = con.execute("SELECT COUNT(*) FROM posts WHERE kind='ad' AND status IN "
                    "('sending','sent','scheduled','uncertain')").fetchone()[0]
    con.close()
    return n


# --------------------------------------------------------------------------- preflight phase
@pytest.mark.parametrize("exc, expect", [
    (policy.PolicyError("identity mismatch"), policy.PolicyError),
    (ConnectionError("dns"), policy.PolicyError),
    (asyncio.CancelledError(), asyncio.CancelledError),
])
def test_matrix_preflight_failure_rolls_back(act_paths, exc, expect):
    post = prep(act_paths)
    human_approve(act_paths, post)
    sender = SendRecorder()

    async def preflight(item):
        raise exc

    with pytest.raises(expect):
        dispatch(act_paths, post["id"], sender, preflight=preflight)
    st = status(act_paths, post["id"])
    _record("preflight:" + type(exc).__name__, sender_calls=len(sender.calls), sqlite=st,
            ad_slots=_ad_slots_used(act_paths), retry_eligible=(st == "approved"))
    assert st == "approved" and not sender.calls and _ad_slots_used(act_paths) == 0
    async def ok_preflight(item):
        return {}
    assert dispatch(act_paths, post["id"], sender, preflight=ok_preflight)["status"] == "sent"
    assert len(sender.calls) == 1


# --------------------------------------------------------------------------- send phase
@pytest.mark.parametrize("exc", [TimeoutError("drop"), policy.PolicyError("late"),
                                 ConnectionResetError(), asyncio.CancelledError(),
                                 KeyboardInterrupt()])
def test_matrix_sender_failure_is_uncertain_never_retried(act_paths, exc):
    post = prep(act_paths)
    human_approve(act_paths, post)
    sender = SendRecorder(exc=exc)

    async def preflight(item):
        return {}

    with pytest.raises(BaseException) as info:
        dispatch(act_paths, post["id"], sender, preflight=preflight)
    st = status(act_paths, post["id"])
    _record("sender:" + type(exc).__name__, raised=type(info.value).__name__,
            sender_calls=len(sender.calls), sqlite=st, ad_slots=_ad_slots_used(act_paths),
            retry_eligible=False)
    assert st == "uncertain" and len(sender.calls) == 1
    with pytest.raises(policy.PolicyError, match="unused"):
        dispatch(act_paths, post["id"], SendRecorder(), preflight=preflight)


def test_matrix_cancellation_inside_sender_surfaces_as_policyerror(act_paths):
    """Observed: CancelledError during send is converted to PolicyError (cancel not propagated)."""
    post = prep(act_paths)
    human_approve(act_paths, post)
    sender = SendRecorder(delay=5)

    async def preflight(item):
        return {}

    async def run():
        task = asyncio.ensure_future(policy.execute(post["id"], sender, preflight=preflight,
                                                    config_path=act_paths[0], db_path=act_paths[1], now=NOW))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except BaseException as e:  # noqa: BLE001
            return e

    err = asyncio.run(run())
    _record("sender:task.cancel()", raised=type(err).__name__, sender_calls=len(sender.calls),
            sqlite=status(act_paths, post["id"]))
    assert status(act_paths, post["id"]) == "uncertain"
    assert isinstance(err, policy.PolicyError)


# --------------------------------------------------------------------------- process death
_CHILD = textwrap.dedent("""
    import asyncio, os, sys
    from pathlib import Path
    from datetime import datetime, timezone
    from act_manager import policy
    cfg, db, draft, phase, marker = sys.argv[1:6]
    async def preflight(item):
        if phase == "preflight":
            os._exit(9)
        return {}
    async def sender(item, prepared):
        with open(marker, "a") as fh:
            fh.write("SEND\\n")
        if phase == "after_send":
            os._exit(9)
        return 4242
    asyncio.run(policy.execute(draft, sender, preflight=preflight, config_path=Path(cfg),
                               db_path=Path(db), now=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)))
""")


@pytest.mark.parametrize("phase", ["preflight", "after_send"])
def test_matrix_process_kill_leaves_sending_forever(act_paths, tmp_path, phase):
    post = prep(act_paths)
    human_approve(act_paths, post)
    marker = tmp_path / "sends.txt"
    child = run_child(_CHILD, act_paths[0], act_paths[1], post["id"], phase, marker)
    sends = marker.read_text().count("SEND") if marker.exists() else 0
    st = status(act_paths, post["id"])
    # restart: new process tries again
    child2 = run_child(_CHILD, act_paths[0], act_paths[1], post["id"], "none", marker)
    sends2 = marker.read_text().count("SEND") if marker.exists() else 0
    with pytest.raises(policy.PolicyError):
        policy.cancel(post["id"], db_path=act_paths[1])
    _record("os._exit during " + phase, child_rc=child.returncode, sender_calls=sends, sqlite=st,
            ad_slots=_ad_slots_used(act_paths), restart_sender_calls=sends2 - sends,
            operator_cancel_possible=False)
    assert child.returncode == 9 and st == "sending"
    assert sends2 == sends  # never re-sent after restart
    assert _ad_slots_used(act_paths) == 1  # quota slot consumed even when nothing was sent


_RACE = textwrap.dedent("""
    import asyncio, sys, time
    from pathlib import Path
    from datetime import datetime, timezone
    from act_manager import policy
    cfg, db, draft, marker, start_at = sys.argv[1:6]
    while time.time() < float(start_at):
        time.sleep(0.001)
    async def preflight(item):
        await asyncio.sleep(0.05)
        return {}
    async def sender(item, prepared):
        with open(marker, "a") as fh:
            fh.write("SEND\\n")
        return 1
    try:
        asyncio.run(policy.execute(draft, sender, preflight=preflight, config_path=Path(cfg),
                    db_path=Path(db), now=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)))
        print("OK")
    except policy.PolicyError as e:
        print("DENIED", e)
""")


def test_matrix_eight_processes_race_one_send(act_paths, tmp_path):
    import subprocess
    import sys
    import time
    post = prep(act_paths)
    human_approve(act_paths, post)
    marker = tmp_path / "race.txt"
    start = time.time() + 2.0
    procs = [subprocess.Popen([sys.executable, "-c", _RACE, str(act_paths[0]), str(act_paths[1]),
                               post["id"], str(marker), str(start)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
             for _ in range(8)]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    sends = marker.read_text().count("SEND")
    _record("8-process race", sender_calls=sends, ok=sum(o.startswith("OK") for o in outs),
            sqlite=status(act_paths, post["id"]))
    assert sends == 1 and sum(o.startswith("OK") for o in outs) == 1


def test_matrix_finalize_commit_failure_after_successful_send(act_paths, monkeypatch):
    post = prep(act_paths)
    human_approve(act_paths, post)
    sender = SendRecorder()
    real = policy.finalize

    def failing_finalize(*a, **kw):
        if kw.get("status") in ("sent", "scheduled"):
            raise sqlite3.OperationalError("database or disk is full")
        return real(*a, **kw)

    monkeypatch.setattr(policy, "finalize", failing_finalize)

    async def preflight(item):
        return {}

    with pytest.raises(Exception) as info:
        dispatch(act_paths, post["id"], sender, preflight=preflight)
    st = status(act_paths, post["id"])
    _record("finalize SQLITE_FULL after send", sender_calls=len(sender.calls), sqlite=st,
            raised=type(info.value).__name__, message=str(info.value)[:120])
    assert st == "sending" and len(sender.calls) == 1
    return info.value


def test_regress_finalize_failure_after_send_says_do_not_retry(act_paths, monkeypatch):
    err = test_matrix_finalize_commit_failure_after_successful_send(act_paths, monkeypatch)
    assert isinstance(err, policy.PolicyError) and "do NOT retry" in str(err)


def test_matrix_unreserve_failure_leaves_sending(act_paths, monkeypatch):
    post = prep(act_paths)
    human_approve(act_paths, post)
    monkeypatch.setattr(policy, "unreserve", lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")))

    async def preflight(item):
        raise ConnectionError("no network")

    with pytest.raises(sqlite3.OperationalError):
        dispatch(act_paths, post["id"], SendRecorder(), preflight=preflight)
    _record("unreserve fails after preflight error", sqlite=status(act_paths, post["id"]),
            sender_calls=0, ad_slots=_ad_slots_used(act_paths))
    assert status(act_paths, post["id"]) == "sending"


# --------------------------------------------------------------------------- quota / spacing / time
def test_matrix_riyadh_midnight_boundary(act_paths):
    sender = SendRecorder()
    # 23:30 Riyadh on 18 Oct == 20:30 UTC ; 00:30 Riyadh 19 Oct == 21:30 UTC (exactly 60 min)
    a = prep(act_paths, content="late", schedule_at="2026-10-18T23:30:00+03:00")
    human_approve(act_paths, a)
    dispatch(act_paths, a["id"], sender)
    b = prep(act_paths, chat_id=CHAT_ADS_2, content="after midnight", schedule_at="2026-10-19T00:30:00+03:00")
    human_approve(act_paths, b)
    assert dispatch(act_paths, b["id"], sender)["status"] == "scheduled"
    c = prep(act_paths, chat_id=CHAT_ADS_3, content="59 min", schedule_at="2026-10-19T01:29:00+03:00")
    human_approve(act_paths, c)
    with pytest.raises(policy.PolicyError, match="60 minutes"):
        dispatch(act_paths, c["id"], sender)
    for i, hh in enumerate(("03", "05")):
        p = prep(act_paths, chat_id=CHAT_ADS_3, content=f"d2 {i}", schedule_at=f"2026-10-19T{hh}:00:00+03:00")
        human_approve(act_paths, p)
        dispatch(act_paths, p["id"], sender)
    d = prep(act_paths, content="4th on 19th", schedule_at="2026-10-19T08:00:00+03:00")
    human_approve(act_paths, d)
    with pytest.raises(policy.PolicyError, match="quota"):
        dispatch(act_paths, d["id"], sender)
    _record("riyadh midnight", sender_calls=len(sender.calls), note="60m exactly allowed; 59m refused; 3/day")


def test_matrix_cli_approval_accepts_stale_schedule_but_reserve_blocks(act_paths):
    """OP-07 (not patched): approve() checks a stale schedule only when `now` is passed (CLI never does).

    reserve() still refuses, so nothing is sent. Recorded, not fixed: the existing suite freezes
    time in fixtures and calls approve() without `now`, so changing this would alter test semantics.
    """
    from opus55_helpers import unkeyed_integrity
    post = prep(act_paths, schedule_at=later(minutes=5).isoformat())
    con = raw_db(act_paths)
    past = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    con.execute("UPDATE posts SET schedule_utc=?, integrity_hash=? WHERE id=?",
                (past, unkeyed_integrity(post["id"], CHAT_ADS, "ad", None, post["content"], past), post["id"]))
    con.close()
    policy.approve(post["id"], db_path=act_paths[1])  # CLI call shape: no `now`
    sender = SendRecorder()
    with pytest.raises(policy.PolicyError, match="too near/past"):
        dispatch(act_paths, post["id"], sender)
    _record("approve stale schedule (CLI shape)", approve="accepted", reserve="refused", sender_calls=len(sender.calls))
    assert not sender.calls


def test_regress_cli_approval_binds_schedule(act_paths, monkeypatch):
    """Schedule rewritten (with a recomputed unkeyed hash) while the operator reads the preview."""
    import builtins
    import io
    import sys
    import act_cli
    from opus55_helpers import unkeyed_integrity
    post = prep(act_paths, schedule_at=later(hours=2).isoformat())
    monkeypatch.setattr(policy, "CONFIG_PATH", act_paths[0])
    monkeypatch.setattr(policy, "DATA_PATH", act_paths[1])
    fake = io.StringIO()
    fake.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake)
    moved = later(days=6).isoformat(timespec="seconds")

    def operator_types_phrase(prompt=""):
        con = raw_db(act_paths)  # agent edits the row between preview and confirmation
        con.execute("UPDATE posts SET schedule_utc=?, planned_utc=?, integrity_hash=? WHERE id=?",
                    (moved, moved, unkeyed_integrity(post["id"], CHAT_ADS, "ad", None, post["content"], moved),
                     post["id"]))
        con.close()
        return f"APPROVE {post['id']}"

    monkeypatch.setattr(builtins, "input", operator_types_phrase)
    try:
        act_cli.main(["approve", post["id"]])
    except SystemExit:
        pass
    assert status(act_paths, post["id"]) == "draft", "approval accepted a schedule the operator never saw"


def test_regress_null_integrity_hash_is_refused(act_paths):
    post = prep(act_paths)
    evil = "changed after preview"
    con = raw_db(act_paths)
    con.execute("UPDATE posts SET content=?, fingerprint=?, integrity_hash=NULL WHERE id=?",
                (evil, policy.compute_fingerprint(CHAT_ADS, "ad", None, evil), post["id"]))
    con.close()
    with pytest.raises(policy.PolicyError, match="integrity"):
        policy.approve(post["id"], db_path=act_paths[1])


def test_regress_trigger_cannot_swap_content_between_verification_and_send(act_paths):
    """Human approves text A; a planted trigger rewrites it when status becomes 'sending'."""
    post = prep(act_paths, content="Text A approved by the operator")
    human_approve(act_paths, post)
    con = raw_db(act_paths)
    con.execute("""CREATE TRIGGER swap AFTER UPDATE OF status ON posts
        WHEN NEW.status='sending' BEGIN
        UPDATE posts SET content='Text B never shown to the operator' WHERE id=NEW.id; END""")
    con.close()
    sender = SendRecorder()
    try:
        dispatch(act_paths, post["id"], sender)
    except policy.PolicyError:
        pass
    sent = [c["content"] for c in sender.calls]
    _record("trigger swap at reserve", sent=sent)
    assert sent in ([], ["Text A approved by the operator"])


def test_regress_database_with_planted_trigger_is_refused(act_paths):
    prep(act_paths)
    con = raw_db(act_paths)
    con.execute("CREATE TRIGGER t AFTER INSERT ON posts BEGIN SELECT 1; END")
    con.close()
    with pytest.raises(policy.PolicyError, match="trigger"):
        prep(act_paths, content="second")


def test_regress_open_draft_cap_is_enforced_when_configured(act_paths):
    cfg = json.loads(act_paths[0].read_text(encoding="utf-8"))
    cfg["max_open_drafts"] = 5
    act_paths[0].write_text(json.dumps(cfg), encoding="utf-8")
    for i in range(5):
        prep(act_paths, content=f"d{i}")
    with pytest.raises(policy.PolicyError, match="max_open_drafts"):
        prep(act_paths, content="d5")
    first = policy.list_posts(db_path=act_paths[1], limit=50)[-1]
    policy.cancel(first["id"], db_path=act_paths[1])  # operator frees one slot
    assert prep(act_paths, content="d6")["status"] == "draft"


_CAP_WRITER = textwrap.dedent("""
    import sys
    from pathlib import Path
    from act_manager import policy
    cfg, db, tag = sys.argv[1:4]
    ok = refused = 0
    for i in range(40):
        try:
            policy.prepare(chat_id=-1001111111111, kind="ad", content=f"{tag}-{i}",
                           config_path=Path(cfg), db_path=Path(db))
            ok += 1
        except policy.PolicyError:
            refused += 1
    print(ok, refused)
""")


def test_regress_open_draft_cap_holds_under_concurrent_processes(act_paths):
    import subprocess
    import sys
    cfg = json.loads(act_paths[0].read_text(encoding="utf-8"))
    cfg["max_open_drafts"] = 50
    act_paths[0].write_text(json.dumps(cfg), encoding="utf-8")
    prep(act_paths, content="seed")
    procs = [subprocess.Popen([sys.executable, "-c", _CAP_WRITER, str(act_paths[0]), str(act_paths[1]), f"p{k}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
             for k in range(4)]
    outs = [p.communicate(timeout=300)[0].split() for p in procs]
    con = raw_db(act_paths)
    open_rows = con.execute("SELECT COUNT(*) FROM posts WHERE status IN ('draft','approved')").fetchone()[0]
    con.close()
    _record("open-draft cap 50, 4 procs x 40", open_rows=open_rows, per_proc=outs)
    assert open_rows == 50
