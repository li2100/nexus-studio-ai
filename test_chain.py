# -*- coding: utf-8 -*-
"""轻量链路自检：不触发 GPU（不调 ④ 出视频），只验 ①→②→③ + 级联失效 + 资产图接口存在。
用法： python test_chain.py [--base http://127.0.0.1:18081]
"""
import json, sys, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:18081"
if "--base" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base") + 1]


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(path + " → " + e.read().decode()[:200])


def wait(jid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        st = call("/api/state")
        jobs = [j for j in st["jobs"] if j["id"] == jid]
        if not jobs:
            return None
        if jobs[0]["status"] in ("done", "failed"):
            return jobs[0]
    return None


results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  OK  " if ok else "  FAIL") + " | " + name + ("  " + detail if detail else ""), flush=True)


print("1) 新建项目并锁目标", flush=True)
st = call("/api/projects", "POST", {
    "title": "自检_链路", "genre": "萌娃团宠", "ratio": "9:16", "episode_count": 1,
    "episode_length": "75-95 秒", "style_lock": "冷调写实 + 暖光收尾；35mm 与 85mm 交替",
    "premise": "五岁半的阿棠在雨夜被亲妈丢在门口，靠半块糖和一沓旧照片把家人一个个找回来。",
    "forbidden": "血腥、换装、换脸、真实品牌、字幕硬字",
})
pid = st["project"]["id"]
ep = call("/api/state?project=%d" % pid)["episodes"]
eid = ep[0]["id"] if ep else call("/api/episodes", "POST", {"project_id": pid, "idx": 1, "title": "第1集"})["id"]
call("/api/projects/%d/lock" % pid, "POST", {})
check("项目建成 + 目标锁定", True, "pid=%d eid=%d" % (pid, eid))

print("2) 跑 ① 大纲 → 正文 → 剧本 → 分镜", flush=True)
for kind, label in (("outline", "大纲"), ("novel", "正文"), ("script", "剧本"), ("board", "分镜")):
    j = call("/api/generate/%s" % kind, "POST", {"project_id": pid, "episode_id": eid})["job_id"]
    r = wait(j, 240)
    check("① %s" % label, bool(r) and r["status"] == "done", (r or {}).get("status", "timeout"))

print("3) 跑 ② 调度 → ③ 提示词", flush=True)
for kind, label in (("blocking", "调度"), ("prompt", "提示词")):
    j = call("/api/generate/%s" % kind, "POST", {"project_id": pid, "episode_id": eid})["job_id"]
    r = wait(j, 240)
    check("② %s" % label if kind == "blocking" else "③ %s" % label,
          bool(r) and r["status"] == "done", (r or {}).get("status", "timeout"))

st = call("/api/state?project=%d" % pid)
shots = st["shots"]
check("分镜有镜头且都拿到 ③ 提示词", len(shots) > 0 and all(s["prompt_cn"] for s in shots),
      "%d 镜" % len(shots))
check("③ 记录 sig2_at_prompt", all(s.get("sig2_at_prompt") for s in shots))
check("④ 全部标记需生成", all(s["flags"]["gen_stale"] for s in shots))

print("4) 改 ① 资产 canon → 检验 ②③④ 级联失效", flush=True)
char = next((a for a in st["assets"] if a["kind"] == "char"), None)
if not char:
    check("资产库有角色", False); sys.exit(1)
old = char["canon"]
call("/api/assets/%d" % char["id"], "POST", {"canon": old + "（自检改写）"})
st2 = call("/api/state?project=%d" % pid)
used = [s for s in st2["shots"] if char["name"] in (s.get("chars_list") or [])]
check("资产 canon 被写入", next(a for a in st2["assets"] if a["id"] == char["id"])["canon"].endswith("（自检改写）"))
check("用到该角色的镜头 ② 失效", all(s["flags"]["block_stale"] for s in used), "%d 镜" % len(used))
check("同批镜头 ③④ 也失效（链式传播）", all(s["flags"]["prompt_stale"] and s["flags"]["gen_stale"] for s in used))
check("H3 提交原文含硬约束（有参考图时还必须含图号映射）",
      all("【硬约束】" in (s["h3_prompt"] or "") and
          (s.get("h3_refs", 0) == 0 or "【参考图锁定（按顺序）】" in (s["h3_prompt"] or ""))
          for s in used),
      "%d 镜，参考图 %d" % (len(used), (used[0].get("h3_refs") if used else 0)))

print("5) 重跑 ② → ③，验证失效被清掉", flush=True)
for kind in ("blocking", "prompt"):
    j = call("/api/generate/%s" % kind, "POST", {"project_id": pid, "episode_id": eid})["job_id"]
    r = wait(j, 240)
    check("重跑 %s" % kind, bool(r) and r["status"] == "done", (r or {}).get("status", "timeout"))
st3 = call("/api/state?project=%d" % pid)
used3 = [s for s in st3["shots"] if char["name"] in (s.get("chars_list") or [])]
check("② 失效已清零", all(not s["flags"]["block_stale"] for s in used3))
check("③ 失效已清零", all(not s["flags"]["prompt_stale"] for s in used3))
check("④ 仍需生成（提示词变过，旧片作废）", all(s["flags"]["gen_stale"] for s in used3))
call("/api/assets/%d" % char["id"], "POST", {"canon": old})
st4 = call("/api/state?project=%d" % pid)
used4 = [s for s in st4["shots"] if char["name"] in (s.get("chars_list") or [])]
check("把 canon 改回去也会再次触发失效", all(s["flags"]["block_stale"] for s in used4))

print("6) 资产图接口存在性（不触发 GPU）", flush=True)
kinds_now = sorted(set(a["kind"] for a in st4["assets"]))
check("资产库按三类分组", len(kinds_now) >= 2, ",".join(kinds_now))
check("/api/state 资产带 images[] 与 chosen_url",
      all("images" in a and "chosen_url" in a for a in st4["assets"]))
for a in st4["assets"][:1]:
    check("资产有 canon 可手改", bool(a["canon"]))

print("7) 清理自检项目", flush=True)
call("/api/projects/%d" % pid, "DELETE")
print("")
bad = [r for r in results if not r[1]]
print("通过 %d / %d" % (len(results) - len(bad), len(results)))
if bad:
    for b in bad:
        print(" 失败：", b[0], b[2])
    sys.exit(1)
print("链路自检全部通过（未触发 GPU 生成）")
