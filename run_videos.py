import json, time, urllib.request, sys
B = "http://127.0.0.1:18081"


def get(p):
    return json.loads(urllib.request.urlopen(B + p, timeout=20).read().decode())


def post(p, b):
    r = urllib.request.Request(B + p, data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())


st = get("/api/state?project=1")
pid = st["project"]["id"]
eid = st["shots"][0]["episode_id"]
shots = [s for s in st["shots"] if s["idx"] >= 2]
print("待生成", len(shots), "镜", flush=True)
for s in shots:
    cur = get("/api/state?project=1")
    shot = [x for x in cur["shots"] if x["id"] == s["id"]][0]
    if shot["status"] == "done" and shot.get("video_url", "").endswith(".mp4"):
        print(f"跳过 {shot['idx']}（已有真视频）", flush=True)
        continue
    j = post("/api/generate/video", {"project_id": pid, "episode_id": eid, "shot_id": s["id"]})
    jid = j["job_id"]
    print(f"第{shot['idx']}镜 job {jid} 开始", flush=True)
    t0 = time.time()
    while True:
        time.sleep(15)
        cur = get("/api/state?project=1")
        job = [x for x in cur["jobs"] if x["id"] == jid]
        if not job:
            break
        job = job[0]
        if job["status"] in ("done", "failed"):
            print(f"第{shot['idx']}镜 {job['status']} 用时 {int(time.time() - t0)}s", flush=True)
            break
        if int(time.time() - t0) > 2700:
            print(f"第{shot['idx']}镜 超时 45min，进入下一镜", flush=True)
            break
print("全部镜头生成队列结束", flush=True)
