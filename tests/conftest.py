import os
import tempfile

os.environ["BT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["BT_COOKIE_SECURE"] = "false"
os.environ["BT_ICS_TOKEN"] = "testtoken123"
os.environ["BT_CHECK_INTERVAL_MIN"] = "999"
os.environ["BT_RATE_LIMIT_PER_MIN"] = "100000"
os.environ["BT_DB_KEY"] = "ab" * 32
os.environ["BT_BASE_URL"] = "https://bills.test"
os.environ["BT_REQUIRE_CHANNEL"] = "false"  # legacy tests talk plaintext; channel tests flip it on
