import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "studio.db")
c = sqlite3.connect(db)
n = c.execute("UPDATE shots SET status='', error=NULL, thumb_url=NULL "
              "WHERE episode_id=1 AND (video_url IS NULL OR video_url='') "
              "AND status IN ('preview','failed')").rowcount
c.commit()
print("reset", n)
