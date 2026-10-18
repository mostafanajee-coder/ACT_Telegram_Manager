"""Opus 5.5 round-3 adversarial tests. Dummy data only; no credentials; no Telegram.

Environment is set before any telegram_mcp import so runtime builds an
unconnected dummy client. The offline_guard plugin blocks real sockets.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DUMMY_DIR = Path(tempfile.mkdtemp(prefix="opus55_dummy_"))
os.environ["TELEGRAM_API_ID"] = "12345"
os.environ["TELEGRAM_API_HASH"] = "dummy_hash_not_a_secret"
os.environ.pop("TELEGRAM_SESSION_STRING", None)
os.environ["TELEGRAM_SESSION_NAME"] = str(_DUMMY_DIR / "dummy_offline")
os.environ["TELEGRAM_ALIASES_FILE"] = str(_DUMMY_DIR / "aliases.json")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)  # 12:00 Riyadh
CHAT_ADS = -1001111111111
CHAT_ADS_2 = -1001111111112
CHAT_ADS_3 = -1001111111113
CHAT_REPLIES_ONLY = -1002222222222
EXPECTED_USER = 7000001


@pytest.fixture
def act_paths(tmp_path):
    cfg = tmp_path / "act_config.json"
    db = tmp_path / "act_data" / "posts.sqlite3"
    cfg.write_text(json.dumps({
        "expected_user_id": EXPECTED_USER,
        "daily_ad_limit": 3,
        "allowed_chats": [
            {"chat_id": CHAT_ADS, "label": "Dummy ads group A", "ads_allowed": True},
            {"chat_id": CHAT_ADS_2, "label": "Dummy ads group B", "ads_allowed": True},
            {"chat_id": CHAT_ADS_3, "label": "Dummy ads group C", "ads_allowed": True},
            {"chat_id": CHAT_REPLIES_ONLY, "label": "Dummy no-ads group", "ads_allowed": False},
        ],
    }), encoding="utf-8")
    return cfg, db
