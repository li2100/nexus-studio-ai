import os, sqlite3, subprocess
ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "studio.db")
MEDIA = os.path.join(ROOT, "data", "media")
THUMBS = os.path.join(MEDIA, "thumbs")
FF = os.environ.get("FFMPEG_PATH", "ffmpeg")
os.makedirs(THUMBS, exist_ok=True)

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
rows = c.execute("SELECT id,idx,episode_id,video_url FROM shots WHERE video_url LIKE '%mp4'").fetchall()
made = 0
for r in rows:
    rel = r["video_url"].split("/media/")[-1]
    src = os.path.join(MEDIA, rel)
    if not os.path.isfile(src):
        continue
    out = os.path.join(THUMBS, f"shot{r['idx']:02d}_{r['id']}.jpg")
    if not os.path.isfile(out):
        try:
            subprocess.run([FF, "-y", "-ss", "0.15", "-i", src, "-frames:v", "1",
                            "-vf", "scale=512:-2", out], check=True, timeout=90,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("fail", r["idx"], e)
            continue
    if os.path.getsize(out) > 2000:
        c.execute("UPDATE shots SET thumb_url=? WHERE id=?",
                  ("/media/thumbs/" + os.path.basename(out), r["id"]))
        made += 1
c.commit()
print("thumbs", made, "/", len(rows))
