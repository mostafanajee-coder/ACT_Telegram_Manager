"""Windows-specific path and NTFS ACL evidence. Touches ONLY pytest tmp dummy directories.

No real ACL, user, or service is modified. Junctions are created inside tmp_path only.
"""

import json
import os
import subprocess

import pytest

from act_manager import policy
from opus55_helpers import prep

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows NTFS evidence")


def test_regress_db_parent_junction_is_refused(act_paths, tmp_path):
    """Baseline `_resolve_db_path` checks is_symlink(); an NTFS junction (no admin needed) passed."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "junction_data"
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, text=True
    )
    if r.returncode != 0:
        pytest.skip("mklink /J unavailable: " + r.stderr)
    assert not link.is_symlink()  # Python 3.11: junction is not reported as symlink
    paths = (act_paths[0], link / "posts.sqlite3")
    try:
        prep(paths)
    except policy.PolicyError:
        pass
    assert not (real / "posts.sqlite3").exists(), "DB silently redirected through a reparse point"


def test_evidence_act_data_acl_is_inherited_not_hardened(act_paths):
    """policy._db() applies chmod 600 on POSIX only; on NTFS the ACL is whatever the parent grants."""
    prep(act_paths)
    db = act_paths[1]
    out = subprocess.run(["icacls", str(db)], capture_output=True, text=True).stdout
    report = os.environ.get("OPUS55_MATRIX_OUT")
    if report:
        with open(report, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"case": "NTFS ACL of new posts.sqlite3 (tmp)", "icacls": out.strip()})
                + "\n"
            )
    assert "(I)" in out  # every ACE inherited; no explicit protection applied by the code
