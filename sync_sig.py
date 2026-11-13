import json, sqlite3, urllib.request, hashlib, os

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "studio.db")
BASE = "http://127.0.0.1:18081"


def sha(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:32]


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
before = c.execute("SELECT script_json FROM episodes WHERE idx=1").fetchone()["script_json"]
post("/api/generate/script", {"project_id": 1, "episode_id": 1})
import time
time.sleep(3)
c2 = sqlite3.connect(DB)
c2.row_factory = sqlite3.Row
row = c2.execute("SELECT id,script_json,script_sig,novel_text FROM episodes WHERE idx=1").fetchone()
after = row["script_json"]
same = (before or "") == (after or "")
print("script same:", same, "len", len(after or ""))
if same:
    c2.execute("UPDATE episodes SET board_sig=? WHERE id=?", (sha(after), row["id"]))
    c2.execute("UPDATE episodes SET script_sig=? WHERE id=?", (sha(row["novel_text"]), row["id"]))
    c2.commit()
    print("cleared stale flags")
else:
    # 剧本确实变了：把分镜标记为需重跑是对的，不动
    print("script changed -> board stays stale")
