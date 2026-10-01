"""测试环境：在导入 app 之前固定配置（临时数据库、不种演示数据、启用审核令牌）。"""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="flood-test-")
os.environ.setdefault("FLOOD_DB_PATH", os.path.join(_TMP, "test.db"))
os.environ.setdefault("FLOOD_SEED_DEMO", "false")
os.environ.setdefault("FLOOD_REVIEW_TOKEN", "secret")
