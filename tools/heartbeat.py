"""Record that a scheduled job finished: python tools/heartbeat.py <job>. The daily check reports jobs that go quiet."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import db, heartbeat

if __name__ == "__main__":
    conn = db()
    cur = conn.cursor()
    heartbeat(cur, sys.argv[1] if len(sys.argv) > 1 else "unknown")
    conn.commit()
    conn.close()
    print("heartbeat", sys.argv[1:])
