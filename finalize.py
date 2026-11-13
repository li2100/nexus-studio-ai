"""跑完 2-18 镜之后收尾：补拍第 1 镜 → 导出 HTML 合集 → ffmpeg 合成 MP4。"""
import json, time, urllib.request

B = "http://127.0.0.1:18081"


def req(path, body=None, method="GET", timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


st = req("/api/state?project=1")
shot1 = [s for s in st["shots"] if s["idx"] == 1][0]
if shot1["status"] == "done" and shot1.get("video_url", "").endswith(".mp4") and not shot1.get("flags", {}).get("gen_stale"):
    print("第1镜已是最新，跳过补拍", flush=True)
else:
    j = req("/api/generate/video", {"project_id": 1, "episode_id": shot1["episode_id"], "shot_id": shot1["id"]}, "POST")
    jid = j["job_id"]
    print("补拍第1镜 job", jid, flush=True)
    t0 = time.time()
    while True:
        time.sleep(15)
        cur = req("/api/state?project=1")
        job = [x for x in cur["jobs"] if x["id"] == jid]
        if not job:
            break
        job = job[0]
        if job["status"] in ("done", "failed"):
            print("第1镜", job["status"], int(time.time() - t0), "s", flush=True)
            break
        if time.time() - t0 > 2700:
            print("第1镜补拍超时", flush=True)
            break

for path, tag in (("/api/reel/export", "HTML"), ("/api/reel/ffmpeg", "MP4")):
    try:
        r = req(path, {"project_id": 1, "episode_id": 1}, "POST", timeout=600)
        print(tag, "→", json.dumps(r, ensure_ascii=False)[:300], flush=True)
    except Exception as e:
        print(tag, "失败：", str(e)[:150], flush=True)
print("收尾完成", flush=True)
