# -*- coding: utf-8 -*-
"""AI 视频创作系统 · 主服务（纯标准库，双击即起，无需 pip）

流水线：
  ① 资产 / 分镜描述 / 对白  →  ② 场面调度  →  ③ 视频提示词  →  ④ 生成视频  →  合集

级联失效（本系统的核心）：
  ① 的任何字段（分镜描述、对白、角色/场景/道具锁定特征、风格锁）改动
      → ② 自动标记「需重跑」→ ③「需重跑」→ ④「需重跑」
  每一级都存了生成当时的输入签名；签名不一致就判定过期，不靠"手动记得"。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine
import frames

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "3.0"                       # 纯版本号，用于新旧比对
APP_LABEL = "3.0 · PRD 对齐版"             # 界面展示用版本名
# 远端版本清单（托管在你自己能公网访问的位置，返回 JSON：
#   {"latest":"3.1","note":"本次更新说明","qq":"229611621"}）。
# 留空 "" = 未配置：不联网检查、不弹更新提示；填好地址后，旧版本打开网站才会收到更新提示。
UPDATE_MANIFEST_URL = ""
DATA_DIR = os.path.join(BASE_DIR, "data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
STATIC_DIR = os.path.join(BASE_DIR, "static")
WORKFLOW_DIR = os.path.join(BASE_DIR, "workflows")
DB_PATH = os.path.join(DATA_DIR, "studio.db")
PORT = int(os.environ.get("STUDIO_PORT", "18081"))

for d in (DATA_DIR, MEDIA_DIR, EXPORT_DIR, WORKFLOW_DIR):
    os.makedirs(d, exist_ok=True)

_local = threading.local()
_db_lock = threading.RLock()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT, premise TEXT, genre TEXT, platform TEXT, audience TEXT,
  ratio TEXT DEFAULT '9:16', episode_count INTEGER DEFAULT 12,
  episode_length TEXT, style_lock TEXT, forbidden TEXT,
  target_locked INTEGER DEFAULT 0, target_note TEXT,
  outline_sig TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
  dialogue_lang TEXT DEFAULT 'zh', prompt_mode TEXT DEFAULT 'cn',
  fps INTEGER DEFAULT 30, quality TEXT DEFAULT '电影级',
  duration_range TEXT DEFAULT '75–95s', avoid_tpl TEXT DEFAULT '标准版',
  video_wf TEXT DEFAULT 'director', viseme_pack TEXT DEFAULT '普通话'
);
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, idx INTEGER DEFAULT 1,
  title TEXT, outline_json TEXT DEFAULT '', novel_text TEXT DEFAULT '',
  script_json TEXT DEFAULT '', outline_sig TEXT DEFAULT '', novel_sig TEXT DEFAULT '',
  script_sig TEXT DEFAULT '', board_sig TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
  archived INTEGER DEFAULT 0, srt_text TEXT DEFAULT '', export_url TEXT DEFAULT '',
  novel_manual INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
  kind TEXT, name TEXT, canon TEXT, image_url TEXT, locked INTEGER DEFAULT 0,
  prompt_maker TEXT DEFAULT '',
  sort INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
  gen_workflow TEXT DEFAULT 'qwen_image2512', view_mode TEXT DEFAULT 'four',
  voice_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, episode_id INTEGER,
  idx INTEGER DEFAULT 1, duration INTEGER DEFAULT 5, shot_size TEXT, camera TEXT,
  movement TEXT, scene_slug TEXT, chars TEXT DEFAULT '[]', props TEXT DEFAULT '[]',
  action TEXT, emotion TEXT, dialogue_json TEXT DEFAULT '[]', transition TEXT,
  blocking_json TEXT DEFAULT '', prompt_cn TEXT DEFAULT '', prompt_en TEXT DEFAULT '',
  prompt_neg TEXT DEFAULT '', sig1_at_block TEXT DEFAULT '', sig2_at_prompt TEXT DEFAULT '',
  sig3_at_gen TEXT DEFAULT '', refs_json TEXT DEFAULT '', locked INTEGER DEFAULT 0,
  status TEXT DEFAULT 'empty', video_url TEXT DEFAULT '', thumb_url TEXT DEFAULT '',
  error TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
  res_tier TEXT DEFAULT '', ref_prev INTEGER DEFAULT 0, perf_json TEXT DEFAULT '',
  perf_lock INTEGER DEFAULT 0, prompt_lock INTEGER DEFAULT 0,
  seed INTEGER DEFAULT -1, parts_json TEXT DEFAULT '', packet_json TEXT DEFAULT '',
  workflow TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, episode_id INTEGER,
  kind TEXT, scope TEXT, status TEXT DEFAULT 'queued', progress INTEGER DEFAULT 0,
  message TEXT DEFAULT '', log TEXT DEFAULT '[]', result TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS asset_images(
  id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER, project_id INTEGER,
  url TEXT, mode TEXT DEFAULT 'single', prompt TEXT DEFAULT '', seed INTEGER DEFAULT 0,
  chosen INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, kind TEXT, message TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS wf_presets(
  key TEXT PRIMARY KEY, name TEXT, sub TEXT, wf_file TEXT DEFAULT '',
  prefix_cn TEXT DEFAULT '', prefix_en TEXT DEFAULT '', neg_extra TEXT DEFAULT '',
  shot_rule TEXT DEFAULT '', params_json TEXT DEFAULT '{}', sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS music(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, kind TEXT DEFAULT 'bgm',
  name TEXT, style TEXT, mood TEXT, duration INTEGER DEFAULT 30, bpm INTEGER DEFAULT 90,
  prompt TEXT, url TEXT DEFAULT '', status TEXT DEFAULT 'draft', job_id INTEGER DEFAULT 0,
  linked_ep INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS voices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, asset_id INTEGER DEFAULT 0,
  name TEXT, engine TEXT DEFAULT 'local', sample_url TEXT DEFAULT '',
  params_json TEXT DEFAULT '{}', viseme TEXT DEFAULT '', created_at TEXT, updated_at TEXT
);
"""

DEFAULT_SETTINGS = {
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_api_key": "",
    "llm_model": "deepseek-chat",
    "engine_mode": "auto",          # auto | llm | local
    "comfy_base": "http://127.0.0.1:8188",
    # 以下为本机 ComfyUI 安装路径，默认留空；部署时在「系统设置」里按本机实际填写
    "comfy_input_dir": "",
    "comfy_output_dir": "",
    # ComfyUI 本体：平台启动/守护时用它自动拉起（端口不通就清僵尸再起）
    "comfy_python": "",
    "comfy_yaml": "",
    "video_workflow": "",
    "builtin_video": "h3_r2v",       # h3_r2v：内置 MiniMax H3 参考生视频（无需 API 格式 json）
    "video_steps": "8",             # H3 采样步数（4 步 LoRA 时改 4，速度翻倍）
    "video_scale": "1.0",           # 分辨率缩放（0.6 = 快速出片验证用）
    # ffmpeg 可执行文件路径（抽帧 / 合成 MP4 用），默认留空；部署时在设置里填写本机 ffmpeg 路径
    "ffmpeg_path": "",
    # —— PRD 十：四类接口（视频/图片默认本地 ComfyUI，可切线上；音乐固定本地 ComfyUI）
    "video_api_mode": "local",       # local | volc | bailian
    "video_api_base": "", "video_api_key": "",
    "image_api_mode": "local",
    "image_api_base": "", "image_api_key": "",
    "music_api_mode": "local",       # 固定 local（PRD 不允许线上音乐服务商）
    "music_workflow": "",            # 本地 ComfyUI 音乐工作流文件名（留空=未配置，禁止假装出声）
    "music_api_base": "",
    # 默认配置（PRD 十·默认配置）
    "default_text_model": "",        # 小说/剧本/分镜默认文本模型
    "default_image_wf": "qwen_image2512",
    "image_concurrency": "1", "video_concurrency": "1",
    "tts_base": "", "tts_key": "",   # 配音通道（留空=未接入，绝不假装配音）
    # 系统设置页（对齐参考产品）：供应商选择 + 文本模型高级参数 + 默认服务
    "llm_provider": "deepseek",
    "llm_temperature": "0.7", "llm_max_tokens": "4096",
    "default_novel_model": "", "default_script_model": "", "default_board_model": "",
    "default_video_service": "local", "default_image_service": "local", "default_music_service": "local",
    "seeded": "0",
}


# PRD 六：9 条视频工作流，每条后台自带一套独立预设（风格前缀 / 负面词 / 镜头规范 / 参数）
WF_PRESETS = [
    ("official_ref", "官方参考图生视频", "官方基线 · 参考图锁脸锁景，最稳", "h3_r2v",
     "严格按参考图还原人物五官、发型、服装与场景结构；不额外加风格化修饰",
     "faithful to reference images, exact facial identity, no style drift",
     "画面文字, 水印, 换脸, 变形脸", "默认景别 中景-近景｜运镜 固定或极缓推｜单镜 4-10s",
     '{"steps": 8, "denoise": 0.55, "ip_weight": 0.85}', 1),
    ("second_pass", "二采工作流", "二次采样 · 补细节、提清晰度", "h3_r2v",
     "二次采样补细节：皮肤纹理真实、发丝清晰、布料有织纹；不改变已定人物与场景",
     "second-pass refinement, crisp micro detail, no identity change",
     "画面文字, 水印, 涂抹感, 塑料皮肤", "默认景别 近景｜运镜 固定｜单镜 4-8s",
     '{"steps": 6, "denoise": 0.35, "ip_weight": 0.9}', 2),
    ("action", "武戏工作流", "打斗 / 追逐 · 动作连贯不抽搐", "h3_r2v",
     "武戏节奏：出手快、收势稳，动作连贯不断帧，打击点有顿感；人物不糊不飘",
     "martial arts action, continuous motion, no limb warping",
     "画面文字, 水印, 肢体扭曲, 多余肢体", "默认景别 全景-中景｜运镜 跟随/手持｜单镜 3-6s",
     '{"steps": 8, "denoise": 0.6, "ip_weight": 0.8}', 3),
    ("director", "导演台工作流", "文戏 / 情绪 / 对话 · 短剧日常首选", "h3_r2v",
     "导演台文戏：镜头稳、表情自然、说话有呼吸；机位不飘，人物占画比例稳定",
     "stable dialogue coverage, natural micro expression, steady frame",
     "画面文字, 水印, 夸张表情, 频繁变焦", "默认景别 中近景-近景｜运镜 固定或微推｜单镜 4-10s",
     '{"steps": 8, "denoise": 0.5, "ip_weight": 0.9}', 4),
    ("fast_transition", "快节奏转场工作流", "冲突反转 / 打脸高光 · 节奏快", "h3_r2v",
     "快节奏：短切、抢拍、镜头有推进感；情绪落点干净利落，不拖尾",
     "fast paced coverage, snappy cuts, punchy emotion beat",
     "画面文字, 水印, 拖影, 糊帧", "默认景别 近景-特写｜运镜 快速推/甩｜单镜 2-4s",
     '{"steps": 6, "denoise": 0.55, "ip_weight": 0.85}', 5),
    ("slow_mood", "慢镜头氛围感工作流", "流泪 / 对视 / 回忆 / 温情虐心", "h3_r2v",
     "慢镜头氛围：时间被拉长，微小动作放大，光晕柔化，情绪悬在半空",
     "slow motion poetic mood, soft bloom, suspended emotion",
     "画面文字, 水印, 过曝, 死黑", "默认景别 特写-近景｜运镜 极缓推/环绕｜单镜 6-12s",
     '{"steps": 8, "denoise": 0.45, "ip_weight": 0.9}', 6),
    ("portrait", "特写人像工作流", "脸部微表情 / 眼神戏 · 防崩脸", "h3_r2v",
     "特写人像：五官结构严格锁死，眼神有焦点，微表情只动眼周与嘴角，禁止换脸",
     "extreme close-up portrait, locked facial structure, micro expression only",
     "画面文字, 水印, 崩脸, 五官错位, 双眼不对称", "默认景别 特写-大特写｜运镜 固定｜单镜 3-6s",
     '{"steps": 10, "denoise": 0.4, "ip_weight": 1.0}', 7),
    ("landscape", "大场景远景工作流", "空镜 / 外景大远景 / 城镇山水", "h3_r2v",
     "大场景远景：地平线水平、透视正确、大气层次分明；画面里不出现无关主角",
     "wide establishing shot, correct perspective, atmospheric depth",
     "画面文字, 水印, 畸变, 建筑扭曲", "默认景别 全景-大全景｜运镜 横移/升降｜单镜 4-8s",
     '{"steps": 8, "denoise": 0.55, "ip_weight": 0.6}', 8),
    ("dynamic_comic", "动态漫顺滑工作流", "漫剧动作连贯 · 减少抽搐变形", "h3_r2v",
     "动态漫：线稿干净、色块平涂、动作逐帧顺滑；不做写实皮肤与实拍景深",
     "smooth 2D motion comic, clean lineart, flat color, no flicker",
     "画面文字, 水印, 抽搐, 线条抖动, 变形", "默认景别 中景-近景｜运镜 平移/推｜单镜 3-8s",
     '{"steps": 8, "denoise": 0.5, "ip_weight": 0.75}', 9),
]


def seed_wf_presets(conn):
    for row in WF_PRESETS:
        conn.execute(
            "INSERT OR IGNORE INTO wf_presets(key,name,sub,wf_file,prefix_cn,prefix_en,neg_extra,shot_rule,params_json,sort)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", row)
    conn.commit()


def wf_preset(key):
    key = (key or "").strip() or "director"
    row = db().execute("SELECT * FROM wf_presets WHERE key=?", (key,)).fetchone()
    return rowdict(row) if row else None


def wf_of(project, shot=None):
    """单镜头优先，其次项目默认；都不填 → 导演台。"""
    key = ((shot or {}).get("workflow") or "").strip() or ((project or {}).get("video_wf") or "").strip()
    p = wf_preset(key)
    if p:
        return p
    return wf_preset("director") or {"key": "director", "name": "导演台工作流", "prefix_cn": "", "neg_extra": "",
                                     "shot_rule": "", "params_json": "{}"}


def apply_wf_to_prompt(p, wf):
    """把视频工作流的风格前缀/镜头规范/负面词压进提示词（PRD 六·全链路绑定）。"""
    if not wf:
        return p
    pre = (wf.get("prefix_cn") or "").strip()
    rule = (wf.get("shot_rule") or "").strip()
    pre_en = (wf.get("prefix_en") or "").strip()
    neg = (wf.get("neg_extra") or "").strip()
    cn = p.get("cn") or ""
    if pre and pre not in cn:
        cn = "【视频工作流：%s】%s｜%s\n%s" % (wf.get("name", ""), pre, rule, cn)
    en = p.get("en") or ""
    if pre_en and pre_en not in en:
        en = "%s. %s" % (pre_en, en)
    negs = [x.strip() for x in (p.get("neg") or "").split(",") if x.strip()]
    for x in [y.strip() for y in neg.split(",") if y.strip()]:
        if x not in negs:
            negs.append(x)
    return {"cn": cn, "en": en, "neg": ", ".join(negs)}


def wf_params(wf):
    try:
        return json.loads((wf or {}).get("params_json") or "{}")
    except Exception:
        return {}


def apply_wf_to_board(shots, wf):
    """分镜层套用工作流的镜头规范：景别/时长区间按预设收口（单镜已手改的不动）。"""
    if not wf or not shots:
        return shots, 0
    rule = (wf.get("shot_rule") or "")
    fixed = 0
    if "特写" in rule:
        for s in shots:
            if "特写" not in (s.get("shot_size") or ""):
                s["shot_size"] = "近景" if "近景" in rule else "特写"
                fixed += 1
                break
    if "全景" in rule and "大全景" in rule:
        for s in shots:
            if s.get("shot_size") in ("中景", "中近景"):
                s["shot_size"] = "全景"
                fixed += 1
    params = wf_params(wf)
    cap = int(params.get("max_sec") or 0)
    if cap:
        for s in shots:
            if int(s.get("duration") or 5) > cap:
                s["duration"] = cap
                fixed += 1
    return shots, fixed


def init_db():
    with _db_lock:
        c = db()
        c.executescript(SCHEMA)
        # 轻量迁移：老库补列（不影响已有数据）
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(shots)")]
            if "refs_json" not in cols:
                c.execute("ALTER TABLE shots ADD COLUMN refs_json TEXT DEFAULT ''")
            if "res_tier" not in cols:
                c.execute("ALTER TABLE shots ADD COLUMN res_tier TEXT DEFAULT ''")
            if "ref_prev" not in cols:
                c.execute("ALTER TABLE shots ADD COLUMN ref_prev INTEGER DEFAULT 0")
            for col, ddl in (("perf_json", "ALTER TABLE shots ADD COLUMN perf_json TEXT DEFAULT ''"),
                             ("perf_lock", "ALTER TABLE shots ADD COLUMN perf_lock INTEGER DEFAULT 0"),
                             ("prompt_lock", "ALTER TABLE shots ADD COLUMN prompt_lock INTEGER DEFAULT 0")):
                if col not in cols:
                    c.execute(ddl)
            pcols = [r[1] for r in c.execute("PRAGMA table_info(projects)")]
            for col, ddl in (
                    ("dialogue_lang", "ALTER TABLE projects ADD COLUMN dialogue_lang TEXT DEFAULT 'zh'"),
                    ("prompt_mode", "ALTER TABLE projects ADD COLUMN prompt_mode TEXT DEFAULT 'cn'"),
                    ("fps", "ALTER TABLE projects ADD COLUMN fps INTEGER DEFAULT 30"),
                    ("quality", "ALTER TABLE projects ADD COLUMN quality TEXT DEFAULT '电影级'"),
                    ("duration_range", "ALTER TABLE projects ADD COLUMN duration_range TEXT DEFAULT '75–95s'"),
                    ("avoid_tpl", "ALTER TABLE projects ADD COLUMN avoid_tpl TEXT DEFAULT '标准版'"),
                    ("video_wf", "ALTER TABLE projects ADD COLUMN video_wf TEXT DEFAULT 'director'"),
                    ("viseme_pack", "ALTER TABLE projects ADD COLUMN viseme_pack TEXT DEFAULT '普通话'")):
                if col not in pcols:
                    c.execute(ddl)
            acols = [r[1] for r in c.execute("PRAGMA table_info(assets)")]
            for col, ddl in (
                    ("prompt_maker", "ALTER TABLE assets ADD COLUMN prompt_maker TEXT DEFAULT ''"),
                    ("gen_workflow", "ALTER TABLE assets ADD COLUMN gen_workflow TEXT DEFAULT 'qwen_image2512'"),
                    ("view_mode", "ALTER TABLE assets ADD COLUMN view_mode TEXT DEFAULT 'four'"),
                    ("voice_id", "ALTER TABLE assets ADD COLUMN voice_id INTEGER DEFAULT 0")):
                if col not in acols:
                    c.execute(ddl)
            scols = [r[1] for r in c.execute("PRAGMA table_info(shots)")]
            for col, ddl in (
                    ("seed", "ALTER TABLE shots ADD COLUMN seed INTEGER DEFAULT -1"),
                    ("parts_json", "ALTER TABLE shots ADD COLUMN parts_json TEXT DEFAULT ''"),
                    ("packet_json", "ALTER TABLE shots ADD COLUMN packet_json TEXT DEFAULT ''"),
                    ("workflow", "ALTER TABLE shots ADD COLUMN workflow TEXT DEFAULT ''")):
                if col not in scols:
                    c.execute(ddl)
            ecols = [r[1] for r in c.execute("PRAGMA table_info(episodes)")]
            for col, ddl in (("archived", "ALTER TABLE episodes ADD COLUMN archived INTEGER DEFAULT 0"),
                             ("srt_text", "ALTER TABLE episodes ADD COLUMN srt_text TEXT DEFAULT ''"),
                             ("export_url", "ALTER TABLE episodes ADD COLUMN export_url TEXT DEFAULT ''"),
                             ("novel_manual", "ALTER TABLE episodes ADD COLUMN novel_manual INTEGER DEFAULT 0")):
                if col not in ecols:
                    c.execute(ddl)
        except Exception:
            pass
        c.execute("""CREATE TABLE IF NOT EXISTS shot_chat(
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, shot_id INTEGER,
            role TEXT, text TEXT, created_at TEXT)""")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
            if v:
                c.execute("UPDATE settings SET value=? WHERE key=? AND (value IS NULL OR value='')", (v, k))
        seed_wf_presets(c)
        c.commit()


# ---------------------------------------------------------------- settings

def get_setting(key, default=""):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    with _db_lock:
        db().execute("INSERT INTO settings(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        db().commit()


# ---------------------------------------------------------------- LLM

_llm_state = {"ok": None, "msg": "未检测", "ts": 0}


def fetch_model_list(base, key, provider=""):
    """按 OpenAI 兼容 /models 拉取可用模型列表（DeepSeek/火山/百炼/Ollama/vLLM 通用）。"""
    base = (base or "").strip().rstrip("/")
    if not base:
        return False, "未填写接口地址", []
    if not base.endswith("/v1") and "api/v3" not in base and "compatible-mode" not in base:
        pass
    req = urllib.request.Request(base + "/models")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:120]), []
    items = payload.get("data") or payload.get("models") or []
    models = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            models.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                models.append(str(mid))
    models = sorted(set(models))
    if not models:
        return False, "接口返回为空（%s/models 无可用模型）" % base, []
    return True, "获取到 %d 个可用模型" % len(models), models


def llm_available():
    key = get_setting("llm_api_key")
    if not key:
        return False, "未配置 API Key（已启用本地引擎）"
    if get_setting("engine_mode") == "local":
        return False, "已手动切换为本地引擎"
    ts = _llm_state["ts"]
    if time.time() - ts < 300 and _llm_state["ok"] is not None:
        return _llm_state["ok"], _llm_state["msg"]
    base = get_setting("llm_base_url").rstrip("/")
    model = get_setting("llm_model")
    try:
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}],
                           "max_tokens": 4}).encode()
        req = urllib.request.Request(base + "/chat/completions", data=body,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            json.loads(r.read().decode("utf-8"))
        _llm_state.update({"ok": True, "msg": f"模型在线 · {model}", "ts": time.time()})
        return True, _llm_state["msg"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:160]
        msg = f"模型不可用 {e.code}（{detail.strip()}）→ 已启用本地引擎"
        _llm_state.update({"ok": False, "msg": msg, "ts": time.time()})
        return False, msg
    except Exception as e:
        _llm_state.update({"ok": False, "msg": f"连不上模型：{e} → 已启用本地引擎", "ts": time.time()})
        return False, _llm_state["msg"]


def llm_json(system: str, user: str, max_tokens=6000):
    """要求模型返回 JSON；失败抛异常，由上层回退本地引擎。"""
    base = get_setting("llm_base_url").rstrip("/")
    key = get_setting("llm_api_key")
    model = get_setting("llm_model")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.75, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    m = re.search(r"\{[\s\S]*\}", text)
    return json.loads(m.group(0) if m else text)


def llm_text(system: str, user: str, max_tokens=4000) -> str:
    base = get_setting("llm_base_url").rstrip("/")
    key = get_setting("llm_api_key")
    model = get_setting("llm_model")
    body = json.dumps({"model": model,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}],
                       "temperature": 0.8, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------- ComfyUI

_comfy_state = {"ok": None, "msg": "", "ts": 0, "version": ""}


def comfy_status(force=False):
    if not force and time.time() - _comfy_state["ts"] < 10 and _comfy_state["ok"] is not None:
        return _comfy_state
    base = get_setting("comfy_base").rstrip("/")
    # 关键：ComfyUI 在采样时 HTTP 会被 GPU 拖住甚至超时，但端口是活的。
    # 所以先用 TCP 连接判生死，再用 /system_stats 补版本号，超时不算离线。
    alive, ver, note = False, "unknown", ""
    try:
        host = urllib.parse.urlparse(base).hostname or "127.0.0.1"
        port = urllib.parse.urlparse(base).port or 8188
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        alive = True
    except Exception as e:
        note = type(e).__name__
    if alive:
        try:
            with urllib.request.urlopen(base + "/system_stats", timeout=6) as r:
                d = json.loads(r.read().decode("utf-8"))
            ver = (d.get("system") or {}).get("comfyui_version", "unknown")
        except Exception:
            ver = "busy"
    _comfy_state.update({"ok": alive, "version": ver, "ts": time.time(),
                         "msg": ("ComfyUI 在线" if alive else f"ComfyUI 离线（端口不通 {note}）")})
    return _comfy_state


def _find_comfy_root(venv_python, yaml_hint):
    """从 venv python 反推 ComfyUI 安装根目录。"""
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(venv_python))))
        if os.path.exists(os.path.join(root, "ComfyUI", "main.py")):
            return root
    except Exception:
        pass
    return ""


def _comfy_log(msg):
    try:
        with open(os.path.join(DATA_DIR, "comfy.log"), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


_comfy_lock = threading.Lock()


def _kill_comfy():
    """清掉卡死的 ComfyUI 进程（只认 ComfyUI\\main.py，不动别的 python）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object {$_.CommandLine -match 'ComfyUI\\\\main\\.py'} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, timeout=40)
    except Exception:
        pass


def ensure_comfy(wait=200, revive=True):
    """ComfyUI 必须跟平台一起活着：端口不通就先清僵尸再拉起，最多等 wait 秒。"""
    st = comfy_status(force=True)
    if st["ok"] or not revive:
        return st
    with _comfy_lock:
        st = comfy_status(force=True)
        if st["ok"]:
            return st
        root = _find_comfy_root(get_setting("comfy_python"), get_setting("comfy_yaml"))
        if not root:
            st = dict(st, msg="ComfyUI 离线，且没识别到安装目录（设置里填 comfy_python）")
            _comfy_state.update(st)
            return _comfy_state
        _comfy_log("检测到 ComfyUI 离线，清理僵尸进程后重新拉起")
        _kill_comfy()
        time.sleep(3)
        py = get_setting("comfy_python")
        yaml_path = get_setting("comfy_yaml")
        args = ["-s", "ComfyUI\\main.py", "--enable-assets", "--enable-manager"]
        if yaml_path and os.path.exists(yaml_path):
            args += ["--extra-model-paths-config", yaml_path]
        args += ["--input-directory", get_setting("comfy_input_dir"),
                 "--output-directory", get_setting("comfy_output_dir")]
        try:
            subprocess.Popen([py] + args, cwd=root, creationflags=0x08000000,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            _comfy_state.update({"ok": False, "version": "unknown", "ts": time.time(),
                                 "msg": f"ComfyUI 拉起失败：{str(e)[:80]}"})
            return _comfy_state
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(5)
            st = comfy_status(force=True)
            if st["ok"]:
                _comfy_log(f"ComfyUI 已自动拉起（{int(time.time() - t0)}s）")
                return st
        _comfy_state.update({"ok": False, "version": "unknown", "ts": time.time(),
                             "msg": "ComfyUI 拉起超时（端口仍不通）"})
    return _comfy_state


def comfy_watchdog():
    """后台守护：每 45 秒确认 ComfyUI 端口活着，掉了就自动拉起来。"""
    while True:
        time.sleep(45)
        try:
            if not comfy_status(force=True)["ok"]:
                ensure_comfy()
        except Exception:
            pass


def pick_video_workflow():
    name = get_setting("video_workflow")
    if name and os.path.exists(os.path.join(WORKFLOW_DIR, name)):
        return name
    try:
        for f in sorted(os.listdir(WORKFLOW_DIR)):
            if f.endswith(".json") and ("video" in f.lower() or "h3" in f.lower()):
                return f
    except Exception:
        pass
    return ""


def comfy_submit(wf, client_id=None):
    base = get_setting("comfy_base").rstrip("/")
    client_id = client_id or str(uuid.uuid4())
    body = json.dumps({"prompt": wf, "client_id": client_id}).encode()
    req = urllib.request.Request(base + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d["prompt_id"]


def _ws_read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("websocket closed")
        buf += chunk
    return buf


def comfy_progress_listener(prompt_id, client_id, on_progress, stop, ready=None):
    """读取 ComfyUI websocket 的采样步数（value/max），失败静默，不影响主流程。
    prompt_id 传 None 时接收该 client 的所有进度（必须先连上再提交任务）。"""
    try:
        base = get_setting("comfy_base").rstrip("/")
        u = urllib.parse.urlparse(base)
        host, port = u.hostname, u.port or 80
        sock = socket.create_connection((host, port), timeout=8)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /ws?clientId={client_id} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            sock.close()
            return
        if ready:
            ready.set()
        sock.settimeout(1.5)
        while not stop.is_set():
            try:
                b1 = _ws_read_exact(sock, 1)
                b2 = _ws_read_exact(sock, 1)
                ln = b2[0] & 0x7F
                if ln == 126:
                    ln = int.from_bytes(_ws_read_exact(sock, 2), "big")
                elif ln == 127:
                    ln = int.from_bytes(_ws_read_exact(sock, 8), "big")
                payload = _ws_read_exact(sock, ln) if ln else b""
            except socket.timeout:
                continue
            op = b1[0] & 0x0F
            if op == 8:
                break
            if op != 1:
                continue
            try:
                d = json.loads(payload.decode("utf-8", "ignore"))
            except Exception:
                continue
            t = d.get("type")
            if t == "progress" and (prompt_id is None or str((d.get("data") or {}).get("prompt_id")) == str(prompt_id)):
                on_progress(int((d.get("data") or {}).get("value") or 0),
                            int((d.get("data") or {}).get("max") or 0))
        sock.close()
    except Exception:
        return


def comfy_wait(prompt_id, timeout=2400, on_tick=None, on_progress=None, client_id=None, progress_stop=None):
    base = get_setting("comfy_base").rstrip("/")
    started = time.time()
    stop = progress_stop or threading.Event()
    if on_progress and client_id and progress_stop is None:
        threading.Thread(target=comfy_progress_listener, args=(prompt_id, client_id, on_progress, stop),
                         daemon=True).start()
    try:
        while time.time() - started < timeout:
            try:
                with urllib.request.urlopen(f"{base}/history/{prompt_id}", timeout=20) as r:
                    hist = json.loads(r.read().decode("utf-8"))
                if prompt_id in hist:
                    item = hist[prompt_id]
                    outs = item.get("outputs") or {}
                    files = []
                    for node in outs.values():
                        for key in ("gifs", "videos", "images"):
                            for f in node.get(key, []) or []:
                                files.append(f)
                    if files:
                        return files[-1]
            except Exception:
                pass
            if on_tick:
                on_tick(int(time.time() - started))
            time.sleep(3)
        raise TimeoutError("ComfyUI 生成超时")
    finally:
        stop.set()


def comfy_download(fileinfo):
    base = get_setting("comfy_base").rstrip("/")
    q = urllib.parse.urlencode({"filename": fileinfo["filename"],
                                "subfolder": fileinfo.get("subfolder", ""),
                                "type": fileinfo.get("type", "output")})
    ext = os.path.splitext(fileinfo["filename"])[1] or ".mp4"
    dest = os.path.join(MEDIA_DIR, f"{uuid.uuid4().hex[:12]}{ext}")
    with urllib.request.urlopen(f"{base}/view?{q}", timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return "/media/" + os.path.basename(dest)


# ---------------------------------------------------------------- 内置 H3 视频工作流
H3_SIZES = {
    "9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024),
    "4:3": (1152, 864), "3:4": (864, 1152), "21:9": (1344, 576),
}
H3_UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
H3_CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
H3_VAE = "minimax_h3_video_vae_fp16.safetensors"
H3_AVAE = "minimax_h3_audio_vae_fp32.safetensors"
H3_LORA = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"


H3_TIER_FACTOR = {"480p": 0.625, "720p": 1.0, "1080p": 1.375}


def h3_size_for(project, shot=None):
    """分辨率档位 → 实际像素。档位来自每镜设置（480p/720p/1080p），空档沿用全局 video_scale。"""
    w, h = H3_SIZES.get(project.get("ratio") or "9:16", (768, 1344))
    tier = ((shot or {}).get("res_tier") or "").strip()
    factor = H3_TIER_FACTOR.get(tier, 0)
    if not factor:
        factor = float(get_setting("video_scale") or 1.0)
    return max(256, int(w * factor) // 16 * 16), max(256, int(h * factor) // 16 * 16)


def extract_ref_video_frame(video_url, shot_idx):
    """取本镜参考视频的首帧，作为 H3 图像参考；视频本身保留在台账里可追溯。"""
    ff = get_setting("ffmpeg_path")
    if not ff or not os.path.isfile(ff):
        raise RuntimeError("ffmpeg 不可用，无法读取参考视频")
    p = media_local_path(video_url)
    if not p or not os.path.isfile(p):
        raise RuntimeError("参考视频文件不存在")
    outdir = os.path.join(MEDIA_DIR, "refs")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "shotref_%02d.png" % int(shot_idx))
    subprocess.run([ff, "-y", "-ss", "0.1", "-i", p, "-frames:v", "1", out],
                   check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.isfile(out) or os.path.getsize(out) < 2000:
        raise RuntimeError("参考视频抽帧为空")
    return "/media/refs/" + os.path.basename(out)


def extract_last_frame(video_url, shot_idx):
    """用 ComfyUI 自带的 ffmpeg 抽上一镜视频的最后一帧，作为本镜的延续参考图。"""
    ff = get_setting("ffmpeg_path")
    if not ff or not os.path.isfile(ff):
        raise RuntimeError("ffmpeg 不可用，无法抽取上一镜末帧")
    rel = video_url.split("/media/")[-1] if "/media/" in video_url else ""
    src = os.path.join(MEDIA_DIR, rel)
    if not src or not os.path.isfile(src):
        raise RuntimeError("上一镜视频文件不存在")
    outdir = os.path.join(MEDIA_DIR, "refs")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"prev_shot{shot_idx:02d}.png")
    subprocess.run([ff, "-y", "-sseof", "-0.15", "-i", src, "-vsync", "0", "-frames:v", "1", out],
                   check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.isfile(out) or os.path.getsize(out) < 2000:
        raise RuntimeError("抽帧结果为空")
    return "/media/refs/" + os.path.basename(out)


def make_video_thumb(video_url, shot_idx, shot_id):
    """真片首帧做缩略图：卡片和合集里看到的就是成片画面。"""
    ff = get_setting("ffmpeg_path")
    if not ff or not os.path.isfile(ff):
        return ""
    rel = video_url.split("/media/")[-1] if "/media/" in video_url else ""
    src = os.path.join(MEDIA_DIR, rel)
    if not src or not os.path.isfile(src):
        return ""
    outdir = os.path.join(MEDIA_DIR, "thumbs")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"shot{shot_idx:02d}_{shot_id}.jpg")
    try:
        subprocess.run([ff, "-y", "-ss", "0.15", "-i", src, "-frames:v", "1", "-vf", "scale=512:-2", out],
                       check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    if not os.path.isfile(out) or os.path.getsize(out) < 2000:
        return ""
    return "/media/thumbs/" + os.path.basename(out)


def h3_frames(duration):
    """H3 帧数必须满足 17 步长 + 偏移 5（约 24fps）"""
    f = max(5, int(round(float(duration) * 24)))
    return f + (5 - f % 17) % 17


def stage_refs_for_comfy(shot, refs):
    """把本镜参考图复制进 ComfyUI 的 input 目录（LoadImage 直接按文件名读）"""
    indir = get_setting("comfy_input_dir")
    if not indir or not os.path.isdir(indir):
        return []
    staged = []
    for i, url in enumerate((refs or [])[:9]):
        rel = url.split("/media/")[-1] if "/media/" in url else ""
        src = os.path.join(MEDIA_DIR, rel) if rel else ""
        if not src or not os.path.isfile(src):
            continue
        ext = os.path.splitext(src)[1] or ".jpg"
        name = "studio_%s_%d%s" % (shot["id"], i, ext)
        try:
            shutil.copyfile(src, os.path.join(indir, name))
            staged.append(name)
        except Exception:
            pass
    return staged


LANGS = {
    "zh": ("普通话", "标准普通话口语，不加播音腔"),
    "yue": ("粤语（白话）", "粤语口语，句尾语气词自然，别念成书面粤语"),
    "hak": ("客家话", "客家话口语，咬字偏硬、尾音短"),
    "en": ("英语", "English, natural conversational delivery"),
    "nan": ("闽南话", "闽南话口语，声调起伏明显"),
    "sichuan": ("四川话", "四川话口语，儿化与语气词自然"),
}

PERF_DEFAULT = {
    "tone": "克制", "intensity": 3, "face": "眼神先动、嘴角后动",
    "volume": "中", "rate": "中", "gesture": "小幅、贴身动作", "eye": "看向对方眼睛",
    "lip": "开口说台词、口型与台词逐字对齐",
}


def perf_of(shot):
    p = parse_json(shot.get("perf_json"), {}) or {}
    out = dict(PERF_DEFAULT)
    out.update({k: v for k, v in p.items() if v not in (None, "")})
    return out


def perf_block(shot, project):
    """表演层：情绪 / 表情 / 音量 / 语速 / 肢体 / 眼神 / 口型，锁死后每次都写进 H3 提交原文"""
    p = perf_of(shot)
    lang = LANGS.get((project or {}).get("dialogue_lang") or "zh", LANGS["zh"])
    lines = ["【表演与台词（锁死，不得改写成旁白）】"]
    lines.append(f"台词语言：{lang[0]}（{lang[1]}）；台词用「角色：台词」格式，按该语言发音与口型")
    lines.append(f"情绪基调：{p['tone']}｜强度 {p['intensity']}/5（{'爆发' if int(p['intensity'] or 3) >= 5 else ('外放' if int(p['intensity'] or 3) == 4 else ('正常' if int(p['intensity'] or 3) == 3 else '收着')) }）")
    lines.append(f"表情细节：{p['face']}（眉眼、鼻翼、嘴角先后到位，情绪从眼底起）")
    lines.append(f"音量与语速：音量{p['volume']}｜语速{p['rate']}｜换气与停顿要真人")
    lines.append(f"眼神与肢体：{p['eye']}；{p['gesture']}")
    lines.append(f"口型：{p['lip']}；一句台词一次呼吸，不抢拍不吞字")
    dlg = parse_json(shot.get("dialogue_json"), []) if isinstance(shot.get("dialogue_json"), str) else (shot.get("dialogue") or [])
    if dlg:
        spoken = "；".join(f"{d.get('sp') or '角色'}：{d.get('line') or ''}" for d in dlg if (d.get("line") or "").strip())
        if spoken and spoken.split("：", 1)[-1] not in (shot.get("prompt_cn") or ""):
            lines.append(f"本镜台词（逐字，必须按上面语言说出口，不得改成旁白或省略）：{spoken}")
    if int(shot.get("perf_lock") or 0):
        lines.append("以上表演参数已锁定：任何重跑不得改动，镜头里必须能看出来")
    return "\n".join(lines)


PART_ORDER = [
    ("size_move", "景别/运镜"), ("video_ref", "视频参考"), ("shot_desc", "镜头描述"),
    ("chars", "角色"), ("scene", "场景"), ("blocking", "场面调度"),
    ("h3", "H3视频提示词（手动补充）"), ("seed", "随机种子"), ("dialogue", "对白"),
    ("assets", "关联资产"), ("angle", "场景视角"),
]


def parts_default(shot, project, assets=None, prev=None):
    """11 栏拆窗默认值：全部从已有字段推导，用户改哪栏就只覆盖哪栏。"""
    assets = assets or []
    chars = engine._as_list(shot.get("chars"))
    props = engine._as_list(shot.get("props"))
    blocking = parse_json(shot.get("blocking_json"), {}) or {}
    cam = blocking.get("camera") or {}
    light = blocking.get("light") or {}

    def canon(kind, name):
        for a in assets:
            if a.get("kind") == kind and (a.get("name") or "") == name:
                return a.get("canon") or a.get("desc") or name
        for a in assets:
            if a.get("kind") == kind and name and name in (a.get("name") or ""):
                return a.get("canon") or name
        return name

    dlg = engine._as_list(shot.get("dialogue")) or engine._as_list(shot.get("dialogue_json"))
    loc = (shot.get("scene_slug") or "").split("·")[1].strip() if "·" in (shot.get("scene_slug") or "") else (shot.get("scene_slug") or "")
    return {
        "size_move": "，".join(x for x in [shot.get("shot_size") or "", cam.get("height") or "",
                                          cam.get("side") or "", cam.get("move") or "",
                                          shot.get("movement") or ""] if x),
        "video_ref": ("参考上一镜末帧：同轴线、同光位、同人物站位；开场不许换机位" if int(shot.get("ref_prev") or 0)
                      else "无视频参考（只靠资产参考图锁人锁景）"),
        "shot_desc": shot.get("action") or "",
        "chars": "；".join("%s：%s" % (n, canon("char", n)[:120]) for n in chars) or "无",
        "scene": "%s：%s" % (loc, canon("scene", loc)[:140]) if loc else "无",
        "blocking": (blocking.get("text") or "") + (("；光：" + "、".join(x for x in [light.get("dir"), light.get("temp"), light.get("mood")] if x)) if light else ""),
        "h3": "",
        "seed": str(shot.get("seed") if int(shot.get("seed") if str(shot.get("seed") or "").lstrip("-").isdigit() else -1) >= 0 else "随机"),
        "dialogue": "；".join(f"{d.get('sp') or '角色'}：{d.get('line') or ''}" for d in dlg if (d.get("line") or "").strip()) or "无对白（只留环境声）",
        "assets": "、".join(chars + props) or "无",
        "angle": "、".join(x for x in [cam.get("height"), cam.get("side"), blocking.get("lens")] if x),
    }


def parts_of(shot, project, assets=None, prev=None):
    saved = parse_json(shot.get("parts_json"), {}) or {}
    base = parts_default(shot, project, assets, prev)
    out = {}
    for k, _ in PART_ORDER:
        v = saved.get(k)
        out[k] = v if isinstance(v, str) and v.strip() else base.get(k, "")
    return out


# ---------------------------------------------------------------- 单镜头结构化数据包（单镜优先）
def _find_asset(assets, kind, name):
    name = (name or "").strip()
    if not name:
        return None
    for a in assets:
        if (a.get("kind") or "") == kind and (a.get("name") or "").strip() == name:
            return a
    for a in assets:
        if (a.get("kind") or "") == kind and name in (a.get("name") or ""):
            return a
    return None


def _asset_img(a):
    if not a:
        return ""
    return (a.get("chosen_url") or a.get("image_url") or "").strip()


def packet_default(project, shot, assets):
    """单镜头数据包的默认值：从旧字段推导，资产按名字自动预绑定，之后由用户逐镜确认。"""
    assets = assets or []
    chars = engine._as_list(shot.get("chars"))
    props = engine._as_list(shot.get("props"))
    blocking = parse_json(shot.get("blocking_json"), {}) or {}
    cam = blocking.get("camera") or {}
    light = blocking.get("light") or {}
    talent = blocking.get("talent") or []
    slug = shot.get("scene_slug") or ""
    loc = slug.split("·")[1].strip() if "·" in slug else slug
    dlg = engine._as_list(shot.get("dialogue")) or engine._as_list(shot.get("dialogue_json"))
    seed = shot.get("seed")
    try:
        seed = int(seed)
    except Exception:
        seed = -1
    if seed < 0:
        seed = int(hashlib.sha1(("packet:%s:%s:%s" % (project.get("id"), shot.get("episode_id"),
                                                       shot.get("id"))).encode()).hexdigest()[:8], 16)
    bind_chars = {}
    for n in chars:
        a = _find_asset(assets, "char", n)
        if a:
            bind_chars[n] = a["id"]
    scene_asset = _find_asset(assets, "scene", loc)
    bind_props = {}
    for n in props:
        a = _find_asset(assets, "prop", n)
        if a:
            bind_props[n] = a["id"]
    char_rows = []
    for i, n in enumerate(chars):
        t = next((x for x in talent if (x.get("name") or "") == n), {})
        char_rows.append({"name": n, "role": "主角" if i == 0 else "配角",
                          "action": t.get("move") or shot.get("action") or "",
                          "position": t.get("anchor") or "", "expression": shot.get("emotion") or "",
                          "costume": ""})
    prop_rows = [{"name": n, "note": ""} for n in props]
    bind = {"chars": bind_chars, "scene": (scene_asset["id"] if scene_asset else ""),
            "props": bind_props, "first_frame": "", "last_frame": "", "ref_video": ""}
    return {
        "desc": shot.get("action") or "",
        "plan": "",
        "shot_size": shot.get("shot_size") or cam.get("distance") or "",
        "angle": "、".join(x for x in [cam.get("height"), cam.get("side")] if x) or blocking.get("axis") or "",
        "movement": shot.get("movement") or cam.get("move") or "",
        "lens": blocking.get("lens") or "",
        "light": "、".join(x for x in [light.get("dir"), light.get("temp"), light.get("mood")] if x),
        "composition": blocking.get("comp") or "",
        "tone": "",
        "texture": "",
        "style_desc": "",
        "chars": char_rows,
        "props": prop_rows,
        "scene": {"name": loc, "env": ""},
        "blocking": {"walk": (talent[0].get("move") if talent else "") or "",
                     "relation": "", "depth": "", "occlusion": "",
                     "direction": (talent[0].get("move") if talent else "") or ""},
        "bind": bind,
        "params": {"prompt_cn": shot.get("prompt_cn") or "", "prompt_neg": shot.get("prompt_neg") or "",
                   "seed": seed, "ip_weight": 0.75, "denoise": 1.0,
                   "steps": int(get_setting("video_steps") or 8), "fps": 24, "quality": "高"},
        "dialogue": {"speaker": (dlg[0].get("sp") if dlg else "") or "",
                     "line": "；".join((d.get("line") or "") for d in dlg if (d.get("line") or "").strip()),
                     "narration": "", "emotion": shot.get("emotion") or "",
                     "dialect": project.get("dialogue_lang") or "zh"},
    }


def get_packet(project, shot, assets=None):
    base = packet_default(project, shot, assets or [])
    raw = parse_json(shot.get("packet_json"), {}) or {}
    if not isinstance(raw, dict):
        return base
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        elif v not in (None, ""):
            base[k] = v
    return base


def media_local_path(url):
    url = (url or "").strip()
    if "/media/" in url:
        return os.path.join(MEDIA_DIR, url.split("/media/")[-1])
    return ""


def media_ok(url):
    p = media_local_path(url)
    return bool(p and os.path.isfile(p) and os.path.getsize(p) > 500)


def packet_ref_list(project, shot, assets, materialize=True):
    """按本镜绑定解析真实参考图：角色图→场景图→道具图→首帧→尾帧→参考视频首帧。"""
    pk = get_packet(project, shot, assets)
    by_id = {str(a["id"]): a for a in assets}
    bind = pk.get("bind") or {}
    refs, labels = [], []

    def add(url, label):
        if url and url not in refs:
            refs.append(url)
            labels.append(label)

    for c in pk.get("chars") or []:
        aid = str((bind.get("chars") or {}).get(c.get("name")) or "")
        a = by_id.get(aid)
        add(_asset_img(a), "角色参考：%s（%s）" % (c.get("name"), c.get("costume") or "原服装"))
    sid = str(bind.get("scene") or "")
    a = by_id.get(sid)
    add(_asset_img(a), "场景参考：%s" % ((pk.get("scene") or {}).get("name") or "本镜场景"))
    for p in pk.get("props") or []:
        aid = str((bind.get("props") or {}).get(p.get("name")) or "")
        a = by_id.get(aid)
        add(_asset_img(a), "道具参考：%s" % p.get("name"))
    if media_ok(bind.get("first_frame")):
        add(bind["first_frame"], "本镜首帧")
    if media_ok(bind.get("last_frame")):
        add(bind["last_frame"], "本镜尾帧")
    if media_ok(bind.get("ref_video")):
        if materialize:
            try:
                add(extract_ref_video_frame(bind["ref_video"], int(shot.get("idx") or 1)), "参考视频首帧")
            except Exception:
                pass
        else:
            add(bind["ref_video"], "参考视频首帧")
    return refs[:9], labels[:9]


def packet_errors(project, shot, assets):
    """提交前强校验：12 条缺一不可，任何一条不通过都不进 ComfyUI 队列。"""
    errs = []

    def add(field, msg):
        errs.append({"shot": int(shot.get("idx") or 0), "shot_id": shot.get("id"),
                     "field": field, "msg": msg})

    # PRD 十一：角色/场景名必须能在当前项目资产库里找到档案（改名残留旧名时立刻标红）
    by_name = {}
    for a in assets:
        for nm in ((a.get("name") or "").strip(), (a.get("canon") or "").split("｜")[0].strip()):
            if nm:
                by_name.setdefault(nm, a)
    for c in (engine._as_list(shot.get("chars")) or []):
        nm = (c.get("name") if isinstance(c, dict) else str(c)) or ""
        nm = nm.strip()
        if nm and nm not in by_name:
            add("角色档案匹配", "角色「%s」在当前项目资产库匹配不到档案（可能改名后残留旧名）" % nm)
    slug = (shot.get("scene_slug") or "").strip()
    if slug and slug not in by_name:
        add("场景档案匹配", "场景「%s」在当前项目资产库匹配不到档案" % slug)

    if not (shot.get("packet_json") or "").strip():
        add("数据包", "本镜尚未确认单镜头数据包（点「一键预绑定」或逐镜保存）")
        return errs
    pk = get_packet(project, shot, assets)

    if not (pk.get("desc") or "").strip():
        add("镜头描述", "镜头描述为空")
    if not (pk.get("movement") or "").strip():
        add("运镜方式", "运镜方式为空")
    bind = pk.get("bind") or {}
    by_id = {str(a["id"]): a for a in assets}
    for c in pk.get("chars") or []:
        a = by_id.get(str((bind.get("chars") or {}).get(c.get("name")) or ""))
        if not a:
            add("角色参考图", "出场人物「%s」未绑定角色资产" % c.get("name"))
        elif not _asset_img(a) or not media_ok(_asset_img(a)):
            add("角色参考图", "角色「%s」的参考图文件读取失败" % c.get("name"))
    scene_name = (pk.get("scene") or {}).get("name") or ""
    a = by_id.get(str(bind.get("scene") or ""))
    if not scene_name:
        add("场景", "本镜场景为空")
    elif not a:
        add("场景参考图", "场景「%s」未绑定场景资产" % scene_name)
    elif not _asset_img(a) or not media_ok(_asset_img(a)):
        add("场景参考图", "场景「%s」的参考图文件读取失败" % scene_name)
    for p in pk.get("props") or []:
        a = by_id.get(str((bind.get("props") or {}).get(p.get("name")) or ""))
        if not a:
            add("道具参考图", "道具「%s」未绑定素材" % p.get("name"))
        elif not _asset_img(a) or not media_ok(_asset_img(a)):
            add("道具参考图", "道具「%s」的参考图文件读取失败" % p.get("name"))
    for key, label in (("first_frame", "首帧"), ("last_frame", "尾帧"), ("ref_video", "参考视频")):
        url = (bind.get(key) or "").strip()
        if url and not media_ok(url):
            add(label, "%s已填写但文件读取失败" % label)
    if not (pk.get("params") or {}).get("prompt_cn") and not (shot.get("prompt_cn") or "").strip():
        add("Mini3正向提示词", "Mini3 正向提示词为空（先由字段生成或先跑③）")
    seed = (pk.get("params") or {}).get("seed")
    try:
        seed = int(seed)
    except Exception:
        seed = -1
    if seed < 0:
        add("随机种子", "随机种子缺失")
    ipw = (pk.get("params") or {}).get("ip_weight")
    if ipw in (None, ""):
        add("IP权重", "IP-Adapter 权重缺失")
    dn = (pk.get("params") or {}).get("denoise")
    if dn in (None, ""):
        add("降噪参数", "降噪参数缺失")
    dlg = engine._as_list(shot.get("dialogue")) or engine._as_list(shot.get("dialogue_json"))
    for d in dlg:
        if not (d.get("sp") or "").strip():
            add("对白", "带对白镜头缺少说话人")
        if not (d.get("line") or "").strip():
            add("对白", "带对白镜头缺少对白文本")
    if dlg:
        need = speech_seconds([d.get("line") or "" for d in dlg])
        dur = int(shot.get("duration") or 5)
        if dur < need:
            add("镜头时长", "镜头 %ds 小于配音朗读约 %.1fs" % (dur, need))
    return errs


def packet_compact_prompt(project, pk):
    """把单镜字段压成一句可读的中文正向提示词，作为 Mini3 主提示（H3 原文另由 packet_prompt_text 生成）。"""
    parts = []
    for k, label in (("shot_size", "景别"), ("angle", "角度"), ("movement", "运镜"),
                     ("lens", "焦段"), ("light", "光影"), ("composition", "构图"),
                     ("tone", "色调"), ("texture", "质感")):
        if (pk.get(k) or "").strip():
            parts.append("%s%s" % (label, pk[k]))
    if (pk.get("desc") or "").strip():
        parts.append(pk["desc"])
    cast = [c.get("name") for c in (pk.get("chars") or []) if c.get("name")]
    if cast:
        parts.append("人物：" + "、".join(cast))
        acts = ["%s%s" % (c.get("name"), c.get("action")) for c in (pk.get("chars") or [])
                if (c.get("action") or "").strip()]
        if acts:
            parts.append("；".join(acts))
    scene = pk.get("scene") or {}
    if scene.get("name"):
        parts.append("场景：" + scene["name"] + (("，" + scene["env"]) if scene.get("env") else ""))
    props = [p.get("name") for p in (pk.get("props") or []) if p.get("name")]
    if props:
        parts.append("道具：" + "、".join(props))
    style = (pk.get("style_desc") or "").strip() or (project.get("style_lock") or "").strip()
    if style:
        parts.append("风格：" + style)
    return "，".join(x.strip("，") for x in parts if x.strip())


def packet_prompt_text(project, shot, ref_labels, assets, prev):
    """把单镜数据包组装成 H3 提交原文；单镜字段优先，全局风格只做兜底。"""
    pk = get_packet(project, shot, assets)
    global_style = (project.get("style_lock") or "").strip()
    style = (pk.get("style_desc") or "").strip()
    style_line = style if style else (global_style + "（全局兜底）" if global_style else "")
    lines = ["【单镜头数据包（单镜优先，禁止用全局参数改写）】"]
    spec = [("景别", pk.get("shot_size")), ("角度", pk.get("angle")), ("运镜", pk.get("movement")),
            ("焦段", pk.get("lens")), ("光影", pk.get("light")), ("构图", pk.get("composition")),
            ("色调", pk.get("tone")), ("质感", pk.get("texture"))]
    spec_txt = "｜".join("%s：%s" % (k, v) for k, v in spec if (v or "").strip())
    if spec_txt:
        lines.append("【镜头规格】" + spec_txt + "｜时长：%ds" % int(shot.get("duration") or 5))
    desc = (pk.get("desc") or "").strip()
    if desc:
        lines.append("【镜头内容】" + desc)
    if (pk.get("plan") or "").strip():
        lines.append("【拍摄方案】" + pk["plan"])
    if style_line:
        lines.append("【画面风格】" + style_line)
    cast = pk.get("chars") or []
    if cast:
        rows = []
        for c in cast:
            rows.append("%s（%s）服装：%s｜站位：%s｜表情：%s｜动作：%s" % (
                c.get("name"), c.get("role") or "角色", c.get("costume") or "沿用角色设定",
                c.get("position") or "按调度", c.get("expression") or "随台词情绪",
                c.get("action") or "按镜头描述"))
        lines.append("【人物】" + "；".join(rows))
    scene = pk.get("scene") or {}
    scene_txt = "场景：" + (scene.get("name") or "未填")
    if (scene.get("env") or "").strip():
        scene_txt += "｜环境：" + scene["env"]
    props = [p.get("name") for p in (pk.get("props") or []) if p.get("name")]
    if props:
        scene_txt += "｜道具：" + "、".join(props)
    lines.append("【场景与道具】" + scene_txt)
    bl = pk.get("blocking") or {}
    bl_labels = (("walk", "人物走位"), ("relation", "镜头与人物关系"), ("depth", "前后景关系"),
                 ("occlusion", "遮挡关系"), ("direction", "运动方向"))
    bl_txt = "｜".join("%s：%s" % (label, bl.get(k)) for k, label in bl_labels if (bl.get(k) or "").strip())
    if bl_txt:
        lines.append("【场面调度】" + bl_txt)
    if ref_labels:
        lines.append("【参考图绑定（按顺序）】" + "；".join(
            "图%d = %s" % (i + 1, n) for i, n in enumerate(ref_labels)))
    dlg = engine._as_list(shot.get("dialogue")) or engine._as_list(shot.get("dialogue_json"))
    if dlg:
        lang = LANGS.get((project.get("dialogue_lang") or "zh"), LANGS["zh"])
        spoken = "；".join("%s：%s" % (d.get("sp") or "角色", d.get("line") or "") for d in dlg)
        lines.append("【对白｜%s】%s" % (lang[0], spoken))
        lines.append("【口型与情绪】说话人：%s｜情绪：%s｜音量语速按表演层，逐字对口型，台词没说完不许切" % (
            dlg[0].get("sp") or "角色", (pk.get("dialogue") or {}).get("emotion") or shot.get("emotion") or "随剧情"))
    if (pk.get("dialogue") or {}).get("narration"):
        lines.append("【旁白】" + pk["dialogue"]["narration"])
    if int(shot.get("ref_prev") or 0) and prev:
        lines.append("【衔接上一镜】本镜第一帧必须与上一镜末帧同轴线、同光位、同人物站位；"
                     "上一镜结尾动作：%s" % (prev.get("action") or "")[:80])
    if int(shot.get("perf_lock") or 0):
        lines.append(perf_block(shot, project))
    lines.append("【渲染参数】seed=%s｜IP权重=%s｜降噪=%s｜档位=%s步｜%sfps｜画质=%s" % (
        (pk.get("params") or {}).get("seed"), (pk.get("params") or {}).get("ip_weight"),
        (pk.get("params") or {}).get("denoise"), (pk.get("params") or {}).get("steps"),
        (pk.get("params") or {}).get("fps"), (pk.get("params") or {}).get("quality")))
    extra = ((parse_json(shot.get("parts_json"), {}) or {}).get("h3") or "").strip()
    if extra:
        lines.append("【H3 手动补充】" + extra)
    lines.append("【硬约束】%ds｜%s｜%sfps｜无字幕｜无水印｜不换脸不换装｜台词说完才切" % (
        int(shot.get("duration") or 5), project.get("ratio") or "9:16",
        (pk.get("params") or {}).get("fps") or 24))
    neg = (pk.get("params") or {}).get("prompt_neg") or (project.get("forbidden") or "")
    if neg:
        lines.append("画面里不要出现：" + neg)
    return "\n".join(lines)


def h3_prompt_text(shot, project, ref_count=0, ref_labels=None, assets=None, prev=None):
    """③ 里展示给用户的「H3 提交原文」：与真正送进 ComfyUI 的字符串逐字一致"""
    if (shot.get("packet_json") or "").strip():
        return packet_prompt_text(project, shot, ref_labels or [], assets or [], prev)
    prompt = (shot.get("prompt_cn") or "").strip()
    neg = (shot.get("prompt_neg") or "").strip()
    mode = (project or {}).get("prompt_mode") or "cn"
    saved_parts = parse_json(shot.get("parts_json"), {}) or {}
    parts = parts_of(shot, project, assets, prev) if saved_parts else None
    cont = ""
    if int(shot.get("ref_prev") or 0) and prev:
        pd = engine._as_list(prev.get("dialogue")) or engine._as_list(prev.get("dialogue_json"))
        tail = ""
        if pd:
            tail = f"上一镜最后一句台词：{pd[-1].get('sp') or '角色'}：{pd[-1].get('line') or ''}；"
        cont = ("【衔接上一镜】" + tail +
                f"上一镜结尾动作：{(prev.get('action') or '')[:80]}；本镜开场第一帧必须与上一镜末帧同轴线、同光位、同人物站位，"
                "情绪从上镜结尾继续，不许重新起范，不许换景。")
    if saved_parts and parts:
        # 拆窗模式：11 栏逐栏落成 H3 原文，手动改过的值原样进提示词
        blocks = []
        for key, label in PART_ORDER:
            v = (parts.get(key) or "").strip()
            if not v:
                continue
            if key == "seed" and not v.isdigit():
                continue
            blocks.append(f"【{label}】{v}")
        if cont:
            blocks.insert(0, cont)
        prompt = "\n".join(blocks) if blocks else prompt
        prompt = perf_block(shot, project) + "\n\n" + prompt
    else:
        # 旧路径：中文自由式 / 官方六段式
        if ref_count:
            lines = ["【参考图锁定（按顺序）】"]
            for i, name in enumerate((ref_labels or [])[:ref_count]):
                lines.append(f"图{i + 1} = <Subject {i + 1}> {name}" if mode == "h3ref" else f"图{i + 1} = {name}")
            prompt = "\n".join(lines) + "\n\n" + prompt
        if cont:
            prompt = cont + "\n\n" + prompt
        if mode != "h3ref":
            prompt = perf_block(shot, project) + "\n\n" + prompt
    if neg:
        prompt += "\n\n画面里不要出现：" + neg
    prompt += f"\n\n【硬约束】时长 {int(shot.get('duration') or 5)} 秒｜画幅 {project.get('ratio') or '9:16'}｜24fps｜无字幕｜无水印｜不换脸不换装"
    return prompt


def build_h3_workflow(shot, project, ref_files, ref_labels=None, assets=None, prev=None):
    """按资产参考图 + 提示词 + 时长，现拼一份 API 格式的 H3 参考生视频工作流"""
    w, h = h3_size_for(project, shot)
    # H3 靠参考图锁人锁物：先把「第几张图是谁」写进提示词，出片才不串脸
    prompt = h3_prompt_text(shot, project, len(ref_files), ref_labels, assets, prev)
    frames = h3_frames(shot.get("duration") or 5)
    steps = int(get_setting("video_steps") or 8)
    pk = get_packet(project, shot, assets or []) if (shot.get("packet_json") or "").strip() else {}
    try:
        denoise = float((pk.get("params") or {}).get("denoise") if pk else 1.0)
    except Exception:
        denoise = 1.0
    denoise = min(1.0, max(0.0, denoise))
    try:
        fixed = int(shot.get("seed") if shot.get("seed") is not None else -1)
    except Exception:
        fixed = -1
    seed = fixed if fixed >= 0 else int(hashlib.sha1((shot.get("prompt_cn") or "").encode("utf-8")).hexdigest()[:8], 16)

    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": H3_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_CLIP, "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": H3_AVAE}},
        "5": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": H3_LORA,
                                                             "strength_model": 0.75}},
        "20": {"class_type": "MiniMaxH3ReferenceToVideo",
               "inputs": {"clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
                          "prompt": prompt, "width": w, "height": h,
                          "length": frames, "ref_image_size": "match"}},
        "21": {"class_type": "BasicGuider", "inputs": {"model": ["5", 0], "conditioning": ["20", 0]}},
        "22": {"class_type": "BasicScheduler", "inputs": {"model": ["5", 0], "scheduler": "simple",
                                                         "steps": steps, "denoise": denoise}},
        "23": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "24": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "25": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["24", 0], "guider": ["21", 0], "sampler": ["23", 0],
                          "sigmas": ["22", 0], "latent_image": ["20", 1]}},
        "26": {"class_type": "VAEDecode", "inputs": {"samples": ["25", 0], "vae": ["3", 0]}},
        "27": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["25", 0], "vae": ["4", 0]}},
        "28": {"class_type": "CreateVideo", "inputs": {"images": ["26", 0], "audio": ["27", 0],
                                                       "fps": 24, "bit_depth": "auto",
                                                       "color_space": "sRGB", "codec": "auto"}},
        "29": {"class_type": "SaveVideo", "inputs": {"video": ["28", 0],
                                                     "filename_prefix": "studio/shot%03d" % int(shot.get("idx") or 1),
                                                     "format": "auto", "codec": "auto"}},
    }
    refmap = {}
    for i, name in enumerate(ref_files):
        nid = str(30 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name, "upload": "image"}}
        refmap["ref_image_%d" % i] = [nid, 0]
    if refmap:
        g["20"]["inputs"]["ref_images"] = refmap
    return g


# ---------------------------------------------------------------- 内置 qwen_image 2512 出图
IMG_UNET = "qwen_image_2512_fp8_e4m3fn.safetensors"
IMG_CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
IMG_VAE = "qwen_image_vae.safetensors"
IMG_LORA = "Wuli-Qwen-Image-2512-Turbo-LoRA-2steps-V1.0-bf16.safetensors"
IMG_NEG = ("低清，模糊，糊脸，畸形手指，多头多手，多余肢体，文字，字幕，标注，编号，水印，logo，"
           "分格线，边框，排版留白，样张文字，画面脏乱，过曝，欠曝，噪点，压缩痕迹，拼接感")


def image_size(asset, mode):
    kind = (asset.get("kind") or "char").lower()
    if kind == "char":
        return (1664, 928) if mode == "four" else (1104, 1472)
    if kind == "scene":
        return (928, 1664)
    return (1328, 1328)


def image_prompt(asset, mode, project):
    kind = (asset.get("kind") or "char").lower()
    name = asset.get("name") or ""
    canon = (asset.get("canon") or "").strip()
    style = (project.get("style_lock") or "").strip()
    base = f"好莱坞电影级质感，超精细，真实摄影级细节。{style}" if style else "好莱坞电影级质感，超精细，真实摄影级细节"
    if kind == "char":
        who = f"{name}。人物设定：{canon}。"
        if mode == "four":
            return (f"角色四视图设定图（Character Turnaround Sheet），同一角色四个视角横向并排："
                    f"左1 正面全身、左2 侧面全身、左3 背面全身、右1 面部特写。"
                    f"{who}四个视角必须是同一个人、同一套服装、同一发型与同一配饰，比例一致，身高一致，"
                    f"浅灰纯色背景，均匀柔光，全身完整入画不裁切，无文字无标注无分格线。{base}", IMG_NEG)
        return (f"角色定妆照，正面半身，目光看向镜头，浅灰背景，柔光，服装与配饰细节清晰。"
                f"{who}{base}", IMG_NEG)
    if kind == "scene":
        # 场景 canon 里往往写着人物动作（"阿棠被拦在门外"），出环境图时必须剥掉，否则模型一定画人
        head = canon.split("环境细节")[0].strip(" ｜")
        mood = "｜".join([seg for seg in head.split("｜")[1:] if seg.strip()])[:120] or head[:120]
        return (f"空镜头环境概念图，竖构图。画面里绝对没有任何人、没有人影、没有背影、没有剪影，是纯环境空景。"
                f"只拍建筑结构、墙面与地面材质、门窗、陈设道具、光源位置与色温。所有招牌和墙面不要出现文字。"
                f"地点：{name}。氛围与时间：{mood}。{base}", IMG_NEG)
    return (f"道具设定图，白色背景，主体居中占画 70%，材质与磨损细节清晰，多角度暗示。"
            f"道具：{name}。设定：{canon}。{base}", IMG_NEG)


def build_image_workflow(pos, neg, width, height, seed):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": IMG_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": IMG_CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": IMG_VAE}},
        "4": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": IMG_LORA,
                                                             "strength_model": 1.0}},
        "5": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["4", 0], "shift": 3.0}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": pos}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": neg}},
        "8": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}},
        "9": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "10": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "positive": ["6", 0], "negative": ["8", 0],
                                                   "latent_image": ["9", 0], "seed": seed, "steps": 2, "cfg": 1.0,
                                                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": "studio/asset"}},
    }


def do_images_batch(jid, pid, kind=""):
    """批量出图：没图的资产先补，角色默认四视图"""
    assets = [a for a in list_assets(pid) if (not kind or a["kind"] == kind)]
    total = max(1, len(assets))
    job_log(jid, f"批量出图：{len(assets)} 项资产（角色四视图 / 场景竖图 / 道具方图）", 4)
    for i, a in enumerate(assets):
        job_log(jid, f"[{i + 1}/{total}] 开始：{a['name']}", int(i / total * 100))
        try:
            do_image(jid, pid, a["id"], "four" if a["kind"] == "char" else "single")
        except Exception as e:
            job_log(jid, f"[{i + 1}/{total}] {a['name']} 失败：{str(e)[:90]}")
    job_log(jid, "批量出图结束", 100)


def list_asset_images(aid):
    return [dict(r) for r in db().execute(
        "SELECT * FROM asset_images WHERE asset_id=? ORDER BY id DESC", (aid,))]


def do_image(jid, pid, aid, mode="single", prompt_override="", seed=0):
    project = get_project(pid)
    asset = rowdict(db().execute("SELECT * FROM assets WHERE id=? AND project_id=?", (aid, pid)).fetchone())
    if not asset:
        raise RuntimeError("资产不存在")
    cs = comfy_status(force=True)
    pos, neg = image_prompt(asset, mode, project)
    if (asset.get("prompt_maker") or "").strip():
        pos = asset["prompt_maker"].strip()
    if prompt_override:
        pos = prompt_override
    w, h = image_size(asset, mode)
    seed = int(seed or int(hashlib.sha1((pos + str(time.time())).encode()).hexdigest()[:8], 16))
    job_log(jid, f"出图目标：{asset['name']}｜{'四视图' if mode == 'four' else '单视图'}｜{w}x{h}", 10)
    job_log(jid, f"提示词：{pos[:160]}…", 18)
    if not cs["ok"]:
        job_log(jid, "ComfyUI 离线，无法出图（先双击 一键启动.bat 拉起 8188）", 100)
        job_update(jid, status="failed", message="ComfyUI 离线")
        return
    # PRD 四：图像前级工作流下拉。内置 qwen_image2512 之外，只认 workflows/ 下真实存在的同名工作流文件
    gen_wf = (asset.get("gen_workflow") or "qwen_image2512").strip()
    if gen_wf and gen_wf not in ("qwen_image2512", "qwen_image_2512"):
        wf_path = os.path.join(WORKFLOW_DIR, gen_wf + ".json")
        if not os.path.exists(wf_path):
            job_update(jid, status="failed", message="前级工作流未安装")
            job_log(jid, "前级工作流「%s」未安装：workflows/%s.json 不存在，已阻断出图（不拿内置模型冒充）"
                    % (gen_wf, gen_wf), 100)
            return
        wf = load_workflow(gen_wf + ".json")
        for nid, node in (wf or {}).items():
            if isinstance(node, dict) and isinstance((node.get("inputs") or {}).get("text"), str):
                node["inputs"]["text"] = pos
        job_log(jid, "用前级工作流 %s 出图" % gen_wf, 28)
    else:
        wf = build_image_workflow(pos, neg, w, h, seed)
        job_log(jid, f"提交 ComfyUI（qwen_image_2512 · 2step LoRA · seed {seed}）", 28)
    started = time.time()
    stepped = [False]

    def tick(elapsed):
        pct = min(88, 28 + int(elapsed / 3.2))
        if stepped[0]:
            job_log(jid, None, pct)
        else:
            job_log(jid, f"生成中 {elapsed}s", pct)

    def steps(v, m):
        if m:
            stepped[0] = True
            job_log(jid, f"采样生成中 {v}/{m} 步", min(88, 30 + int(56 * v / m)))

    cid = str(uuid.uuid4())
    stop = threading.Event()
    ready = threading.Event()
    threading.Thread(target=comfy_progress_listener, args=(None, cid, steps, stop, ready), daemon=True).start()
    ready.wait(1.0)
    pidc = comfy_submit(wf, cid)

    fileinfo = comfy_wait(pidc, timeout=900, on_tick=tick, client_id=cid, progress_stop=stop)
    url = comfy_download(fileinfo)
    with _db_lock:
        cur = db().execute("INSERT INTO asset_images(asset_id,project_id,url,mode,prompt,seed,chosen,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (aid, pid, url, mode, pos, seed, 1 if not list_asset_images(aid) else 0, now()))
        db().commit()
    if not list_asset_images(aid)[1:]:
        with _db_lock:
            db().execute("UPDATE assets SET image_url=?, locked=1, updated_at=? WHERE id=?", (url, now(), aid))
            db().commit()
    job_log(jid, f"出图完成 → {url}（已进资产库：{asset['name']}）", 100)
    log_activity(pid, "image", f"出图 {asset['name']}（{mode}）")


# ---------------------------------------------------------------- 数据读取

def rowdict(r):
    return dict(r) if r else None


def get_project(pid):
    return rowdict(db().execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())


def get_episode(eid):
    return rowdict(db().execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone())


def list_assets(pid):
    return [dict(r) for r in db().execute(
        "SELECT * FROM assets WHERE project_id=? ORDER BY kind, sort, id", (pid,))]


def list_shots(eid):
    return [dict(r) for r in db().execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY idx", (eid,))]


def parse_json(s, default):
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except Exception:
        return default


# ---------------------------------------------------------------- 签名 / 失效

def sha(*parts) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def shot_sig1(project, shot, assets):
    """① 层的输入签名：分镜描述 / 对白 / 角色·场景·道具锁定特征 / 风格锁。"""
    def canon(kind, name):
        for a in assets:
            if a["kind"] == kind and name and (a["name"] == name or name in (a["name"] or "")):
                return (a.get("canon") or "") + ("|L" if a.get("locked") else "|U")
        return "∅"

    chars = parse_json(shot.get("chars"), [])
    props = parse_json(shot.get("props"), [])
    return sha(
        shot.get("action"), shot.get("dialogue_json"), shot.get("duration"),
        shot.get("shot_size"), shot.get("camera"), shot.get("movement"),
        shot.get("scene_slug"), shot.get("emotion"), shot.get("transition"),
        [canon("char", c) for c in chars], [canon("prop", p) for p in props],
        canon("scene", (shot.get("scene_slug") or "").split("·")[1].strip() if "·" in (shot.get("scene_slug") or "") else shot.get("scene_slug")),
        project.get("style_lock"), project.get("ratio"), project.get("forbidden"),
    )


def shot_sig2(shot):
    return sha(shot.get("blocking_json"), shot.get("duration"))


def shot_sig3(shot, project=None):
    vals = [shot.get("prompt_cn"), shot.get("prompt_en"), shot.get("prompt_neg"),
            shot.get("perf_json"), shot.get("perf_lock"),
            shot.get("parts_json") or "", shot.get("seed") if shot.get("seed") is not None else -1,
            shot.get("ref_prev") or 0,
            (project or {}).get("dialogue_lang") or "zh"]
    if (shot.get("packet_json") or "").strip():
        vals.append(shot["packet_json"])
    return sha(*vals)


def shot_flags(project, shot, assets):
    s1 = shot_sig1(project, shot, assets)
    block_stale = (not shot.get("blocking_json")) or s1 != (shot.get("sig1_at_block") or "")
    prompt_stale = block_stale or (not shot.get("prompt_cn")) or shot_sig2(shot) != (shot.get("sig2_at_prompt") or "")
    gen_stale = prompt_stale or (not shot.get("sig3_at_gen")) or shot_sig3(shot, project) != (shot.get("sig3_at_gen") or "")
    return {"sig1": s1, "block_stale": block_stale, "prompt_stale": prompt_stale, "gen_stale": gen_stale}


def proj_sig(project):
    return sha(project.get("title"), project.get("premise"), project.get("genre"),
               project.get("episode_count"), project.get("episode_length"),
               project.get("style_lock"), project.get("forbidden"), project.get("ratio"))


# ---------------------------------------------------------------- 状态聚合

STEP_NAMES = ["目标锁定", "小说大纲", "小说正文", "剧本生成", "分镜设计",
              "场面调度", "视频提示词", "生成视频", "成片合集"]


# ---------------------------------------------------------------- 视觉风格库
# 风格锁不是一句随便的话：每条都是「画风 + 材质 + 光比 + 镜头语言」四件套，选完直接写进 style_lock
STYLE_CATALOG = [
    {"g": "写实", "id": "live_action", "n": "真人写实", "t": "实拍电影质感，真实皮肤纹理与毛孔，35mm 胶片色彩，自然光比"},
    {"g": "写实", "id": "cinematic", "n": "电影感", "t": "电影级宽银幕质感，高动态范围，浅景深，冷暖对撞的布光，轻微颗粒"},
    {"g": "写实", "id": "doc", "n": "纪实摄影", "t": "手持纪实感，现场光，低饱和，轻微晃动，真实环境声"},
    {"g": "2D 动画", "id": "anime_jp", "n": "日系动漫", "t": "日式赛璐璐上色，清晰线稿，柔和渐变天空，大眼睛高光，2D 平涂"},
    {"g": "2D 动画", "id": "cartoon", "n": "卡通", "t": "粗描边卡通，明快纯色，夸张表情与弹性动作，简笔背景"},
    {"g": "2D 动画", "id": "dark_comic", "n": "暗黑漫画风格", "t": "高反差黑白漫画，粗墨线，网点阴影，压迫构图，克制的红点缀"},
    {"g": "2D 动画", "id": "ink_comic", "n": "国漫条漫", "t": "现代国漫条漫风，细线稿 + 赛璐璐，暖色滤镜，都市感"},
    {"g": "2D 动画", "id": "shadow_puppet", "n": "皮影戏插画风格", "t": "皮影剪影质感，镂空纹理，暖黄幕布光，横向叙事构图"},
    {"g": "3D 动画", "id": "c3d_guofeng", "n": "3D 国风动漫", "t": "三维国风角色，布料与发丝物理质感，柔光棚拍，浅景深，青绿山水背景"},
    {"g": "3D 动画", "id": "c3d_pixar", "n": "3D 卡通渲染", "t": "皮克斯式三维卡通，圆润造型，大面积柔光，饱和但不刺眼"},
    {"g": "3D 动画", "id": "c3d_real", "n": "3D 写实渲染", "t": "影视级三维写实，PBR 材质，全局光照，次表面散射皮肤"},
    {"g": "3D 动画", "id": "c3d_clay", "n": "黏土定格风", "t": "黏土定格质感，手捏肌理，微缩布景，暖棚灯"},
    {"g": "东方美学", "id": "guofeng", "n": "古风", "t": "东方古典美学，绢本设色，留白构图，烛光与窗棂投影，衣袂飘逸"},
    {"g": "东方美学", "id": "xianxia", "n": "神话仙侠", "t": "仙侠玄幻，灵气粒子，飘带与云海，冷青 + 金高光，体积光穿透"},
    {"g": "东方美学", "id": "cn_myth", "n": "中国神话风格", "t": "上古神话气质，青铜与朱砂配色，夸张神性造型，烟雾缭绕，仰视威压构图"},
    {"g": "东方美学", "id": "wuxia", "n": "武侠水墨", "t": "水墨武侠，墨色晕染，飞白笔触，雨雾竹林，动势留痕"},
    {"g": "幻想", "id": "fantasy", "n": "奇幻", "t": "西式奇幻，魔法辉光，繁复纹理服饰，丁达尔光束，史诗构图"},
    {"g": "幻想", "id": "cyberpunk", "n": "赛博朋克", "t": "霓虹雨夜，青紫与洋红对撞，体积雾，反射湿地，机械义体细节"},
    {"g": "幻想", "id": "scifi", "n": "硬科幻", "t": "冷白工业光，金属与玻璃材质，全息界面，精确几何构图"},
    {"g": "幻想", "id": "steampunk", "n": "蒸汽朋克", "t": "黄铜齿轮，蒸汽与煤烟，暖橙光源，繁复机械结构"},
    {"g": "绘画", "id": "oil", "n": "油画", "t": "古典油画笔触，厚涂肌理，伦勃朗式明暗，暖褐调"},
    {"g": "绘画", "id": "watercolor", "n": "水彩", "t": "透明水彩，湿边晕染，纸纹颗粒，清淡高调"},
    {"g": "绘画", "id": "sketch", "n": "素描", "t": "铅笔素描，排线结构，灰阶层次，纸面质感"},
    {"g": "绘画", "id": "ukiyoe", "n": "浮世绘", "t": "浮世绘版画，平涂色块，粗轮廓线，波纹与云纹装饰"},
    {"g": "风格化", "id": "pixel", "n": "像素风", "t": "16-bit 像素美术，有限色板，硬边阴影，网格对齐"},
    {"g": "风格化", "id": "lowpoly", "n": "低多边形", "t": "低多边形三维，块面切分，单色渐变，极简光影"},
    {"g": "风格化", "id": "vaporwave", "n": "蒸汽波", "t": "粉紫渐变，网格地平线，复古扫描线，故障色散"},
    {"g": "风格化", "id": "flat", "n": "扁平插画", "t": "扁平矢量插画，无描边，几何概括，柔和阴影"},
    {"g": "风格化", "id": "grain", "n": "胶片颗粒", "t": "高感光胶片，明显颗粒，色彩偏移，漏光边缘"},
    {"g": "风格化", "id": "noir", "n": "黑白黑色电影", "t": "黑白高反差，硬光百叶窗阴影，低机位，雾气质感"},
]


def revise_shot(shot, project, text):
    """镜头 AI 对话：把一句自然语言意见落成可执行改动，返回（回复, 待更新字段）。"""
    t = (text or "").strip()
    updates = {}
    notes = []
    dialogue = parse_json(shot.get("dialogue_json"), [])
    blocking = parse_json(shot.get("blocking_json"), {})
    cam = dict(blocking.get("camera") or {})
    light = dict(blocking.get("light") or {})

    if any(k in t for k in ("台词", "对白", "这句话", "这句", "说得", "语气", "生硬")) and dialogue:
        strong = any(k in t for k in ("强烈", "更狠", "更重", "爆发", "生气", "怒", "哭", "哽咽", "颤", "压迫"))
        softer = any(k in t for k in ("温柔", "轻", "克制", "平静", "小声"))
        for d in dialogue:
            line = d.get("line") or ""
            if strong:
                d["emotion"] = "压抑后的爆发"
                if line.endswith("。"):
                    d["line"] = line[:-1] + "！"
                elif not line.endswith(("！", "？")):
                    d["line"] = line.rstrip("，,") + "！"
                d["line"] = d["line"].replace("吗", "").replace("吧", "")
            elif softer:
                d["emotion"] = "克制、低音量"
                d["line"] = line.rstrip("！") + "。"
        updates["dialogue_json"] = json.dumps(dialogue, ensure_ascii=False)
        notes.append("台词情绪已重写：%s" % ("拉满（去软化词、收尾改感叹）" if strong else
                                             ("降为克制低音量" if softer else "重排语气")))
    if any(k in t for k in ("台词", "对白", "这句话", "这句", "说得", "语气", "生硬")) and not dialogue:
        strong = any(k in t for k in ("强烈", "更狠", "更重", "爆发", "生气", "怒", "哭", "哽咽", "颤", "压迫"))
        if strong:
            updates["action"] = ((shot.get("action") or "").rstrip("。；") +
                                 "；表演加码：咬字更硬、呼吸更重、眼神更狠，情绪落点前留半拍停顿")[:400]
            updates["emotion"] = "压抑后的爆发（不许软下来）"
            notes.append("这一镜原本没有写对白，我把表演与情绪强度顶上去了（要真加一句台词就说「给阿棠加一句…」）")
        else:
            updates["action"] = ((shot.get("action") or "").rstrip("。；") + "；表演收着来，情绪藏在眼神和小动作里")[:400]
            updates["emotion"] = "克制、低音量"
            notes.append("这一镜没有对白，改成收着演的表演方向")
    if any(k in t for k in ("加一句", "加台词", "补一句")):
        who = next((d.get("sp") for d in dialogue), None) or (((parse_json(shot.get("chars"), [])) or ["角色"])[0])
        dialogue.append({"sp": who, "line": "（这里补一句更短的钩子台词）", "emotion": "压低声音"})
        updates["dialogue_json"] = json.dumps(dialogue, ensure_ascii=False)
        notes.append("补了一条待填台词，角色=%s" % who)
    if any(k in t for k in ("删台词", "去掉台词", "不要说话", "无对白")):
        updates["dialogue_json"] = "[]"
        notes.append("已清空本镜对白（改成纯画面表演）")

    if any(k in t for k in ("推近", "靠近", "特写", "怼脸", "放大")):
        updates["shot_size"] = "大特写" if "大特写" in t else "特写"
        updates["movement"] = "缓慢推近"
        notes.append("景别→%s + 缓慢推近" % updates["shot_size"])
    if any(k in t for k in ("拉远", "退后", "全景", "拉开")):
        updates["shot_size"] = "全景"
        updates["movement"] = "缓慢拉远"
        notes.append("景别→全景 + 缓慢拉远")
    if any(k in t for k in ("仰拍", "低机位", "仰视")):
        cam["height"] = "低机位仰拍"
        notes.append("机位→低机位仰拍（压迫感）")
    if any(k in t for k in ("俯拍", "高机位", "俯视")):
        cam["height"] = "高机位俯拍"
        notes.append("机位→高机位俯拍")
    if any(k in t for k in ("侧面", "侧脸", "侧前方")):
        cam["side"] = "侧前方 45°"
        notes.append("机位→侧前方 45°")
    if any(k in t for k in ("背后", "背面", "背影")):
        cam["side"] = "正后方"
        notes.append("机位→正后方跟背")

    if any(k in t for k in ("暗", "压暗", "低调", "阴")):
        light["temp"] = "冷调 4300K"
        light["dir"] = light.get("dir") or "侧逆光"
        notes.append("光比压暗、色温转冷")
    if any(k in t for k in ("亮", "提亮", "阳光", "暖")):
        light["temp"] = "暖调 5200K"
        notes.append("整体提亮、色温转暖")
    if "逆光" in t:
        light["dir"] = "逆光轮廓"
        notes.append("主光→逆光轮廓")

    if any(k in t for k in ("快", "紧张", "急", "节奏")):
        updates["duration"] = max(3, int(shot.get("duration") or 5) - 1)
        notes.append("时长压到 %ss" % updates["duration"])
    elif any(k in t for k in ("慢", "拖", "缓")):
        updates["duration"] = min(10, int(shot.get("duration") or 5) + 1)
        notes.append("时长放到 %ss" % updates["duration"])

    if any(k in t for k in ("加", "换成", "改成", "加入")) and not notes:
        add = re.sub(r"^(把|让|请|帮我)?(加|加入|换成|改成)", "", t).strip("。！ ")
        if add:
            updates["action"] = ((shot.get("action") or "").rstrip("。") + "；" + add)[:400]
            notes.append("画面动作追加：%s" % add[:40])

    if cam:
        blocking["camera"] = cam
    if light:
        blocking["light"] = light
    if cam or light:
        updates["blocking_json"] = json.dumps(blocking, ensure_ascii=False)

    if not notes:
        return ("没识别出可执行的改动。可以直接这样说：这句台词太生硬、情绪更强烈点 / 镜头推近拍表情 / "
                "光线压暗、转冷 / 节奏再快一点 / 机位改侧后方。", {})
    return ("已改 %d 处：%s。① 已变动 → ②③④ 标记需重跑；想让片子跟着变，去②重跑调度再出片。"
            % (len(notes), "；".join(notes))), updates


def apply_shot_updates(shot, updates):
    if not updates:
        return
    cols = [c for c in ("shot_size", "movement", "duration", "action", "emotion", "dialogue_json", "blocking_json")
            if c in updates]
    sets = ", ".join("%s=?" % c for c in cols)
    vals = [updates[c] for c in cols]
    with _db_lock:
        db().execute("UPDATE shots SET %s, updated_at=? WHERE id=?" % sets, vals + [now(), shot["id"]])
        db().commit()


def sync_episodes(pid):
    """按大纲把缺失的集补齐（12 集就真的存在 12 个分组，而不是只写在大纲里）"""
    project = get_project(pid)
    have = {int(e["idx"]) for e in db().execute("SELECT idx FROM episodes WHERE project_id=?", (pid,))}
    titles = {}
    row = db().execute("SELECT outline_json FROM episodes WHERE project_id=? ORDER BY idx LIMIT 1", (pid,)).fetchone()
    if row and row["outline_json"]:
        outline = parse_json(row["outline_json"], {})
        for act in outline.get("acts") or []:
            for e in act.get("episodes") or []:
                titles[int(e.get("idx") or 0)] = e.get("title") or ""
    created = 0
    for idx in range(1, int(project.get("episode_count") or 12) + 1):
        if idx in have:
            continue
        with _db_lock:
            db().execute("INSERT INTO episodes(project_id,idx,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (pid, idx, titles.get(idx) or ("第%d集" % idx), now(), now()))
            db().commit()
        created += 1
    return created


def speech_seconds(lines, lang="zh"):
    """按字数估朗读时长：中文 4.2 字/秒，英文按 2.6 词/秒；含标点停顿。"""
    text = "".join(lines or [])
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z']+", text))
    return round(cjk / 4.2 + words / 2.6 + (0.8 if (cjk or words) else 0.0), 1)


def _split_long_line(line, limit_sec):
    """单句台词超过上限：按标点 + 字数硬切，保证每段朗读 ≤ limit_sec。"""
    max_chars = max(8, int(limit_sec * 4.2))
    parts = [p for p in re.split(r"(?<=[，。！？；…])", line) if p.strip()] or [line]
    out = []
    for p in parts:
        while len(p) > max_chars:
            cut = max(p.rfind("，", 0, max_chars), p.rfind("。", 0, max_chars))
            if cut < 4:
                cut = max_chars
            out.append(p[:cut + 1])
            p = p[cut + 1:]
        if p:
            out.append(p)
    return out


def normalize_shots(shots):
    """分镜时长硬规则：≥ 台词朗读时长；单镜 ≤15s；超长台词自动拆镜。"""
    out = []
    for s in shots:
        dlg = list(s.get("dialogue") or [])
        lines = [d.get("line") or "" for d in dlg]
        need = speech_seconds(lines)
        if need <= 15:
            dur = max(1, min(15, int(max(need + 0.4, float(s.get("duration") or 2)))))
            s["duration"] = dur
            out.append(s)
            continue
        # 超过 15 秒：按语义断点拆成多镜，每镜台词朗读 ≤13.5 秒
        groups, cur, cur_sec = [], [], 0.0
        for d in dlg:
            ln = d.get("line") or ""
            sec = speech_seconds([ln])
            if sec > 15 and len(cur) == 0:
                for piece in _split_long_line(ln, 13.5):
                    groups.append([{"sp": d.get("sp"), "line": piece}])
                continue
            if cur and cur_sec + sec > 13.5:
                groups.append(cur)
                cur, cur_sec = [], 0.0
            cur.append(d)
            cur_sec += sec
        if cur:
            groups.append(cur)
        for gi, g in enumerate(groups):
            ns = dict(s)
            ns["dialogue"] = g
            ns["duration"] = max(1, min(15, int(speech_seconds([x.get("line") or "" for x in g]) + 0.4)))
            if gi > 0:
                ns["action"] = (s.get("action") or "") + "（承接上一句，继续说完）"
                ns["transition"] = "cut"
            out.append(ns)
    return out


def missing_assets(pid, eid, shot_id=None):
    """④ 前的强校验：本集用到的角色/场景/道具必须有参考图，否则禁止出片。"""
    assets = list_assets(pid)
    amap = {a["name"]: a for a in assets}
    miss = []
    for s in list_shots(eid):
        if shot_id and s["id"] != shot_id:
            continue
        names = list(parse_json(s.get("chars"), [])) + list(parse_json(s.get("props"), []))
        slug = s.get("scene_slug") or ""
        loc = slug.split("·")[1].strip() if "·" in slug else slug
        if loc:
            names.append(loc)
        for n in names:
            a = amap.get(n)
            if not a:
                miss.append({"shot": s["idx"], "name": n, "kind": "missing"})
            elif not ((a.get("chosen_url") or a.get("image_url") or "").strip()):
                miss.append({"shot": s["idx"], "name": n, "kind": "no_image"})
    seen, out = set(), []
    for m in miss:
        k = (m["name"], m["kind"])
        if k in seen:
            continue
        seen.add(k)
        out.append(m)
    return out


def episode_status(ep, shots):
    if (ep.get("export_url") or "").strip():
        return "成片导出完成"
    if shots and all((s.get("video_url") or "").lower().endswith((".mp4", ".webm")) for s in shots):
        return "画面已生成"
    if shots or (ep.get("script_json") or "").strip() or (ep.get("novel_text") or "").strip():
        return "制作中"
    return "待制作"


def build_srt(ep, shots):
    """按镜头时长 + 台词字数分配时间轴，输出 SRT。"""
    def ts(sec):
        h = int(sec // 3600); m = int(sec % 3600 // 60); s = int(sec % 60); ms = int(round((sec - int(sec)) * 1000))
        return "%02d:%02d:%02d,%03d" % (h, m, s, min(999, ms))

    lines, n, t = [], 1, 0.0
    for s in shots:
        dur = float(s.get("duration") or 5)
        dlg = parse_json(s.get("dialogue_json"), [])
        if dlg:
            total_chars = sum(max(1, len(d.get("line") or "")) for d in dlg)
            cur = t
            for d in dlg:
                share = dur * max(1, len(d.get("line") or "")) / total_chars
                text = (d.get("line") or "").strip()
                if text:
                    lines.append("%d\n%s --> %s\n%s\n" % (n, ts(cur), ts(min(t + dur, cur + share)), text))
                    n += 1
                cur += share
        t += dur
    return "\n".join(lines)


def compute_state(pid=None):
    projects = [dict(r) for r in db().execute("SELECT * FROM projects ORDER BY id DESC")]
    if not projects:
        return {"projects": [], "project": None, "episodes": [], "assets": [],
                "shots": [], "jobs": [], "server": server_info(), "config": public_config(),
                "stats": {}, "steps": STEP_NAMES, "wf": [], "music": [], "voices": []}
    pid = pid or projects[0]["id"]
    project = next((p for p in projects if p["id"] == pid), projects[0])
    assets = list_assets(project["id"])
    episodes = [dict(r) for r in db().execute(
        "SELECT * FROM episodes WHERE project_id=? ORDER BY idx", (project["id"],))]
    shots = []
    flags = {}
    for ep in episodes:
        for s in list_shots(ep["id"]):
            f = shot_flags(project, s, assets)
            flags[s["id"]] = f
            s["flags"] = f
            s["chars_list"] = parse_json(s.get("chars"), [])
            s["props_list"] = parse_json(s.get("props"), [])
            s["dialogue"] = parse_json(s.get("dialogue_json"), [])
            s["refs_list"] = parse_json(s.get("refs_json"), [])
            s["blocking"] = parse_json(s.get("blocking_json"), {})
            s["episode_idx"] = ep["idx"]
            s["wf_key"] = (s.get("workflow") or "").strip() or (project.get("video_wf") or "director")
            shots.append(s)

    # 集级链路状态
    ep_state = []
    for ep in episodes:
        epshots = [s for s in shots if s["episode_id"] == ep["id"]]
        cur_p = proj_sig(project)
        outline_stale = bool(ep.get("outline_json")) and cur_p != (ep.get("outline_sig") or "")
        novel_stale = bool(ep.get("novel_text")) and (
            outline_stale or sha(ep.get("outline_json")) != (ep.get("novel_sig") or ""))
        script_stale = bool(ep.get("script_json")) and (
            novel_stale or sha(ep.get("novel_text")) != (ep.get("script_sig") or ""))
        board_stale = bool(epshots) and (script_stale or sha(ep.get("script_json")) != (ep.get("board_sig") or ""))
        n_block = sum(1 for s in epshots if s["flags"]["block_stale"])
        n_prompt = sum(1 for s in epshots if s["flags"]["prompt_stale"])
        n_gen = sum(1 for s in epshots if s["flags"]["gen_stale"])
        done = sum(1 for s in epshots if s["status"] == "done"
                   and str(s.get("video_url") or "").endswith((".mp4", ".webm")))
        miss = missing_assets(project["id"], ep["id"])
        perrs = []
        for s in epshots:
            perrs.extend(packet_errors(project, s, assets))
        label = episode_status(ep, epshots)
        if n_gen and label not in ("待制作",):
            label += "（%d 镜待重跑）" % n_gen
        ep_state.append({
            "id": ep["id"], "idx": ep["idx"], "title": ep.get("title"),
            "outline_stale": outline_stale, "novel_stale": novel_stale,
            "script_stale": script_stale, "board_stale": board_stale,
            "need_block": n_block, "need_prompt": n_prompt, "need_gen": n_gen,
            "generated": done, "shots": len(epshots),
            "locked": sum(1 for s in epshots if s.get("locked")),
            "archived": int(ep.get("archived") or 0),
            "status_label": label,
            "missing": miss,
            "packet_errors": perrs,
            "packet_bad": len(perrs),
            "has_novel": bool((ep.get("novel_text") or "").strip()),
            "has_script": bool((ep.get("script_json") or "").strip()),
            "has_srt": bool((ep.get("srt_text") or "").strip()),
            "export_url": ep.get("export_url") or "",
        })

    # 当前该做哪一步
    if not project.get("target_locked"):
        step = 0
    elif not episodes:
        step = 1
    else:
        ep = ep_state[0]
        if not ep["shots"]:
            step = 4 if ep.get("script_stale") is False and True else 3
        elif ep["need_block"]:
            step = 5
        elif ep["need_prompt"]:
            step = 6
        elif ep["need_gen"]:
            step = 7
        else:
            step = 8
        if not episodes[0].get("outline_json"):
            step = 1
        elif not episodes[0].get("novel_text"):
            step = 2
        elif not episodes[0].get("script_json"):
            step = 3
        elif not ep["shots"]:
            step = 4

    jobs = [dict(r) for r in db().execute(
        "SELECT * FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 30", (project["id"],))]
    for j in jobs:
        j["log"] = parse_json(j.get("log"), [])

    # 资产图片版本（每项资产一个列表，前端直接渲染）
    for a in assets:
        a["images"] = list_asset_images(a["id"])
        a["chosen_url"] = next((im["url"] for im in a["images"] if im.get("chosen")), a.get("image_url") or "")

    # ③ 展示「真正送进 H3 的提示词原文」：参考图编号 + 主提示 + 负面 + 硬约束
    amap = {a["name"]: a for a in assets}
    umap = {}
    for a in assets:
        for u in [a.get("chosen_url"), a.get("image_url")] + [im["url"] for im in a.get("images") or []]:
            if u:
                umap[u] = a["name"]
    for s in shots:
        names = list(s.get("chars_list") or []) + list(s.get("props_list") or [])
        slug = s.get("scene_slug") or ""
        loc = slug.split("·")[1].strip() if "·" in slug else ""
        if loc:
            names.append(loc)
        if (s.get("packet_json") or "").strip():
            _refs, labels = packet_ref_list(project, s, assets, materialize=False)
        else:
            labels = [n for n in names if n in amap and (amap[n].get("chosen_url") or amap[n].get("image_url"))]
        # ③ 必须和真正提交的一致：勾了「参考上一镜」且上一镜已有真视频，就多一张末帧参考
        prevs = [p for p in shots if p["episode_id"] == s["episode_id"] and p["idx"] == int(s["idx"]) - 1]
        prev_shot = prevs[0] if prevs else None
        if int(s.get("ref_prev") or 0):
            if prevs and str(prevs[0].get("video_url") or "").lower().endswith((".mp4", ".webm")):
                labels.append("上一镜末帧（延续同轴线/同光位/同身位）")
        s["h3_refs"] = len(labels)
        s["parts"] = parts_of(s, project, assets, prev_shot)
        s["h3_prompt"] = h3_prompt_text(s, project, len(labels), labels, assets, prev_shot) if (s.get("prompt_cn") or "").strip() else ""
        s["chat"] = [dict(r) for r in db().execute(
            "SELECT role,text,created_at FROM shot_chat WHERE shot_id=? ORDER BY id DESC LIMIT 24",
            (s["id"],))][::-1]

    total = len(shots)
    return {
        "projects": projects,
        "project": project,
        "episodes": episodes,
        "ep_state": ep_state,
        "assets": assets,
        "shots": shots,
        "jobs": jobs,
        "steps": STEP_NAMES,
        "step": step,
        "wf": [dict(r) for r in db().execute("SELECT * FROM wf_presets ORDER BY sort")],
        "music": [dict(r) for r in db().execute(
            "SELECT * FROM music WHERE project_id=? ORDER BY id DESC", (project["id"],))],
        "voices": [dict(r) for r in db().execute(
            "SELECT * FROM voices WHERE project_id=? ORDER BY id", (project["id"],))],
        "stats": {
            "shots": total,
            "locked": sum(1 for s in shots if s.get("locked")),
            "need_block": sum(1 for s in shots if s["flags"]["block_stale"]),
            "need_prompt": sum(1 for s in shots if s["flags"]["prompt_stale"]),
            "need_gen": sum(1 for s in shots if s["flags"]["gen_stale"]),
            "generated": sum(1 for s in shots if s["status"] == "done" and str(s.get("video_url") or "").endswith((".mp4", ".webm"))),
            "duration": sum(int(s.get("duration") or 5) for s in shots),
        },
        "server": server_info(),
        "config": public_config(),
    }


def server_info():
    cs = comfy_status()
    ok, msg = llm_available()
    return {
        "time": now(),
        "llm": {"ok": ok, "msg": msg},
        "comfy": {"ok": bool(cs["ok"]), "msg": cs["msg"], "version": cs.get("version", "")},
        "ffmpeg": bool(get_setting("ffmpeg_path") and os.path.exists(get_setting("ffmpeg_path"))),
        "port": PORT,
    }


# ---------------------------------------------------------------- 版本更新检查
_update_state = {"ts": 0, "result": None}


def _version_tuple(v):
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in nums) if nums else (0,)


def check_update():
    """拉取托管的版本清单，比对本地版本；未配置/联网失败均不打扰用户。"""
    if not UPDATE_MANIFEST_URL:
        return {"available": False, "configured": False}
    if time.time() - _update_state["ts"] < 300 and _update_state["result"] is not None:
        return _update_state["result"]
    result = {"available": False, "configured": True}
    try:
        req = urllib.request.Request(UPDATE_MANIFEST_URL, headers={"User-Agent": "nexus-studio"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = str(data.get("latest") or "").strip()
        if latest and _version_tuple(latest) > _version_tuple(APP_VERSION):
            result = {"available": True, "configured": True, "current": APP_VERSION,
                      "latest": latest, "note": str(data.get("note") or ""),
                      "qq": str(data.get("qq") or "")}
    except Exception as e:
        result = {"available": False, "configured": True, "offline": True, "msg": str(e)[:120]}
    _update_state.update({"ts": time.time(), "result": result})
    return result


def public_config():
    key = get_setting("llm_api_key")
    return {
        "llm_base_url": get_setting("llm_base_url"),
        "llm_model": get_setting("llm_model"),
        "llm_key_set": bool(key),
        "llm_key_mask": (key[:6] + "…" + key[-4:]) if key else "",
        "engine_mode": get_setting("engine_mode"),
        "comfy_base": get_setting("comfy_base"),
        "video_workflow": get_setting("video_workflow"),
        "ffmpeg_path": get_setting("ffmpeg_path"),
    }


# ---------------------------------------------------------------- 任务队列

_jobs_sem = threading.Semaphore(2)


def log_activity(pid, kind, message):
    with _db_lock:
        db().execute("INSERT INTO activity(project_id,kind,message,created_at) VALUES(?,?,?,?)",
                     (pid, kind, message, now()))
        db().commit()


def new_job(pid, eid, kind, scope):
    with _db_lock:
        cur = db().execute("INSERT INTO jobs(project_id,episode_id,kind,scope,status,progress,message,log,created_at,updated_at)"
                           " VALUES(?,?,?,?,'queued',0,?, '[]',?,?)",
                           (pid, eid, kind, scope, "排队中", now(), now()))
        db().commit()
        return cur.lastrowid


def job_update(jid, **kw):
    fields = ", ".join(f"{k}=?" for k in kw)
    with _db_lock:
        db().execute(f"UPDATE jobs SET {fields}, updated_at=? WHERE id=?",
                     (*kw.values(), now(), jid))
        db().commit()


def job_log(jid, msg, progress=None):
    row = db().execute("SELECT log, progress FROM jobs WHERE id=?", (jid,)).fetchone()
    logs = parse_json(row["log"] if row else "[]", [])
    if msg:
        logs.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": msg})
    kw = {"log": json.dumps(logs, ensure_ascii=False)}
    if progress is not None:
        kw["progress"] = max(int(row["progress"] or 0) if row else 0, int(progress))
    job_update(jid, **kw)


def run_async(fn, jid, *args):
    def wrapper():
        with _jobs_sem:
            try:
                job_update(jid, status="running", message="执行中")
                fn(jid, *args)
                job_update(jid, status="done", progress=100, message="完成")
            except Exception as e:
                traceback.print_exc()
                job_update(jid, status="failed", message=f"{type(e).__name__}: {e}")
                job_log(jid, f"失败：{e}")
    threading.Thread(target=wrapper, daemon=True).start()


# ---------------------------------------------------------------- 生成逻辑

def ctx_for(project, episode, shot=None, blocking=None, prompt=None):
    return {
        "project": project, "episode": episode,
        "outline": parse_json(episode.get("outline_json"), {}),
        "novel": episode.get("novel_text") or "",
        "script": parse_json(episode.get("script_json"), {}),
        "assets": list_assets(project["id"]),
        "shot": shot or {}, "blocking": blocking or {}, "prompt": prompt or {},
    }


def ensure_assets_from_outline(pid, outline):
    """把大纲里的角色/场景/道具同步成可锁定资产（已存在则不覆盖用户改动）。"""
    existing = {a["kind"] + ":" + a["name"] for a in list_assets(pid)}
    created = 0
    with _db_lock:
        for c in outline.get("cast", []):
            key = "char:" + c["name"]
            if key in existing:
                continue
            canon = (f"{c['name']}｜{c.get('age','')}｜{c.get('traits','')}"
                     f"｜服装与外形：{c.get('look','固定造型，发型与服装在所有镜头保持一致')}")
            db().execute("INSERT INTO assets(project_id,kind,name,canon,locked,sort,created_at,updated_at)"
                         " VALUES(?,?,?,?,0,?,?,?)",
                         (pid, "char", c["name"], canon, len(existing), now(), now()))
            created += 1
        for loc in outline.get("locations", []):
            key = "scene:" + loc["name"]
            if key in existing:
                continue
            db().execute("INSERT INTO assets(project_id,kind,name,canon,locked,sort,created_at,updated_at)"
                         " VALUES(?,?,?,?,0,?,?,?)",
                         (pid, "scene", loc["name"], f"{loc['name']}｜{loc.get('desc','')}", len(existing), now(), now()))
            created += 1
        for p in outline.get("props", []):
            key = "prop:" + p["name"]
            if key in existing:
                continue
            db().execute("INSERT INTO assets(project_id,kind,name,canon,locked,sort,created_at,updated_at)"
                         " VALUES(?,?,?,?,0,?,?,?)",
                         (pid, "prop", p["name"], f"{p['name']}｜{p.get('desc','')}", len(existing), now(), now()))
            created += 1
        db().commit()
    return created


def ensure_scene_assets(pid, script):
    """把剧本里真实出现的地点同步成场景资产；占位场景（主场/对手场/中转场）就地改名，不重复堆。"""
    placeholders = {"主场", "对手场", "中转场"}
    scenes = [a for a in list_assets(pid) if a["kind"] == "scene"]
    names = [a["name"] or "" for a in scenes]
    free = [a for a in scenes if (a["name"] or "") in placeholders and not a["locked"]]
    created = 0
    with _db_lock:
        for sc in script.get("scenes", []):
            loc = (sc.get("location") or "").strip()
            if not loc:
                continue
            if any(loc in n or n in loc for n in names if n):
                continue
            canon = (f"{loc}｜{sc.get('time', '')} · {sc.get('slug', '')}｜"
                     f"环境细节：{sc.get('action', '')[:46]}；光源方向固定，背景不出现真实品牌与门牌")
            if free:
                a = free.pop(0)
                db().execute("UPDATE assets SET name=?, canon=?, updated_at=? WHERE id=?",
                             (loc, canon, now(), a["id"]))
            else:
                db().execute("INSERT INTO assets(project_id,kind,name,canon,locked,sort,created_at,updated_at)"
                             " VALUES(?,?,?,?,0,?,?,?)",
                             (pid, "scene", loc, canon, 10 + created, now(), now()))
                created += 1
            names.append(loc)
        db().commit()
    return created


def write_outline(pid, eid, outline, jid=None):
    with _db_lock:
        db().execute("UPDATE episodes SET outline_json=?, outline_sig=?, title=?, updated_at=? WHERE id=?",
                     (json.dumps(outline, ensure_ascii=False), proj_sig(get_project(pid)),
                      outline.get("title"), now(), eid))
        db().commit()
    n = ensure_assets_from_outline(pid, outline)
    if jid:
        job_log(jid, f"大纲落库 · 三幕 {len(outline.get('acts', []))} 段 · 同步资产 {n} 项")


def do_outline(jid, pid, eid):
    project = get_project(pid)
    episode = get_episode(eid)
    job_log(jid, "读取目标锁定卡与创意", 8)
    out_head, msg = llm_available()
    outline = None
    if out_head and get_setting("engine_mode") != "local":
        try:
            job_log(jid, "调用模型生成三幕结构与人物关系", 20)
            outline = llm_json(
                "你是短剧总编剧。输出 JSON，字段：title,genre,logline,target,acts[{name,beat,episodes[{idx,title,hook,beats[],climax,payoff,duration}]}],cast[{name,role,age,traits,want,secret,look}],locations[{name,desc}],props[{name,desc}],style{palette,camera,music},risk[]。集数严格等于 target.episode_count。",
                json.dumps({"创意": project.get("premise"), "题材": project.get("genre"),
                            "平台": project.get("platform"), "受众": project.get("audience"),
                            "集数": project.get("episode_count"), "单集时长": project.get("episode_length"),
                            "风格锁": project.get("style_lock"), "禁区": project.get("forbidden")},
                           ensure_ascii=False), 6000)
        except Exception as e:
            job_log(jid, f"模型不可用，切换本地引擎（{str(e)[:80]}）", 30)
    if not outline:
        job_log(jid, "本地引擎：按题材模板生成三幕十二集骨架", 30)
        outline = engine.local_outline(ctx_for(project, episode))
    job_log(jid, "深度思考：结构体检 → 修复 → 复核", 60)
    for rnd in range(1, 4):
        rep = engine.audit_outline(outline)
        outline = rep["outline"]
        job_log(jid, f"第{rnd}轮体检：{len(rep['issues'])} 个问题已处理", 60 + rnd * 8)
    write_outline(pid, eid, outline, jid)
    log_activity(pid, "outline", "生成小说大纲")
    job_log(jid, f"大纲完成：{sum(len(a['episodes']) for a in outline.get('acts', []))} 集", 100)


def do_novel(jid, pid, eid):
    project = get_project(pid)
    episode = get_episode(eid)
    if int(episode.get("novel_manual") or 0):
        raise RuntimeError("本集正文已被你手动修改，AI 重写会覆盖。确认要重写请点「强制重写」（会先清掉手改标记）")
    outline = parse_json(episode.get("outline_json"), {})
    if not outline:
        raise RuntimeError("请先生成小说大纲")
    ctx = ctx_for(project, episode)
    text = None
    ok, msg = llm_available()
    if ok and get_setting("engine_mode") != "local":
        try:
            job_log(jid, "模型写作中（按锁定人物/场景/道具展开）", 20)
            text = llm_text(
                "你是短剧小说作者。按给定大纲写本集正文：冷调写实、句子短、画面感强；人物外形与道具必须与锁定资产一致；结尾留钩子。只输出正文，不要解释。",
                json.dumps({"大纲": outline, "本集": episode.get("idx"), "锁定资产": [
                    {"kind": a["kind"], "name": a["name"], "canon": a["canon"]} for a in ctx["assets"]]},
                    ensure_ascii=False), 4000)
        except Exception as e:
            job_log(jid, f"模型失败，本地引擎接手（{str(e)[:60]}）", 30)
    if not text:
        job_log(jid, "本地引擎：按本集钩子/爽点铺正文", 30)
        text = engine.local_novel(ctx)
    job_log(jid, f"正文 {len(text)} 字，开始深度修复（节奏/钩子/一致性）", 60)
    if len(text) < 320:
        text += "\n\n" + engine.local_novel(ctx)
        job_log(jid, "篇幅不足，已补写第二段", 70)
    with _db_lock:
        db().execute("UPDATE episodes SET novel_text=?, novel_sig=?, updated_at=? WHERE id=?",
                     (text, sha(outline), now(), eid))
        db().commit()
    log_activity(pid, "novel", f"生成第{episode.get('idx')}集正文 {len(text)} 字")
    job_log(jid, "正文落库（下游剧本已标记需重跑）", 100)


def do_script(jid, pid, eid):
    project = get_project(pid)
    episode = get_episode(eid)
    novel = episode.get("novel_text") or ""
    if not novel:
        raise RuntimeError("请先生成小说正文")
    ctx = ctx_for(project, episode)
    script = None
    ok, msg = llm_available()
    if ok and get_setting("engine_mode") != "local":
        try:
            job_log(jid, "模型改写剧本（场次 / 动作 / 对白）", 20)
            script = llm_json(
                "你是短剧编剧。把小说改成分镜可执行的剧本 JSON：{episode,logline,scenes[{no,slug,time,location,cast,purpose,action,dialogue[{sp,line,emotion}],duration}],note}。每场必须有动作描述，台词单句不超过 24 字。",
                json.dumps({"小说": novel, "锁定资产": ctx["assets"]}, ensure_ascii=False), 5000)
        except Exception as e:
            job_log(jid, f"模型失败，本地引擎接手（{str(e)[:60]}）", 30)
    if not script:
        job_log(jid, "本地引擎：拆 6 场，台词按 4.5 字/秒控长", 30)
        script = engine.local_script(ctx)
    for rnd in range(1, 4):
        rep = engine.audit_script(script)
        script = rep["script"]
        job_log(jid, f"第{rnd}轮剧本体检：{len(rep['issues'])} 条提示", 55 + rnd * 10)
    with _db_lock:
        db().execute("UPDATE episodes SET script_json=?, script_sig=?, updated_at=? WHERE id=?",
                     (json.dumps(script, ensure_ascii=False), sha(novel), now(), eid))
        db().commit()
    log_activity(pid, "script", f"生成剧本 {len(script.get('scenes', []))} 场")
    n_scene = ensure_scene_assets(pid, script)
    if n_scene:
        job_log(jid, f"同步场景资产 {n_scene} 个（来自剧本真实地点）")
    job_log(jid, "剧本落库（分镜已标记需重跑）", 100)


def do_board(jid, pid, eid):
    project = get_project(pid)
    episode = get_episode(eid)
    script = parse_json(episode.get("script_json"), {})
    if not script:
        raise RuntimeError("请先生成剧本")
    ctx = ctx_for(project, episode)
    job_log(jid, "拆镜：按场次生成建立镜 / 对白镜 / 反应镜", 20)
    shots = engine.local_storyboard(ctx)
    ok, msg = llm_available()
    if ok and get_setting("engine_mode") != "local":
        try:
            job_log(jid, "模型精修分镜表（景别/运镜/时长）", 40)
            data = llm_json(
                "你是分镜师。把剧本拆成 4-10 秒的可生成镜头，输出 JSON {shots:[{idx,scene_slug,shot_size,camera,movement,duration,chars,props,action,emotion,dialogue:[{sp,line}],transition}]}。相邻镜头不要同景别。",
                json.dumps({"剧本": script, "锁定资产": ctx["assets"]}, ensure_ascii=False), 6000)
            if data.get("shots"):
                shots = data["shots"]
        except Exception as e:
            job_log(jid, f"模型失败，使用本地分镜表（{str(e)[:50]}）", 45)
    rep = engine.audit_storyboard(shots, ctx["assets"])
    shots = rep["shots"]
    # PRD 六：本集视频工作流作为分镜 AI 链路默认模板，单镜可在分镜表里单独覆盖
    wf = wf_of(project)
    shots, n_wf = apply_wf_to_board(shots, wf)
    if n_wf:
        job_log(jid, "按工作流「%s」的镜头规范收口 %d 处（%s）" % (wf.get("name"), n_wf, wf.get("shot_rule")))
    # 时长硬规则：台词朗读时长 ≤ 镜头时长；单镜 ≤15s；超长台词自动拆镜
    shots = normalize_shots(shots)
    job_log(jid, f"分镜体检：{len(rep['issues'])} 条，合计 {rep['total_duration']} 秒；时长已按台词朗读重算（≤15s）", 70)

    with _db_lock:
        db().execute("DELETE FROM shots WHERE episode_id=?", (eid,))
        for i, s in enumerate(shots):
            db().execute(
                "INSERT INTO shots(project_id,episode_id,idx,duration,shot_size,camera,movement,scene_slug,"
                "chars,props,action,emotion,dialogue_json,transition,workflow,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, eid, i + 1, int(s.get("duration") or 5), s.get("shot_size"), s.get("camera"),
                 s.get("movement"), s.get("scene_slug"),
                 json.dumps(s.get("chars") or [], ensure_ascii=False),
                 json.dumps(s.get("props") or [], ensure_ascii=False),
                 s.get("action"), s.get("emotion"),
                 json.dumps(s.get("dialogue") or [], ensure_ascii=False),
                 s.get("transition"), (project.get("video_wf") or "director"), now(), now()))
        db().execute("UPDATE episodes SET board_sig=?, updated_at=? WHERE id=?",
                     (sha(episode.get("script_json")), now(), eid))
        db().commit()
    log_activity(pid, "board", f"分镜设计 {len(shots)} 镜")
    job_log(jid, f"分镜落库 {len(shots)} 镜（②③④ 全部标记待执行）", 100)


def do_blocking(jid, pid, eid, shot_id=None):
    project = get_project(pid)
    episode = get_episode(eid)
    assets = list_assets(pid)
    targets = [s for s in list_shots(eid) if (shot_id is None or s["id"] == shot_id)]
    if not targets:
        raise RuntimeError("没有可调度的镜头")
    total = len(targets)
    for i, shot in enumerate(targets):
        ctx = ctx_for(project, episode, shot)
        b = engine.local_blocking(ctx)
        with _db_lock:
            db().execute("UPDATE shots SET blocking_json=?, sig1_at_block=?, updated_at=? WHERE id=?",
                         (json.dumps(b, ensure_ascii=False),
                          shot_sig1(project, shot, assets), now(), shot["id"]))
            db().commit()
        job_log(jid, f"第{shot['idx']}镜调度完成 · {b['camera']['height']} / {b['camera']['side']}",
                int((i + 1) / total * 100))
    log_activity(pid, "blocking", f"场面调度 {total} 镜")
    job_log(jid, f"调度落库 {total} 镜（③④ 标记待重跑）", 100)


def repair_dialogue_prompts(pid, eid):
    """历史 bug 修复：有台词但提示词里写「无对白」的镜头，按当前格式重写提示词。"""
    project = get_project(pid)
    assets = list_assets(pid)
    episode = get_episode(eid)
    fixed, kept = [], []
    for shot in list_shots(eid):
        shot = dict(shot,
                    dialogue=parse_json(shot.get("dialogue_json"), []),
                    chars=parse_json(shot.get("chars"), []),
                    props=parse_json(shot.get("props"), []))
        dlg = shot["dialogue"]
        if not dlg:
            continue
        line = "；".join((d.get("line") or "") for d in dlg if (d.get("line") or "").strip())
        if line and line in (shot.get("prompt_cn") or ""):
            kept.append(shot["idx"])
            continue
        blocking = parse_json(shot.get("blocking_json"), {})
        if not blocking:
            blocking = engine.local_blocking(ctx_for(project, episode, shot))
        if (project.get("prompt_mode") or "cn") == "h3ref":
            p = engine.local_prompt_v2({"project": project, "assets": assets, "shot": shot,
                                        "blocking": blocking, "perf": perf_of(shot)})
        else:
            p = engine.local_prompt({"project": project, "assets": assets, "shot": shot,
                                     "blocking": blocking})
        p = apply_wf_to_prompt(p, wf_of(project, shot))
        with _db_lock:
            db().execute("UPDATE shots SET prompt_cn=?, prompt_en=?, prompt_neg=?, sig2_at_prompt=?, updated_at=? WHERE id=?",
                         (p["cn"], p["en"], p["neg"], shot_sig2(shot), now(), shot["id"]))
            db().commit()
        fixed.append(shot["idx"])
    return fixed, kept


def do_prompt(jid, pid, eid, shot_id=None):
    project = get_project(pid)
    episode = get_episode(eid)
    assets = list_assets(pid)
    targets = [s for s in list_shots(eid) if (shot_id is None or s["id"] == shot_id)]
    if not targets:
        raise RuntimeError("没有可生成提示词的镜头")
    total = len(targets)
    skipped = 0
    for i, shot in enumerate(targets):
        # DB 里数组是 JSON 字符串：先还原，否则台词/角色会被当成 0 条，提示词写成「无对白」
        shot = dict(shot,
                    dialogue=parse_json(shot.get("dialogue_json"), []),
                    chars=parse_json(shot.get("chars"), []),
                    props=parse_json(shot.get("props"), []))
        if int(shot.get("prompt_lock") or 0):
            skipped += 1
            job_log(jid, f"第{shot['idx']}镜提示词已锁，跳过不覆盖", int((i + 1) / total * 100))
            continue
        blocking = parse_json(shot.get("blocking_json"), {})
        if not blocking:
            job_log(jid, f"第{shot['idx']}镜缺调度，先补调度", None)
            blocking = engine.local_blocking(ctx_for(project, episode, shot))
        if (project.get("prompt_mode") or "cn") == "h3ref":
            p = engine.local_prompt_v2({"project": project, "assets": assets, "shot": shot,
                                        "blocking": blocking, "perf": perf_of(shot)})
        else:
            p = engine.local_prompt({"project": project, "assets": assets, "shot": shot, "blocking": blocking})
        p = apply_wf_to_prompt(p, wf_of(project, shot))
        with _db_lock:
            db().execute("UPDATE shots SET prompt_cn=?, prompt_en=?, prompt_neg=?, sig2_at_prompt=?, updated_at=? WHERE id=?",
                         (p["cn"], p["en"], p["neg"], shot_sig2(shot), now(), shot["id"]))
            db().commit()
        job_log(jid, f"第{shot['idx']}镜提示词生成 · {len(p['cn'])} 字", int((i + 1) / total * 100))
    log_activity(pid, "prompt", f"视频提示词 {total} 镜" + (f"（锁定跳过 {skipped}）" if skipped else ""))
    job_log(jid, f"提示词落库 {total - skipped} 镜（④ 标记待重跑）", 100)


def do_optimize(jid, pid, eid, kind, shot_id=None):
    """AI 深度思考优化修复：体检 → 修 → 复核，逐条落日志。"""
    project = get_project(pid)
    episode = get_episode(eid)
    assets = list_assets(pid)
    job_log(jid, f"深度优化：{kind} · 三轮（诊断 / 修复 / 复核）", 5)

    if kind == "outline":
        outline = parse_json(episode.get("outline_json"), {})
        if not outline:
            raise RuntimeError("还没有大纲，先生成")
        total = 0
        for rnd in range(1, 4):
            rep = engine.audit_outline(json.loads(json.dumps(outline)))
            outline = rep["outline"]
            total += len(rep["issues"])
            job_log(jid, f"第{rnd}轮：{len(rep['issues'])} 条结构问题已修", 20 + rnd * 22)
        write_outline(pid, eid, outline)
        job_log(jid, f"复核完成：集数/钩子/爽点对齐（累计 {total} 条）", 100)
        return

    if kind == "novel":
        text = episode.get("novel_text") or ""
        if not text:
            raise RuntimeError("还没有正文，先生成")
        lead = next((c["name"] for c in (parse_json(episode.get("outline_json"), {}).get("cast") or [])), "")
        fixed = 0
        if len(text) < 600:
            text = text + "\n\n" + engine.local_novel(ctx_for(project, episode))
            fixed += 1
            job_log(jid, "篇幅不足 600 字，补写一段", 30)
        if lead and lead not in text:
            fixed += 1
            job_log(jid, "主角名没出现，正文与锁定角色脱节（建议补写，已在复核里标注）", 40)
        if not any(k in text[-120:] for k in ("不知道", "停住", "门口", "名字", "名单", "车")):
            text = text.rstrip() + "\n\n她不知道，门外那辆车已经停了很久。"
            fixed += 1
            job_log(jid, "结尾缺钩子，已补一句留白", 60)
        with _db_lock:
            db().execute("UPDATE episodes SET novel_text=?, novel_sig=?, updated_at=? WHERE id=?",
                         (text, sha(episode.get("outline_json")), now(), eid))
            db().commit()
        job_log(jid, f"复核完成（{fixed} 处修复）· 下游剧本/分镜已标记需重跑", 100)
        return

    if kind == "script":
        script = parse_json(episode.get("script_json"), {})
        if not script:
            raise RuntimeError("还没有剧本，先生成")
        fixed = 0
        for sc in script.get("scenes", []):
            new_lines = []
            for d in sc.get("dialogue", []):
                line = (d.get("line") or "")
                if len(line) > 24:
                    # 在中间标点处拆成两句，并插一个反应节拍
                    cut = max(line.rfind("，", 0, len(line) * 2 // 3), line.rfind("。", 0, len(line) * 2 // 3))
                    if cut > 6:
                        new_lines.append({**d, "line": line[:cut + 1]})
                        new_lines.append({"sp": d.get("sp"), "line": line[cut + 1:], "emotion": "承接"})
                        fixed += 1
                        continue
                new_lines.append(d)
            sc["dialogue"] = new_lines
        rep = engine.audit_script(script)
        script = rep["script"]
        with _db_lock:
            db().execute("UPDATE episodes SET script_json=?, script_sig=?, updated_at=? WHERE id=?",
                         (json.dumps(script, ensure_ascii=False), sha(episode.get("novel_text")), now(), eid))
            db().commit()
        job_log(jid, f"复核完成：长台词拆句 {fixed} 处 · 分镜已标记需重跑", 100)
        return

    if kind == "board":
        rows = [dict(r) for r in db().execute("SELECT * FROM shots WHERE episode_id=? ORDER BY idx", (eid,))]
        if shot_id:
            rows = [r for r in rows if r["id"] == shot_id]
        if not rows:
            raise RuntimeError("没有镜头")
        payload = []
        for r in rows:
            payload.append({"idx": r["idx"], "shot_size": r["shot_size"], "duration": r["duration"],
                            "scene_slug": r["scene_slug"], "chars": parse_json(r["chars"], [])})
        rep = engine.audit_storyboard(payload, assets)
        for item in rep["shots"]:
            r = next((x for x in rows if x["idx"] == item["idx"]), None)
            if not r:
                continue
            with _db_lock:
                db().execute("UPDATE shots SET shot_size=?, duration=?, updated_at=? WHERE id=?",
                             (item["shot_size"], item["duration"], now(), r["id"]))
                db().commit()
        job_log(jid, f"分镜体检 {len(rep['issues'])} 条 · 合计 {rep['total_duration']}s", 60)
        job_log(jid, "复核完成：景别重复与异常时长已修（②③④ 相应镜头标记需重跑）", 100)
        return

    if kind in ("blocking", "prompt"):
        fixed_total = 0
        for rnd in range(1, 4):
            round_fixed = 0
            targets = [s for s in list_shots(eid) if (shot_id is None or s["id"] == shot_id)]
            for shot in targets:
                if kind == "blocking":
                    b = parse_json(shot.get("blocking_json"), {})
                    changed = False
                    if not b:
                        b = engine.local_blocking(ctx_for(project, episode, shot))
                        changed = True
                    if not (b.get("camera") or {}).get("side"):
                        b.setdefault("camera", {})["side"] = "轴线左侧"
                        changed = True
                    if not (b.get("light") or {}).get("temp"):
                        b.setdefault("light", {})["temp"] = "5000K 偏冷"
                        changed = True
                    if len(b.get("beats") or []) < 3:
                        b["beats"] = engine.local_blocking(ctx_for(project, episode, shot))["beats"]
                        changed = True
                    if changed:
                        with _db_lock:
                            db().execute("UPDATE shots SET blocking_json=?, sig1_at_block=?, updated_at=? WHERE id=?",
                                         (json.dumps(b, ensure_ascii=False), shot_sig1(project, shot, assets), now(), shot["id"]))
                            db().commit()
                        fixed_total += 1
                        round_fixed += 1
                else:
                    shot_audit = dict(shot, chars=parse_json(shot.get("chars"), []),
                                      props=parse_json(shot.get("props"), []))
                    rep = engine.audit_prompt({"shot": shot_audit,
                                               "prompt": {"cn": shot["prompt_cn"], "en": shot["prompt_en"],
                                                          "neg": shot["prompt_neg"]},
                                               "blocking": parse_json(shot.get("blocking_json"), {}),
                                               "assets": assets})
                    new_cn = rep["prompt"]["cn"]
                    new_neg = rep["prompt"]["neg"]
                    new_dur = rep["shot_patch"]["duration"]
                    if new_cn != shot["prompt_cn"] or new_neg != shot["prompt_neg"] or int(new_dur) != int(shot["duration"]):
                        patched = dict(shot, duration=new_dur, prompt_cn=new_cn, prompt_neg=new_neg,
                                       prompt_en=rep["prompt"]["en"])
                        with _db_lock:
                            db().execute("UPDATE shots SET prompt_cn=?, prompt_en=?, prompt_neg=?, duration=?, "
                                         "sig2_at_prompt=?, updated_at=? WHERE id=?",
                                         (new_cn, rep["prompt"]["en"], new_neg, new_dur,
                                          shot_sig2(patched), now(), shot["id"]))
                            db().commit()
                        fixed_total += 1
                        round_fixed += 1
            job_log(jid, f"第{rnd}轮：本轮新增 {round_fixed} 处 · 累计 {fixed_total} 处", 20 + rnd * 24)
        job_log(jid, f"复核完成：轴线/光位/节拍/语速/负面词已对齐（累计 {fixed_total} 处，收敛后不再变动）", 100)
        return

    job_log(jid, "该阶段没有独立优化规则", 100)


def do_video(jid, pid, eid, shot_id=None):
    project = get_project(pid)
    episode = get_episode(eid)
    targets = [s for s in list_shots(eid) if (shot_id is None or s["id"] == shot_id)]
    if shot_id is None:
        # 「全部生成」只跑真正需要重跑的镜头，已出片且没改过的不重烧 GPU
        assets0 = list_assets(pid)
        targets = [s for s in targets if shot_flags(project, s, assets0)["gen_stale"]]
    if not targets:
        raise RuntimeError("没有需要重跑的镜头（全部都已是干净成片）")
    # 单镜头数据包强校验：12 条缺一不可，缺任何一项直接阻断，不进 ComfyUI 队列
    all_assets = list_assets(pid)
    perrs = []
    for s in targets:
        perrs.extend(packet_errors(project, s, all_assets))
    if perrs:
        lines = "；".join("第%d镜 %s：%s" % (e["shot"], e["field"], e["msg"]) for e in perrs[:10])
        with _db_lock:
            for s in targets:
                own = [e for e in perrs if e["shot_id"] == s["id"]]
                if own:
                    db().execute("UPDATE shots SET status='blocked', error=? WHERE id=?",
                                 ("数据包缺 %d 项：%s" % (len(own), own[0]["msg"])[:200], s["id"]))
            db().commit()
        raise RuntimeError("单镜头数据包不完整，已阻断渲染：" + lines
                           + ("（共 %d 项）" % len(perrs) if len(perrs) > 10 else "")
                           + "。点第③步「数据包」逐镜补齐后再生成。")
    cs = comfy_status(force=True)
    total = len(targets)
    # 本机实测：8 步 / 768x1344 / 5s 约 754s，拆成「模型初始化 ~100s + 每步 ~80s」，按分辨率面积折算
    v_steps = int(get_setting("video_steps") or 8)
    v_scale = float(get_setting("video_scale") or 1.0)
    est_sec = 100 + v_steps * 80 * max(0.35, v_scale * v_scale)
    for i, shot in enumerate(targets):
        flags = shot_flags(project, shot, list_assets(pid))
        if flags["prompt_stale"] or not shot.get("prompt_cn"):
            job_log(jid, f"第{shot['idx']}镜提示词已失效，跳过（先跑③）", int(i / total * 100))
            continue
        with _db_lock:
            db().execute("UPDATE shots SET status='running', updated_at=? WHERE id=?", (now(), shot["id"]))
            db().commit()
        # 按本镜绑定的数据包取参考图（角色/场景/道具/首尾帧/参考视频），不再吃全局名字映射
        assets_map = {a["name"]: a for a in all_assets}
        refs, labels = packet_ref_list(project, shot, all_assets)
        with _db_lock:
            db().execute("UPDATE shots SET refs_json=? WHERE id=?", (json.dumps(refs, ensure_ascii=False), shot["id"]))
            db().commit()
        prev_shot = next((x for x in list_shots(eid) if x["idx"] == int(shot["idx"]) - 1), None)
        # 视频参考：把上一镜的末帧抽出来当第 N 张参考图，锁住同一条轴线、同一光位、同一身位
        if int(shot.get("ref_prev") or 0):
            prev = [s for s in list_shots(eid) if s["idx"] == int(shot["idx"]) - 1]
            if prev and (prev[0].get("video_url") or "").lower().endswith((".mp4", ".webm")):
                try:
                    frame = extract_last_frame(prev[0]["video_url"], int(shot["idx"]))
                    if frame not in refs and len(refs) < 9:
                        refs.append(frame)
                        labels.append("上一镜末帧（延续同轴线/同光位/同身位）")
                    job_log(jid, f"第{shot['idx']}镜已接入上一镜末帧参考（{frame}）")
                except Exception as e:
                    job_log(jid, f"第{shot['idx']}镜上一镜末帧抽取失败：{str(e)[:80]}")
            else:
                job_log(jid, f"第{shot['idx']}镜：上一镜还没有真视频，跳过末帧参考")
        # 真实参考图清单落库（含末帧），前端和 ③ 原文据此对齐
        with _db_lock:
            db().execute("UPDATE shots SET refs_json=? WHERE id=?",
                         (json.dumps(refs, ensure_ascii=False), shot["id"]))
            db().commit()
        if refs:
            job_log(jid, f"第{shot['idx']}镜绑定参考图 {len(refs)} 张（资产库 canon+图）")
        if not comfy_status(force=True)["ok"]:
            job_log(jid, f"第{shot['idx']}镜：ComfyUI 不通，先自动拉起再提交")
            cs = ensure_comfy()
        else:
            cs = comfy_status(force=True)
        if not cs["ok"]:
            # 真离线：直接判失败，绝不用预演帧冒充真片
            with _db_lock:
                db().execute("UPDATE shots SET status='failed', error=?, updated_at=? WHERE id=?",
                             (f"ComfyUI 离线，未提交真生成：{cs.get('msg','')[:60]}", now(), shot["id"]))
                db().commit()
            job_log(jid, f"第{shot['idx']}镜失败：ComfyUI 离线（{cs.get('msg','')[:60]}）",
                    int((i + 1) / total * 100))
            continue
        try:
            job_log(jid, f"第{shot['idx']}镜提交 ComfyUI", int(i / total * 100))
            sw_, sh_ = h3_size_for(project, shot)
            est_shot = 100 + v_steps * 80 * max(0.2, (sw_ * sh_) / (768 * 1344))
            preset = wf_of(project, shot)
            wf_name = (preset.get("wf_file") or "").strip()
            if wf_name and not wf_name.lower().endswith(".json"):
                wf_name = ""
            if wf_name and not os.path.exists(os.path.join(WORKFLOW_DIR, wf_name)):
                job_log(jid, f"第{shot['idx']}镜：工作流「{preset.get('name')}」的 {wf_name} 未找到，回落内置 H3")
                wf_name = ""
            if not wf_name:
                wf_name = pick_video_workflow()
            job_log(jid, f"第{shot['idx']}镜视频工作流：{preset.get('name')}"
                         f"{'（单镜覆盖）' if (shot.get('workflow') or '').strip() else '（集级默认）'}")
            if wf_name:
                wf = load_workflow(wf_name)
                inject_workflow(wf, shot)
                job_log(jid, f"第{shot['idx']}镜用工作流文件 {wf_name}")
            else:
                ref_files = stage_refs_for_comfy(shot, refs)
                wf = build_h3_workflow(shot, project, ref_files, labels, list(assets_map.values()), prev_shot)
                sw, sh = h3_size_for(project, shot)
                job_log(jid, f"第{shot['idx']}镜用内置 H3 工作流（参考图 {len(ref_files)} 张 · "
                             f"{shot.get('res_tier') or '默认档'} {sw}x{sh} · {int(get_setting('video_steps') or 8)} 步）")
            stepped = [False]

            def tick(elapsed, _i=i, _total=total, _est=est_shot, _tier=(shot.get("res_tier") or "默认档")):
                # 进度条同步：按本机实测（约 80s/步 + 100s 模型初始化）折算，进度条不再空转
                inner = min(96, int(elapsed / max(1.0, _est) * 100))
                pct = int(((_i * 100) + inner) / max(1, _total))
                if stepped[0]:
                    job_log(jid, None, pct)
                elif elapsed % 10 == 0 or elapsed < 5:
                    job_log(jid, f"第{shot['idx']}镜[{_tier}]生成中 {elapsed}s / 预计 {int(_est)}s（本镜 {inner}%）", pct)
                else:
                    job_log(jid, None, pct)

            def steps(v, m, _i=i, _total=total):
                if not m:
                    return
                stepped[0] = True
                inner = min(96, int(90 * v / m))
                pct = int(((_i * 100) + inner) / max(1, _total))
                job_log(jid, f"第{shot['idx']}镜采样生成中 {v}/{m} 步", pct)

            cid = str(uuid.uuid4())
            stop = threading.Event()
            ready = threading.Event()
            threading.Thread(target=comfy_progress_listener, args=(None, cid, steps, stop, ready), daemon=True).start()
            ready.wait(1.0)
            pid_comfy = comfy_submit(wf, cid)

            fileinfo = comfy_wait(pid_comfy, timeout=2400, on_tick=tick, client_id=cid, progress_stop=stop)
            url = comfy_download(fileinfo)
            # 缩略图直接用真片首帧，卡片/合集里看到的就是成片画面，不是预演帧
            thumb = make_video_thumb(url, shot["idx"], shot["id"])
            with _db_lock:
                db().execute("UPDATE shots SET status='done', video_url=?, thumb_url=?, sig3_at_gen=?, "
                             "error='', updated_at=? WHERE id=?",
                             (url, thumb or f"/api/shots/{shot['id']}/frame.svg", shot_sig3(shot, project),
                              now(), shot["id"]))
                db().commit()
            job_log(jid, f"第{shot['idx']}镜完成 → {url}", int((i + 1) / total * 100))
        except Exception as e:
            # 真视频没成就是失败：绝不拿预演帧顶替，避免合集里混进假片
            with _db_lock:
                db().execute("UPDATE shots SET status='failed', thumb_url=?, "
                             "error=?, updated_at=? WHERE id=?",
                             (f"/api/shots/{shot['id']}/frame.svg",
                              ("生成失败：" + str(e))[:300], now(), shot["id"]))
                db().commit()
            job_log(jid, f"第{shot['idx']}镜失败：{str(e)[:120]}", None)
    log_activity(pid, "video", f"生成视频 {total} 镜")
    job_log(jid, "生成任务结束", 100)


def load_workflow(name):
    p = os.path.join(WORKFLOW_DIR, name)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "prompt" in data and isinstance(data["prompt"], dict):
        return data["prompt"]
    return data


def inject_workflow(wf, shot):
    """按 class_type 注入提示词 / 时长 / 种子（与常见 H3/Wan 工作流兼容）。"""
    prompt = shot.get("prompt_cn") or ""
    negative = shot.get("prompt_neg") or ""
    dur = int(shot.get("duration") or 5)
    seed = int(hashlib.sha1(shot.get("prompt_cn", "").encode()).hexdigest()[:8], 16)
    for nid, node in wf.items():
        ct = node.get("class_type", "")
        inp = node.setdefault("inputs", {})
        title = (node.get("_meta") or {}).get("title", "")
        if ct in ("PrimitiveStringMultiline", "PrimitiveString") and "value" in inp:
            if "负" in title or "neg" in title.lower():
                inp["value"] = negative
            elif "提示" in title or "prompt" in title.lower() or inp.get("value") == "":
                inp["value"] = prompt
        elif ct == "CLIPTextEncode" and "text" in inp:
            inp["text"] = prompt
        if "seed" in inp and isinstance(inp["seed"], int) and ct in ("KSampler", "RandomNoise", "SamplerCustomAdvanced"):
            inp["seed"] = seed
        if "noise_seed" in inp and isinstance(inp["noise_seed"], int):
            inp["noise_seed"] = seed
        if ct == "PrimitiveFloat" and "value" in inp and ("时长" in title or "duration" in title.lower()):
            inp["value"] = float(dur)


# ---------------------------------------------------------------- 合集

def reel_payload(pid, eid=None):
    project = get_project(pid)
    eps = [dict(r) for r in db().execute("SELECT * FROM episodes WHERE project_id=? ORDER BY idx", (pid,))]
    shots = []
    for ep in eps:
        if eid and ep["id"] != int(eid):
            continue
        for s in list_shots(ep["id"]):
            shots.append({
                "id": s["id"], "idx": s["idx"], "duration": int(s.get("duration") or 5),
                "frame": f"/api/shots/{s['id']}/frame.svg",
                "thumb": s.get("thumb_url") or f"/api/shots/{s['id']}/frame.svg",
                "video": s.get("video_url") or "",
                "status": s.get("status"),
                "dialogue": parse_json(s.get("dialogue_json"), []),
                "shot_size": s.get("shot_size"),
                "scene": s.get("scene_slug"),
            })
    return {"project": {"id": project["id"], "title": project["title"],
                        "ratio": project.get("ratio"), "style": project.get("style_lock")},
            "shots": shots,
            "total": sum(s["duration"] for s in shots)}


REEL_TEMPLATE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>合集 · {{TITLE}}</title>
<style>
html,body{margin:0;height:100%;background:#07080a;color:#e8ecf2;font-family:"Microsoft YaHei",sans-serif;overflow:hidden}
#stage{position:relative;width:100vw;height:100vh;display:flex;align-items:center;justify-content:center}
#box{position:relative;background:#000;box-shadow:0 40px 120px rgba(0,0,0,.8);overflow:hidden}
.slide{position:absolute;inset:0;opacity:0;transition:opacity .35s}
.slide.on{opacity:1}
.slide img{width:100%;height:100%;object-fit:cover;animation:kb 8s linear both}
@keyframes kb{from{transform:scale(1.02)}to{transform:scale(1.09)}}
#sub{position:absolute;left:6%;right:6%;bottom:7%;text-align:center;font-size:min(2.6vw,26px);line-height:1.5;
background:rgba(0,0,0,.42);padding:10px 16px;border-radius:6px;text-shadow:0 2px 8px #000}
#hud{position:fixed;top:0;left:0;right:0;height:46px;display:flex;align-items:center;gap:14px;padding:0 18px;
background:linear-gradient(#000c,#0000);font-size:13px;letter-spacing:.5px;z-index:9}
#bar{position:fixed;bottom:0;left:0;height:3px;background:#57d9a3;width:0;transition:width .2s;z-index:9}
.btn{border:1px solid #2a3238;background:#12161a;color:#cfd8e3;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px}
#list{position:fixed;right:0;top:46px;bottom:0;width:220px;background:#0b0e11cc;overflow:auto;font-size:12px;display:none}
#list div{padding:8px 12px;border-bottom:1px solid #161b20;cursor:pointer;color:#8fa0b0}
</style>
<div id="hud"><b>{{TITLE}}</b><span style="color:#7b8a99">自动合集 · {{N}} 镜 · {{TOTAL}}s</span>
<span style="flex:1"></span><button class="btn" onclick="tog()">播放/暂停</button>
<button class="btn" onclick="lst()">镜头表</button></div>
<div id="stage"><div id="box"></div></div>
<div id="sub"></div><div id="bar"></div><div id="list"></div>
<script>
const SHOTS={{DATA}};
const box=document.getElementById('box'),sub=document.getElementById('sub'),bar=document.getElementById('bar');
function fit(){const r=window.innerWidth/window.innerHeight,ar=SHOTS.ar||0.5625;
 if(ar<r){box.style.height='100vh';box.style.width=(window.innerHeight*ar)+'px'}else{box.style.width='100vw';box.style.height=(window.innerWidth/ar)+'px'}}
fit();addEventListener('resize',fit);
let i=0,timer=null,paused=false;
SHOTS.shots.forEach((s,k)=>{const d=document.createElement('div');d.className='slide';d.dataset.k=k;
 d.innerHTML= s.video_data ? '<video src="'+s.video_data+'" muted playsinline autoplay></video>'
                           : '<img src="'+s.frame+'">';box.appendChild(d)});
const slides=[...box.children];
function show(k){i=k;slides.forEach((s,n)=>s.classList.toggle('on',n===k));
 const s=SHOTS.shots[k];sub.textContent=(s.dialogue||[]).map(d=>d.sp+'：'+d.line).join('  ')||'';
 document.getElementById('bar').style.width=((k+1)/SHOTS.shots.length*100)+'%';
 const v=slides[k].querySelector('video'); if(v){v.currentTime=0; const p=v.play(); if(p&&p.catch)p.catch(()=>{});} next()}
function next(){clearTimeout(timer);if(paused)return;const s=SHOTS.shots[i];
 const v=slides[i].querySelector('video');
 if(v){v.onended=()=>{if(i<SHOTS.shots.length-1)show(i+1);else paused=true};return}
 timer=setTimeout(()=>{if(i<SHOTS.shots.length-1)show(i+1);else paused=true},(s.duration||5)*1000)}
function tog(){paused=!paused;if(!paused)next()}
function lst(){const l=document.getElementById('list');l.style.display=l.style.display==='block'?'none':'block'}
SHOTS.shots.forEach((s,k)=>{if(!document.getElementById('list').childElementCount){};const d=document.createElement('div');
 d.textContent=('SHOT '+String(s.idx).padStart(2,'0')+' · '+s.shot_size+' · '+s.duration+'s');
 d.onclick=()=>{paused=true;show(k)};document.getElementById('list').appendChild(d)});
show(0);
document.onkeydown=e=>{if(e.code==='Space'){e.preventDefault();tog()}if(e.code==='ArrowRight')show(Math.min(i+1,SHOTS.shots.length-1))};
</script></html>"""


def export_reel(pid, eid=None, shot_ids=None):
    data = reel_payload(pid, eid)
    if shot_ids:
        if isinstance(shot_ids, str):
            shot_ids = [x for x in shot_ids.split(",") if x.strip()]
        keep = {int(x) for x in shot_ids}
        data["shots"] = [s for s in data["shots"] if int(s["id"]) in keep]
        data["total"] = sum(s["duration"] for s in data["shots"])
    project = get_project(pid)
    total = data["total"]
    # 把预演帧内联成 data-uri，导出的 HTML 才能脱离服务端单独播放
    shots = []
    for s in data["shots"]:
        svg = ""
        try:
            row = db().execute("SELECT * FROM shots WHERE id=?", (s["id"],)).fetchone()
            svg = frames.render(dict(row), project, parse_json(row["blocking_json"], {}))
        except Exception:
            svg = ""
        item = {**{k: v for k, v in s.items() if k != "frame"}, "frame": inline_svg(svg)}
        # 真视频也内联成 data-uri：导出的 HTML 拷到别的电脑照样自动播放
        vid = s.get("video") or ""
        if vid.lower().endswith((".mp4", ".webm")):
            vp = os.path.join(DATA_DIR, vid.lstrip("/").replace("/", os.sep))
            if os.path.isfile(vp) and os.path.getsize(vp) < 64 * 1024 * 1024:
                with open(vp, "rb") as f:
                    item["video_data"] = ("data:video/mp4;base64," if vid.lower().endswith(".mp4")
                                          else "data:video/webm;base64,") + base64.b64encode(f.read()).decode()
        shots.append(item)
    payload = {"ar": 9 / 16 if (project.get("ratio") or "").startswith("9:16") else 16 / 9,
               "shots": shots}
    html_text = (REEL_TEMPLATE
                 .replace("{{TITLE}}", project.get("title") or "合集")
                 .replace("{{N}}", str(len(shots)))
                 .replace("{{TOTAL}}", str(total))
                 .replace("{{DATA}}", json.dumps(payload, ensure_ascii=False)))
    safe_title = re.sub(r'[^\w\u4e00-\u9fa5]', '', project.get('title') or 'project')
    name = f"合集_{safe_title}_{datetime.now().strftime('%H%M%S')}.html"
    path = os.path.join(EXPORT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return name, path


def inline_svg(svg: str) -> str:
    if not svg:
        return ""
    import base64
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()


def concat_with_ffmpeg(pid, eid=None, shot_ids=None, pro=True):
    ff = get_setting("ffmpeg_path")
    if not ff or not os.path.exists(ff):
        raise RuntimeError("未配置 ffmpeg，已改为导出可独立播放的 HTML 合集")
    shots = [s for s in reel_payload(pid, eid)["shots"] if s.get("video")]
    if shot_ids:
        if isinstance(shot_ids, str):
            shot_ids = [x for x in shot_ids.split(",") if x.strip()]
        keep = {int(x) for x in shot_ids}
        shots = [s for s in shots if int(s["id"]) in keep]
    if not shots:
        raise RuntimeError("还没有真实视频片段（当前为预演帧）")
    project = get_project(pid)
    listfile = os.path.join(EXPORT_DIR, f"concat_{uuid.uuid4().hex[:6]}.txt")
    out = os.path.join(EXPORT_DIR, f"成片_{datetime.now().strftime('%H%M%S')}.mp4")
    if pro:
        # 统一画幅/帧率 + 轻微调色均衡 + 每镜首尾淡入淡出，再无损拼接
        portrait = (project.get("ratio") or "").startswith("9:16")
        W, H = (768, 1344) if portrait else (1344, 768)
        tmpdir = os.path.join(EXPORT_DIR, "tmp_" + uuid.uuid4().hex[:6])
        os.makedirs(tmpdir, exist_ok=True)
        parts = []
        for i, s in enumerate(shots):
            src = os.path.join(DATA_DIR, s["video"].lstrip("/").replace("/", os.sep))
            dur = float(s.get("duration") or 0) or 5.0
            fo = max(0.0, dur - 0.3)
            dst = os.path.join(tmpdir, f"{i:03d}.mp4")
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,"
                  f"eq=contrast=1.06:saturation=1.05:brightness=0.02,"
                  f"fade=t=in:st=0:d=0.25,fade=t=out:st={fo:.2f}:d=0.25,format=yuv420p")
            subprocess.run([ff, "-y", "-i", src, "-vf", vf, "-c:v", "libx264",
                            "-crf", "20", "-preset", "veryfast",
                            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", dst], check=True, timeout=600)
            parts.append(dst)
        with open(listfile, "w", encoding="utf-8") as f:
            for p in parts:
                f.write(f"file '{p}'\n")
        subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                        "-c", "copy", out], check=True, timeout=900)
        try:
            for p in parts:
                os.remove(p)
            os.rmdir(tmpdir)
        except Exception:
            pass
        return os.path.basename(out)
    with open(listfile, "w", encoding="utf-8") as f:
        for s in shots:
            f.write(f"file '{os.path.join(DATA_DIR, s['video'].lstrip('/').replace('/', os.sep))}'\n")
    subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", out],
                   check=True, timeout=900)
    return os.path.basename(out)


def do_assemble(jid, pid, eid):
    """本集合集：按分镜顺序拼 MP4，并把结果写回 episodes.export_url。"""
    ep = get_episode(eid)
    shots = [s for s in list_shots(eid) if (s.get("video_url") or "").lower().endswith((".mp4", ".webm"))]
    if not shots:
        raise RuntimeError("本集还没有已生成的镜头")
    job_log(jid, f"开始拼接：{len(shots)} 镜（按分镜顺序）", 15)
    name = concat_with_ffmpeg(pid, eid, [s["id"] for s in shots])
    url = "/exports/" + urllib.parse.quote(name)
    with _db_lock:
        db().execute("UPDATE episodes SET export_url=?, updated_at=? WHERE id=?", (url, now(), eid))
        db().commit()
    job_log(jid, f"拼接完成 → {url}", 100)
    log_activity(pid, "export", f"第{ep['idx']}集合集完成")
    return url


# ---------------------------------------------------------------- PRD 四：资产改名 / 换图联动同步

def asset_ref_map(pid, name, kind):
    """列出引用该资产的镜头（集号 / 镜号 / 引用字段），删除前提示用。"""
    refs = []
    for ep in db().execute("SELECT id,idx FROM episodes WHERE project_id=? ORDER BY idx", (pid,)):
        for s in list_shots(ep["id"]):
            fields = []
            if kind == "char" and name in parse_json(s.get("chars"), []):
                fields.append("出场角色")
            if kind == "prop" and name in parse_json(s.get("props"), []):
                fields.append("道具")
            if kind == "scene" and name and name in (s.get("scene_slug") or ""):
                fields.append("主场景")
            for d in parse_json(s.get("dialogue_json"), []):
                if (d.get("sp") or "") == name:
                    fields.append("说话人")
                    break
            if fields:
                refs.append({"shot_id": s["id"], "idx": s["idx"], "episode_idx": ep["idx"],
                             "fields": sorted(set(fields))})
    return refs


def sync_asset_rename(pid, old, new, kind):
    """改名后全局同步：分镜角色/道具/场景、说话人、剧本 cast 与 dialogue.sp，不留旧名。"""
    if not old or not new or old == new:
        return 0
    touched = 0
    with _db_lock:
        for ep in db().execute("SELECT id FROM episodes WHERE project_id=? ORDER BY idx", (pid,)):
            eid = ep["id"]
            for s in list_shots(eid):
                sets = []
                if kind == "char":
                    cur = parse_json(s.get("chars"), [])
                    if old in cur:
                        sets.append(("chars", json.dumps([new if c == old else c for c in cur], ensure_ascii=False)))
                if kind == "prop":
                    cur = parse_json(s.get("props"), [])
                    if old in cur:
                        sets.append(("props", json.dumps([new if p == old else p for p in cur], ensure_ascii=False)))
                if kind == "scene":
                    slug = s.get("scene_slug") or ""
                    if old in slug:
                        sets.append(("scene_slug", slug.replace(old, new)))
                dlg = parse_json(s.get("dialogue_json"), [])
                hit = False
                for d in dlg:
                    if (d.get("sp") or "") == old:
                        d["sp"] = new
                        hit = True
                if hit:
                    sets.append(("dialogue_json", json.dumps(dlg, ensure_ascii=False)))
                if sets:
                    cols = ", ".join("%s=?" % c for c, _ in sets)
                    db().execute("UPDATE shots SET %s, updated_at=? WHERE id=?" % cols,
                                 (*[v for _, v in sets], now(), s["id"]))
                    touched += 1
            ep_row = get_episode(eid)
            script = parse_json(ep_row.get("script_json"), {})
            changed = False
            for scene in script.get("scenes", []) or []:
                cast = scene.get("cast") or []
                if old in cast:
                    scene["cast"] = [new if c == old else c for c in cast]
                    changed = True
                for d in scene.get("dialogue", []) or []:
                    if (d.get("sp") or "") == old:
                        d["sp"] = new
                        changed = True
            if changed:
                db().execute("UPDATE episodes SET script_json=?, updated_at=? WHERE id=?",
                             (json.dumps(script, ensure_ascii=False), now(), eid))
        db().commit()
    return touched


# ---------------------------------------------------------------- PRD 九：音乐创作（固定本地 ComfyUI）

def music_row(mid):
    return rowdict(db().execute("SELECT * FROM music WHERE id=?", (mid,)).fetchone())


def do_music(jid, mid):
    m = music_row(mid)
    if not m:
        raise RuntimeError("音乐条目不存在")
    project = get_project(m["project_id"])
    wf_file = (get_setting("music_workflow") or "").strip()
    if not wf_file:
        with _db_lock:
            db().execute("UPDATE music SET status='unconfigured', updated_at=? WHERE id=?", (now(), mid))
            db().commit()
        job_log(jid, "音乐生成接口未配置：系统设置 → 音乐生成接口 里填本地 ComfyUI 音乐工作流文件名", 10)
        raise RuntimeError("音乐生成接口未配置（PRD 九：只允许本地 ComfyUI，不开放线上服务商）")
    path = os.path.join(WORKFLOW_DIR, wf_file)
    if not os.path.exists(path):
        with _db_lock:
            db().execute("UPDATE music SET status='unconfigured', updated_at=? WHERE id=?", (now(), mid))
            db().commit()
        raise RuntimeError("找不到音乐工作流文件 workflows/%s" % wf_file)
    wf = load_workflow(wf_file)
    job_log(jid, "载入本地 ComfyUI 音乐工作流 %s" % wf_file, 20)
    # 只改提示词 / 时长 / 节拍这几个输入，不动节点结构
    for nid, node in (wf or {}).items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "").lower()
        inp = node.get("inputs") or {}
        if "text" in inp and isinstance(inp["text"], str) and any(k in ct for k in ("clip", "text", "prompt")):
            node["inputs"]["text"] = m.get("prompt") or m.get("name") or "剧集配乐"
        for k in ("seconds", "duration", "duration_seconds"):
            if k in inp and isinstance(inp[k], (int, float)):
                node["inputs"][k] = int(m.get("duration") or 30)
        for k in ("bpm", "tempo"):
            if k in inp and isinstance(inp[k], (int, float)):
                node["inputs"][k] = int(m.get("bpm") or 90)
    cs = comfy_status(force=True)
    if not cs["ok"]:
        cs = ensure_comfy()
    if not cs["ok"]:
        raise RuntimeError("ComfyUI 离线：" + str(cs.get("msg") or ""))
    job_log(jid, "提交音乐生成任务", 40)
    pid_comfy = comfy_submit(wf, str(uuid.uuid4()))
    fileinfo = comfy_wait(pid_comfy, timeout=1800)
    url = comfy_download(fileinfo)
    with _db_lock:
        db().execute("UPDATE music SET url=?, status='done', updated_at=? WHERE id=?", (url, now(), mid))
        db().commit()
    log_activity(m["project_id"], "music", "生成音乐 %s → %s" % (m.get("name"), url))
    job_log(jid, "音乐生成完成 → %s" % url, 100)


# ---------------------------------------------------------------- PRD 八：角色音色 / 合音

def voice_row(vid):
    return rowdict(db().execute("SELECT * FROM voices WHERE id=?", (vid,)).fetchone())


def voice_of_asset(project_id, asset_id):
    for v in db().execute("SELECT * FROM voices WHERE project_id=? AND asset_id=? ORDER BY id", (project_id, asset_id)):
        return dict(v)
    return None


# ---------------------------------------------------------------- PRD 十：系统设置

SETTING_KEYS = ("llm_base_url", "llm_api_key", "llm_model", "engine_mode", "comfy_base",
                "comfy_input_dir", "comfy_output_dir", "comfy_python", "comfy_yaml",
                "video_workflow", "builtin_video", "video_steps", "video_scale", "ffmpeg_path",
                "video_api_mode", "video_api_base", "video_api_key",
                "image_api_mode", "image_api_base", "image_api_key",
                "music_api_mode", "music_workflow", "music_api_base",
                "default_text_model", "default_image_wf", "image_concurrency", "video_concurrency",
                "tts_base", "tts_key",
                "llm_provider", "llm_temperature", "llm_max_tokens",
                "default_novel_model", "default_script_model", "default_board_model",
                "default_video_service", "default_image_service", "default_music_service")


def settings_payload():
    out = {}
    for k in SETTING_KEYS:
        out[k] = get_setting(k)
    out["llm_key_set"] = bool(out.get("llm_api_key"))
    out["music_configured"] = bool((out.get("music_workflow") or "").strip())
    out["tts_configured"] = bool((out.get("tts_base") or "").strip())
    out["version"] = APP_LABEL
    out["version_code"] = APP_VERSION
    out["db"] = DB_PATH
    out["port"] = PORT
    return out


def save_settings(body):
    saved = []
    for k in SETTING_KEYS:
        if k in body:
            set_setting(k, str(body.get(k) or "").strip())
            saved.append(k)
    return saved


# ---------------------------------------------------------------- PRD 十：接口连通性测试

def test_endpoint(kind, ov=None):
    ov = ov or {}
    import socket as _socket
    kind = {"text": "llm", "model": "llm", "comfyui": "comfy"}.get(kind, kind)
    if kind == "tts":
        base = (get_setting("tts_base") or "").strip()
        if not base:
            return False, "未接入 TTS：填地址后才能生成合音（不会用占位音频冒充）"
        host = base.split("//")[-1].split("/")[0]
        try:
            h, _, port = host.partition(":")
            s = _socket.create_connection((h, int(port or 443)), timeout=4)
            s.close()
            return True, "端口连通"
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, str(e)[:80])
    if kind == "llm":
        base = (ov.get("base") or get_setting("llm_base_url") or "").strip()
        key = (ov.get("key") or get_setting("llm_api_key") or "").strip()
        provider = ov.get("provider") or get_setting("llm_provider") or ""
        local = provider in ("ollama", "vllm") or "127.0.0.1" in base or "localhost" in base
        if not base:
            return False, "未填写 API 地址"
        if not key and not local:
            return False, "未配置 API Key（已启用本地引擎）"
        ok, msg, models = fetch_model_list(base, key, provider)
        if ok:
            return True, msg
        return False, msg
    if kind in ("comfy", "video", "image", "music"):
        mode = ov.get("mode") or get_setting(
            {"video": "video_api_mode", "image": "image_api_mode"}.get(kind, "music_api_mode")) or "local"
        if ov.get("base"):
            base = ov.get("base")
        elif kind in ("video", "image") and mode != "local":
            base = get_setting(kind + "_api_base") or ""
        elif kind == "music" and get_setting("music_api_base"):
            base = get_setting("music_api_base")
        else:
            base = get_setting("comfy_base") or ""
        if not base:
            return False, "未填写接口地址"
        host = base.split("//")[-1].split("/")[0]
        try:
            if host.startswith("http"):
                urllib.request.urlopen(base.rstrip("/") + "/system_stats", timeout=6).read()
                return True, "连通"
            h, _, port = host.partition(":")
            s = _socket.create_connection((h, int(port or 80)), timeout=4)
            s.close()
            return True, "端口连通"
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, str(e)[:80])
    return False, "未知接口类型"


# ---------------------------------------------------------------- 项目删除（连带清理）

def delete_project(pid):
    """PRD 十三：删项目 = 剧本/分镜/素材/成片/音乐一并清除。"""
    files = []
    for s in db().execute("SELECT video_url, thumb_url FROM shots WHERE project_id=?", (pid,)):
        files.extend([s["video_url"], s["thumb_url"]])
    for a in db().execute("SELECT id FROM assets WHERE project_id=?", (pid,)):
        for im in db().execute("SELECT url FROM asset_images WHERE asset_id=?", (a["id"],)):
            files.append(im["url"])
    for m in db().execute("SELECT url FROM music WHERE project_id=?", (pid,)):
        files.append(m["url"])
    removed = 0
    for u in files:
        if not u or "/media/" not in str(u):
            continue
        fp = os.path.join(MEDIA_DIR, str(u).split("/media/")[-1])
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                removed += 1
        except Exception:
            pass
    with _db_lock:
        db().execute("DELETE FROM projects WHERE id=?", (pid,))
        db().execute("DELETE FROM episodes WHERE project_id=?", (pid,))
        db().execute("DELETE FROM assets WHERE project_id=?", (pid,))
        db().execute("DELETE FROM asset_images WHERE project_id=?", (pid,))
        db().execute("DELETE FROM shots WHERE project_id=?", (pid,))
        db().execute("DELETE FROM jobs WHERE project_id=?", (pid,))
        db().execute("DELETE FROM music WHERE project_id=?", (pid,))
        db().execute("DELETE FROM voices WHERE project_id=?", (pid,))
        db().execute("DELETE FROM shot_chat WHERE project_id=?", (pid,))
        db().commit()
    return removed


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Studio/2.0"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _ok(self, payload):
        return self._send(200, payload)

    def _err(self, msg, code=400):
        return self._send(code, {"error": str(msg)})

    # ---- routing
    def do_OPTIONS(self):
        self._send(204, "", extra={"Access-Control-Allow-Origin": "*",
                                   "Access-Control-Allow-Headers": "*",
                                   "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS"})

    def do_GET(self):
        try:
            self.route_get(urllib.parse.urlparse(self.path))
        except Exception as e:
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        try:
            self.route_post(urllib.parse.urlparse(self.path))
        except Exception as e:
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_PATCH(self):
        self.do_POST()

    def do_DELETE(self):
        try:
            u = urllib.parse.urlparse(self.path)
            m = re.match(r"/api/projects/(\d+)", u.path)
            if m:
                pid = int(m.group(1))
                removed = delete_project(pid)
                qs2 = urllib.parse.parse_qs(u.query)
                left = [p["id"] for p in db().execute("SELECT id FROM projects ORDER BY id DESC")]
                nxt = int(qs2.get("next", [0])[0] or 0) or (left[0] if left else 0)
                return self._ok({"ok": True, "removed_files": removed, "next": nxt,
                                 "state": compute_state(nxt or None)})
            m = re.match(r"/api/shots/(\d+)$", u.path)
            if m:
                sid = int(m.group(1))
                row = db().execute("SELECT episode_id FROM shots WHERE id=?", (sid,)).fetchone()
                with _db_lock:
                    db().execute("DELETE FROM shots WHERE id=?", (sid,))
                    db().commit()
                if row:
                    rows = db().execute("SELECT id FROM shots WHERE episode_id=? ORDER BY idx", (row["episode_id"],)).fetchall()
                    for i, r in enumerate(rows):
                        with _db_lock:
                            db().execute("UPDATE shots SET idx=? WHERE id=?", (i + 1, r["id"]))
                            db().commit()
                return self._ok({"ok": True})

            m = re.match(r"/api/images/(\d+)$", u.path)
            if m:
                iid = int(m.group(1))
                row = rowdict(db().execute("SELECT * FROM asset_images WHERE id=?", (iid,)).fetchone())
                if row:
                    try:
                        fn = row["url"].split("/media/")[-1]
                        fp = os.path.join(MEDIA_DIR, fn)
                        if os.path.isfile(fp):
                            os.remove(fp)
                    except Exception:
                        pass
                    with _db_lock:
                        db().execute("DELETE FROM asset_images WHERE id=?", (iid,))
                        db().commit()
                    remain = list_asset_images(row["asset_id"])
                    if remain:
                        pick = remain[0]
                        with _db_lock:
                            db().execute("UPDATE asset_images SET chosen=1 WHERE id=?", (pick["id"],))
                            db().execute("UPDATE assets SET image_url=?, updated_at=? WHERE id=?",
                                         (pick["url"], now(), row["asset_id"]))
                            db().commit()
                    else:
                        with _db_lock:
                            db().execute("UPDATE assets SET image_url='', locked=0 WHERE id=?", (row["asset_id"],))
                            db().commit()
                return self._ok({"ok": True})
            m = re.match(r"/api/assets/(\d+)$", u.path)
            if m:
                aid = int(m.group(1))
                a = rowdict(db().execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())
                refs = asset_ref_map(a["project_id"], a["name"], a["kind"]) if a else []
                qs3 = urllib.parse.parse_qs(u.query)
                force = qs3.get("force", ["0"])[0] == "1"
                if refs and not force:
                    return self._send(409, {"error": "该资产仍被 %d 个镜头引用，先解绑或确认替换" % len(refs),
                                            "refs": refs})
                with _db_lock:
                    db().execute("DELETE FROM assets WHERE id=?", (aid,))
                    db().execute("DELETE FROM asset_images WHERE asset_id=?", (aid,))
                    db().commit()
                return self._ok({"ok": True, "unbound": len(refs)})
            m = re.match(r"/api/music/(\d+)$", u.path)
            if m:
                mid = int(m.group(1))
                mrow = music_row(mid)
                if mrow and mrow.get("url") and "/media/" in mrow["url"]:
                    try:
                        fp = os.path.join(MEDIA_DIR, mrow["url"].split("/media/")[-1])
                        if os.path.isfile(fp):
                            os.remove(fp)
                    except Exception:
                        pass
                with _db_lock:
                    db().execute("DELETE FROM music WHERE id=?", (mid,))
                    db().commit()
                return self._ok({"ok": True, "state": compute_state((mrow or {}).get("project_id"))})
            m = re.match(r"/api/shots/(\d+)", u.path)
            if m:
                with _db_lock:
                    db().execute("DELETE FROM shots WHERE id=?", (int(m.group(1)),))
                    db().commit()
                return self._ok({"ok": True})
            return self._err("未知接口", 404)
        except Exception as e:
            self._err(str(e), 500)

    def route_get(self, u):
        p = u.path
        qs = urllib.parse.parse_qs(u.query)
        pid = int(qs.get("project", [0])[0] or 0) or None

        if p in ("/", "/index.html"):
            return self.serve_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            return self.serve_file(os.path.join(STATIC_DIR, p[len("/static/"):]))
        if p.startswith("/media/"):
            return self.serve_file(os.path.join(MEDIA_DIR, p[len("/media/"):]), stream=True)
        if p.startswith("/exports/"):
            return self.serve_file(os.path.join(EXPORT_DIR, urllib.parse.unquote(p[len("/exports/"):])), stream=True)
        if p == "/api/state":
            return self._ok(compute_state(pid))
        if p == "/api/config":
            return self._ok({"config": public_config(), "server": server_info()})
        if p == "/api/settings":
            return self._ok({"settings": settings_payload(), "server": server_info()})
        if p == "/api/update_check":
            return self._ok(check_update())
        if p == "/api/wf":
            return self._ok({"wf": [dict(r) for r in db().execute("SELECT * FROM wf_presets ORDER BY sort")]})
        if p == "/api/music":
            q = int(qs.get("project", [0])[0] or 0)
            rows = [dict(r) for r in db().execute(
                "SELECT * FROM music WHERE project_id=? ORDER BY id DESC", (q,))] if q else []
            return self._ok({"music": rows})
        if p == "/api/voices":
            q = int(qs.get("project", [0])[0] or 0)
            rows = [dict(r) for r in db().execute(
                "SELECT * FROM voices WHERE project_id=? ORDER BY id", (q,))] if q else []
            return self._ok({"voices": rows})
        if p == "/api/styles":
            groups = {}
            for s in STYLE_CATALOG:
                groups.setdefault(s["g"], []).append(s)
            return self._ok({"groups": [{"name": k, "styles": v} for k, v in groups.items()]})
        if p == "/api/health":
            return self._ok({"ok": True, "time": now()})
        if p == "/api/reel":
            eid = int(qs.get("episode", [0])[0] or 0) or None
            if not pid:
                return self._err("缺 project")
            return self._ok(reel_payload(pid, eid))
        if p.startswith("/api/reel/export"):
            if not pid:
                return self._err("缺 project")
            eid = int(qs.get("episode", [0])[0] or 0) or None
            name, path = export_reel(pid, eid, qs.get("shots", [None])[0])
            return self._ok({"file": name, "url": "/exports/" + urllib.parse.quote(name)})
        m = re.match(r"/api/shots/(\d+)/frame\.svg", p)
        if m:
            sid = int(m.group(1))
            row = db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err("镜头不存在", 404)
            shot = dict(row)
            project = get_project(shot["project_id"])
            blocking = parse_json(shot.get("blocking_json"), {})
            svg = frames.render(shot, project, blocking)
            return self._send(200, svg, "image/svg+xml; charset=utf-8")
        m = re.match(r"/api/shots/(\d+)/chat$", p)
        if m:
            sid = int(m.group(1))
            rows = db().execute("SELECT role,text,created_at FROM shot_chat WHERE shot_id=? ORDER BY id", (sid,))
            return self._ok({"messages": [dict(r) for r in rows]})

        # 小说下载（单集 / 全书）
        if p == "/api/export/novel":
            project = get_project(pid) if pid else None
            eps = [dict(r) for r in db().execute(
                "SELECT * FROM episodes WHERE project_id=? ORDER BY idx", (pid or 0,))]
            if not eps:
                return self._err("还没有内容可下载", 404)
            want = int(qs.get("episode", [0])[0] or 0)
            if want:
                eps = [e for e in eps if e["id"] == want] or eps[:1]
            parts = []
            for e in eps:
                title = e.get("title") or ("第%d集" % e["idx"])
                parts.append("# %s\n\n%s\n" % (title, (e.get("novel_text") or "（本集还没写正文）")))
            full = ((project or {}).get("title") or "小说") + "\n\n" + "\n\n".join(parts)
            name = ((project or {}).get("title") or "小说") + ("_第%d集" % eps[0]["idx"] if want else "_全文") + ".txt"
            data = full.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        m = re.match(r"/api/shots/(\d+)/packet$", p)
        if m:
            shot = rowdict(db().execute("SELECT * FROM shots WHERE id=?", (int(m.group(1)),)).fetchone())
            if not shot:
                return self._err("镜头不存在", 404)
            project = get_project(shot["project_id"])
            assets = list_assets(shot["project_id"])
            return self._ok({"packet": get_packet(project, shot, assets),
                             "errors": packet_errors(project, shot, assets),
                             "duration": int(shot.get("duration") or 5)})

        m = re.match(r"/api/shots/(\d+)$", p)
        return self._err("未知接口 " + p, 404)

    def route_post(self, u):
        p = u.path
        body = self._json_body()

        # 配置
        if p == "/api/config":
            for k in ("llm_base_url", "llm_api_key", "llm_model", "engine_mode",
                      "comfy_base", "comfy_input_dir", "builtin_video", "video_steps", "video_scale",
                      "video_workflow", "ffmpeg_path"):
                if k in body and body[k] != "":
                    set_setting(k, body[k])
            _llm_state["ts"] = 0
            comfy_status(force=True)
            return self._ok({"config": public_config(), "server": server_info()})

        # 项目
        if p == "/api/repair/dialogue":
            pid = int(body.get("project_id") or 1)
            eid = int(body.get("episode_id") or 0)
            if not eid:
                ep = db().execute("SELECT id FROM episodes WHERE project_id=? ORDER BY idx", (pid,)).fetchone()
                eid = ep["id"] if ep else 0
            if not eid:
                return self._err("没有分集")
            fixed, kept = repair_dialogue_prompts(pid, eid)
            return self._ok({"fixed": fixed, "already": kept, "state": compute_state(pid)})

        if p == "/api/repair/continuity":
            # 全镜开启「参考上一镜末帧 + 衔接段」：只对第 2 镜以后生效
            pid = int(body.get("project_id") or 1)
            eid = int(body.get("episode_id") or 0)
            val = 1 if body.get("value", 1) else 0
            shots = [s for s in list_shots(eid) if int(s["idx"]) > 1]
            with _db_lock:
                for s in shots:
                    db().execute("UPDATE shots SET ref_prev=?, updated_at=? WHERE id=?", (val, now(), s["id"]))
                db().commit()
            return self._ok({"changed": len(shots), "value": val, "state": compute_state(pid)})

        if p == "/api/projects":
            pid = body.get("id")
            fields = {k: body.get(k) for k in
                      ("title", "premise", "genre", "platform", "audience", "ratio",
                       "episode_count", "episode_length", "style_lock", "forbidden",
                       "dialogue_lang", "prompt_mode", "fps", "quality", "duration_range",
                       "avoid_tpl", "video_wf", "viseme_pack")}
            if pid:
                sets = ", ".join(f"{k}=?" for k in fields if fields[k] is not None)
                with _db_lock:
                    db().execute(f"UPDATE projects SET {sets}, updated_at=? WHERE id=?",
                                 (*[fields[k] for k in fields if fields[k] is not None], now(), pid))
                    db().commit()
                if fields.get("episode_count") is not None:
                    n = int(fields["episode_count"] or 12)
                    sync_episodes(int(pid))
                    with _db_lock:
                        db().execute("UPDATE episodes SET archived=0 WHERE project_id=? AND idx<=?", (int(pid), n))
                        db().execute("UPDATE episodes SET archived=1 WHERE project_id=? AND idx>?", (int(pid), n))
                        db().commit()
                return self._ok(compute_state(pid))
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO projects(title,premise,genre,platform,audience,ratio,episode_count,"
                    "episode_length,style_lock,forbidden,fps,quality,duration_range,avoid_tpl,video_wf,"
                    "dialogue_lang,viseme_pack,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (body.get("title") or "未命名项目", body.get("premise") or "",
                     body.get("genre") or "都市情感", body.get("platform") or "抖音",
                     body.get("audience") or "25-45 岁女性", body.get("ratio") or "9:16",
                     int(body.get("episode_count") or 12),
                     body.get("episode_length") or "75–95s",
                     body.get("style_lock") or "仿真人写实：真实皮肤纹理与毛孔，35mm 胶片色彩，自然光比",
                     body.get("forbidden") or "不写血腥、不写未成年恋爱、不写真实品牌",
                     int(body.get("fps") or 30), body.get("quality") or "电影级",
                     body.get("duration_range") or "75–95s", body.get("avoid_tpl") or "标准版",
                     body.get("video_wf") or "director", body.get("dialogue_lang") or "zh",
                     body.get("viseme_pack") or "普通话",
                     now(), now()))
                db().commit()
                pid = cur.lastrowid
                db().execute("INSERT INTO episodes(project_id,idx,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                             (pid, 1, "第1集", now(), now()))
                db().commit()
            sync_episodes(pid)
            return self._ok(compute_state(pid))

        m = re.match(r"/api/projects/(\d+)/lock$", p)
        if m:
            pid = int(m.group(1))
            project = get_project(pid)
            if not project.get("premise"):
                return self._err("请先填写创意与目标")
            # 目标锁定前的体检
            issues = []
            if int(project.get("episode_count") or 0) < 6:
                issues.append({"msg": "集数少于 6 集，短剧钩子铺不开"})
            if not project.get("style_lock"):
                issues.append({"msg": "没有写风格锁，画面会不一致"})
            if not project.get("forbidden"):
                issues.append({"msg": "没有写禁区，模型可能越界"})
            note = "目标已锁定：" + "；".join(i["msg"] for i in issues) if issues else "目标已锁定全部字段"
            with _db_lock:
                db().execute("UPDATE projects SET target_locked=?, target_note=?, updated_at=? WHERE id=?",
                             (1, note, now(), pid))
                db().commit()
            log_activity(pid, "lock", "锁定创作目标")
            return self._ok({"ok": True, "note": note, "state": compute_state(pid)})

        m = re.match(r"/api/projects/(\d+)/lock-target", p)
        if m:
            pid = int(m.group(1))
            with _db_lock:
                db().execute("UPDATE projects SET target_locked=0, updated_at=? WHERE id=?", (now(), pid))
                db().commit()
            return self._ok({"ok": True})

        # 资产
        if p == "/api/assets":
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO assets(project_id,kind,name,canon,image_url,locked,sort,gen_workflow,view_mode,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (body.get("project_id"), body.get("kind") or "char", body.get("name") or "新资产",
                     body.get("canon") or "", body.get("image_url") or "",
                     1 if body.get("locked") else 0, 99,
                     body.get("gen_workflow") or get_setting("default_image_wf") or "qwen_image2512",
                     body.get("view_mode") or "four", now(), now()))
                db().commit()
            return self._ok({"ok": True, "id": cur.lastrowid})

        m = re.match(r"/api/assets/(\d+)$", p)
        if m:
            aid = int(m.group(1))
            old = rowdict(db().execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())
            sets = []
            vals = []
            for k in ("name", "canon", "locked", "image_url", "prompt_maker", "gen_workflow", "view_mode", "voice_id"):
                if k in body:
                    sets.append(f"{k}=?")
                    vals.append(body[k])
            if sets:
                with _db_lock:
                    db().execute(f"UPDATE assets SET {', '.join(sets)}, updated_at=? WHERE id=?",
                                 (*vals, now(), aid))
                    db().commit()
            # PRD 四·改名联动：改名即全局同步分镜 / 剧本 / 说话人，不让旧名残留
            renamed = 0
            if old and body.get("name") and str(body["name"]).strip() != (old.get("name") or ""):
                renamed = sync_asset_rename(old["project_id"], old["name"], str(body["name"]).strip(), old["kind"])
                log_activity(old["project_id"], "asset", "资产改名 %s → %s（同步 %d 镜）"
                             % (old["name"], body["name"], renamed))
            return self._ok({"ok": True, "renamed_shots": renamed})

        # PRD 四：删除前先看被谁引用
        m = re.match(r"/api/assets/(\d+)/refs$", p)
        if m:
            a = rowdict(db().execute("SELECT * FROM assets WHERE id=?", (int(m.group(1)),)).fetchone())
            if not a:
                return self._err("资产不存在", 404)
            return self._ok({"asset": a["name"], "kind": a["kind"],
                             "refs": asset_ref_map(a["project_id"], a["name"], a["kind"])})

        # PRD 四：批量生成图片提示词
        if p == "/api/assets/batch_prompt":
            pid = int(body.get("project_id") or 0)
            project = get_project(pid)
            if not project:
                return self._err("项目不存在")
            n = 0
            for a in list_assets(pid):
                mode = "four" if a["kind"] == "char" else "single"
                pos, _neg = image_prompt(a, mode, project)
                with _db_lock:
                    db().execute("UPDATE assets SET prompt_maker=?, updated_at=? WHERE id=?", (pos, now(), a["id"]))
                    db().commit()
                n += 1
            log_activity(pid, "asset", "批量生成图片提示词 %d 项" % n)
            return self._ok({"ok": True, "count": n, "state": compute_state(pid)})

        # 剧集（大纲 / 正文 / 剧本 的编辑同步）
        # 向导专用：素材校验 / 时长重算 / SRT
        m = re.match(r"/api/episodes/(\d+)/validate$", p)
        if m:
            eid = int(m.group(1))
            ep = get_episode(eid)
            if not ep:
                return self._err("集不存在", 404)
            miss = missing_assets(ep["project_id"], eid)
            shots = list_shots(eid)
            assets = list_assets(ep["project_id"])
            errs = []
            for s in shots:
                errs.extend(packet_errors(get_project(ep["project_id"]), s, assets))
            return self._ok({"ok": (not miss and not errs), "missing": miss,
                             "packet_errors": errs, "errors": len(errs), "shots": len(shots),
                             "status": episode_status(ep, shots)})

        m = re.match(r"/api/episodes/(\d+)/normalize$", p)
        if m:
            eid = int(m.group(1))
            ep = get_episode(eid)
            fixed, warn = [], []
            for s in list_shots(eid):
                dlg = parse_json(s.get("dialogue_json"), [])
                need = speech_seconds([d.get("line") or "" for d in dlg])
                dur = int(s.get("duration") or 5)
                if need > 15:
                    warn.append({"idx": s["idx"], "need": need, "msg": "台词朗读约 %.0f 秒，超过 15 秒，请拆镜" % need})
                    newdur = 15
                else:
                    newdur = max(1, min(15, int(max(need + 0.4, dur))))
                    if newdur < need:
                        newdur = min(15, int(need) + 1)
                if abs(newdur - dur) >= 1 or need > dur:
                    fixed.append({"idx": s["idx"], "from": dur, "to": newdur})
                with _db_lock:
                    db().execute("UPDATE shots SET duration=? WHERE id=?", (newdur, s["id"]))
                    db().commit()
            log_activity(ep["project_id"], "board", f"时长按台词重算 {len(fixed)} 镜")
            return self._ok({"fixed": fixed, "warn": warn, "state": compute_state(ep["project_id"])})

        m = re.match(r"/api/episodes/(\d+)/srt$", p)
        if m:
            eid = int(m.group(1))
            ep = get_episode(eid)
            shots = list_shots(eid)
            srt = str(body.get("srt_text") or "") if body.get("srt_text") is not None else build_srt(ep, shots)
            if not srt.strip():
                srt = build_srt(ep, shots)
            with _db_lock:
                db().execute("UPDATE episodes SET srt_text=?, updated_at=? WHERE id=?", (srt, now(), eid))
                db().commit()
            if body.get("download"):
                self._send(200, srt, "application/x-subrip; charset=utf-8",
                           {"Content-Disposition": "attachment; filename=EP%02d.srt" % int(ep["idx"])})
                return
            return self._ok({"srt": srt, "lines": srt.count("-->")})

        if p == "/api/episodes/assemble":
            eid = int(body.get("episode_id") or 0)
            ep = get_episode(eid)
            if not ep:
                return self._err("集不存在")
            jid = new_job(ep["project_id"], eid, "video", "reel")
            run_async(do_assemble, jid, ep["project_id"], eid)
            return self._ok({"job_id": jid})

        m = re.match(r"/api/episodes/(\d+)$", p)
        if m:
            eid = int(m.group(1))
            ep = get_episode(eid)
            if not ep:
                return self._err("集不存在", 404)
            pid = ep["project_id"]
            if "outline" in body:
                write_outline(pid, eid, body["outline"])
            if "title" in body:
                with _db_lock:
                    db().execute("UPDATE episodes SET title=?, updated_at=? WHERE id=?", (body["title"], now(), eid))
                    db().commit()
            if "novel" in body:
                ep = get_episode(eid)
                with _db_lock:
                    db().execute("UPDATE episodes SET novel_text=?, novel_sig=?, novel_manual=1, updated_at=? WHERE id=?",
                                 (body["novel"], sha(ep.get("outline_json")), now(), eid))
                    db().commit()
            if "novel_manual" in body:
                with _db_lock:
                    db().execute("UPDATE episodes SET novel_manual=? WHERE id=?", (int(body["novel_manual"]), eid))
                    db().commit()
            if "script" in body:
                ep = get_episode(eid)
                with _db_lock:
                    db().execute("UPDATE episodes SET script_json=?, script_sig=?, updated_at=? WHERE id=?",
                                 (json.dumps(body["script"], ensure_ascii=False), sha(ep.get("novel_text")), now(), eid))
                    db().commit()
            log_activity(pid, "edit", "手动修改了创作内容（下游已标记需重跑）")
            return self._ok(compute_state(pid))

        if p == "/api/shots/reorder":
            eid = int(body.get("episode_id") or 0)
            ids = [int(x) for x in (body.get("ids") or [])]
            for i, sid in enumerate(ids):
                with _db_lock:
                    db().execute("UPDATE shots SET idx=?, updated_at=? WHERE id=? AND episode_id=?",
                                 (i + 1, now(), sid, eid))
                    db().commit()
            return self._ok({"ok": True, "state": compute_state(get_episode(eid)["project_id"])})

        if p == "/api/shots":
            pid = int(body.get("project_id") or 0)
            eid = int(body.get("episode_id") or 0)
            n = db().execute("SELECT COUNT(*) c FROM shots WHERE episode_id=?", (eid,)).fetchone()["c"]
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO shots(project_id,episode_id,idx,duration,shot_size,camera,movement,scene_slug,"
                    "chars,props,action,emotion,dialogue_json,transition,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, eid, n + 1, int(body.get("duration") or 5), body.get("shot_size") or "中景",
                     body.get("camera") or "固定", body.get("movement") or "固定",
                     body.get("scene_slug") or "", json.dumps(body.get("chars") or [], ensure_ascii=False),
                     json.dumps(body.get("props") or [], ensure_ascii=False), body.get("action") or "新镜头",
                     "", "[]", "硬切", now(), now()))
                db().commit()
            return self._ok({"ok": True, "id": cur.lastrowid})

        # 镜头 AI 对话
        m = re.match(r"/api/shots/(\d+)/chat$", p)
        if m:
            sid = int(m.group(1))
            text = (body.get("text") or "").strip()
            row = db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err("镜头不存在", 404)
            shot = dict(row)
            project = get_project(shot["project_id"])
            with _db_lock:
                db().execute("INSERT INTO shot_chat(project_id,shot_id,role,text,created_at) VALUES(?,?,?,?,?)",
                             (shot["project_id"], sid, "user", text, now()))
                db().commit()
            reply, updates = revise_shot(shot, project, text)
            # PRD 七：先给修改后的镜头文案，点【应用】才覆盖原内容；点【取消】保留原内容
            if body.get("dry"):
                with _db_lock:
                    db().execute("INSERT INTO shot_chat(project_id,shot_id,role,text,created_at) VALUES(?,?,?,?,?)",
                                 (shot["project_id"], sid, "assistant", reply, now()))
                    db().commit()
                return self._ok({"reply": reply, "changed": list(updates.keys()),
                                 "proposal": updates, "shot_id": sid, "dry": True})
            apply_shot_updates(shot, updates)
            with _db_lock:
                db().execute("INSERT INTO shot_chat(project_id,shot_id,role,text,created_at) VALUES(?,?,?,?,?)",
                             (shot["project_id"], sid, "assistant", reply, now()))
                db().commit()
            log_activity(shot["project_id"], "chat", "镜头 %d 对话改稿" % shot["idx"])
            return self._ok({"reply": reply, "changed": list(updates.keys()), "state": compute_state(shot["project_id"])})

        # 按大纲补齐集分组
        if p == "/api/episodes/sync":
            pid = int(body.get("project_id") or 0)
            if not pid:
                return self._err("缺 project_id")
            n = sync_episodes(pid)
            return self._ok({"created": n, "state": compute_state(pid)})

        # 资产：生成图片提示词 / 批量出图
        m = re.match(r"/api/assets/(\d+)/make_prompt", p)
        if m:
            aid = int(m.group(1))
            asset = rowdict(db().execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())
            if not asset:
                return self._err("资产不存在")
            project = get_project(asset["project_id"])
            pos, _neg = image_prompt(asset, "four" if asset["kind"] == "char" else "single", project)
            with _db_lock:
                db().execute("UPDATE assets SET prompt_maker=?, updated_at=? WHERE id=?", (pos, now(), aid))
                db().commit()
            return self._ok({"prompt": pos, "name": asset["name"]})

        if p == "/api/assets/batch_image":
            pid = int(body.get("project_id") or 0)
            kind = (body.get("kind") or "").strip()
            only_missing = bool(body.get("only_missing"))
            assets = [a for a in list_assets(pid) if (not kind or a.get("kind") == kind)]
            if only_missing:
                assets = [a for a in assets if not ((a.get("chosen_url") or a.get("image_url") or "").strip())]
            fallback_all = False
            if not assets and only_missing:
                assets = [a for a in list_assets(pid) if (not kind or a.get("kind") == kind)]
                fallback_all = True
            if not assets:
                return self._err("没有可出图的资产")
            queued = []
            for a in assets:
                mode = "four" if a.get("kind") == "char" else "single"
                jid = new_job(pid, 0, "image", "asset")
                job_update(jid, message=f"批量出图：{a.get('name')}")
                run_async(do_image, jid, pid, a["id"], mode, a.get("prompt_maker") or "", 0)
                queued.append({"id": a["id"], "name": a.get("name"), "job": jid})
            return self._ok({"queued": queued, "fallback_all": fallback_all})

        # 资产出图 / 选图
        m = re.match(r"/api/assets/(\d+)/image", p)
        if m:
            aid = int(m.group(1))
            asset = rowdict(db().execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())
            if not asset:
                return self._err("资产不存在")
            mode = body.get("mode") or ("four" if asset["kind"] == "char" else "single")
            if mode == "reroll":
                # 重新生成沿用上一次的画幅类型，避免四视图被降成单图
                prev = db().execute("SELECT mode FROM asset_images WHERE asset_id=? ORDER BY id DESC LIMIT 1",
                                    (aid,)).fetchone()
                mode = (prev["mode"] if prev and prev["mode"] in ("four", "single")
                        else ("four" if asset["kind"] == "char" else "single"))
            jid = new_job(asset["project_id"], 0, "image", "asset")
            jid_note = f"出图：{asset['name']}"
            job_update(jid, message=jid_note)
            run_async(do_image, jid, asset["project_id"], aid, mode,
                      body.get("prompt") or "", int(body.get("seed") or 0))
            return self._ok({"job_id": jid})

        m = re.match(r"/api/projects/(\d+)/images", p)
        if m:
            pid = int(m.group(1))
            jid = new_job(pid, 0, "image", "batch")
            job_update(jid, message="批量出图")
            run_async(do_images_batch, jid, pid, body.get("kind") or "")
            return self._ok({"job_id": jid})

        m = re.match(r"/api/images/(\d+)/choose$", p)
        if m:
            iid = int(m.group(1))
            row = rowdict(db().execute("SELECT * FROM asset_images WHERE id=?", (iid,)).fetchone())
            if not row:
                return self._err("图片不存在")
            with _db_lock:
                db().execute("UPDATE asset_images SET chosen=0 WHERE asset_id=?", (row["asset_id"],))
                db().execute("UPDATE asset_images SET chosen=1 WHERE id=?", (iid,))
                db().execute("UPDATE assets SET image_url=?, locked=1, updated_at=? WHERE id=?",
                             (row["url"], now(), row["asset_id"]))
                db().commit()
            return self._ok({"ok": True})

        # 单镜头数据包：保存结构化配置 + 绑定素材
        m = re.match(r"/api/shots/(\d+)/packet$", p)
        if m:
            sid = int(m.group(1))
            shot = rowdict(db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone())
            if not shot:
                return self._err("镜头不存在", 404)
            project = get_project(shot["project_id"])
            assets = list_assets(shot["project_id"])
            base = get_packet(project, shot, assets)
            incoming = body.get("packet") or {}
            if isinstance(incoming, dict):
                for k, v in incoming.items():
                    if isinstance(v, dict) and isinstance(base.get(k), dict):
                        base[k].update(v)
                    elif k in base:
                        base[k] = v
            if "duration" in body:
                try:
                    d = int(float(body["duration"]))
                except Exception:
                    return self._err("时长必须是数字")
                if d < 1 or d > 15:
                    return self._err("单镜时长只允许 1–15 秒")
            else:
                d = int(shot.get("duration") or 5)
            if body.get("regen_prompt"):
                base.setdefault("params", {})["prompt_cn"] = packet_compact_prompt(project, base)
            sets, vals = [], []
            sets += ["duration=?", "packet_json=?", "action=?", "shot_size=?", "movement=?"]
            vals += [d, json.dumps(base, ensure_ascii=False), base.get("desc") or "",
                     base.get("shot_size") or "", base.get("movement") or ""]
            params = base.get("params") or {}
            try:
                seed = int(params.get("seed"))
            except Exception:
                seed = -1
            sets += ["seed=?", "prompt_cn=?", "prompt_neg=?"]
            vals += [seed, params.get("prompt_cn") or "", params.get("prompt_neg") or ""]
            chars = [c.get("name") for c in (base.get("chars") or []) if c.get("name")]
            props = [x.get("name") for x in (base.get("props") or []) if x.get("name")]
            sets += ["chars=?", "props=?"]
            vals += [json.dumps(chars, ensure_ascii=False), json.dumps(props, ensure_ascii=False)]
            scene_name = (base.get("scene") or {}).get("name") or ""
            old_slug = shot.get("scene_slug") or ""
            slug = (old_slug.split("·")[0] + "·" + scene_name) if ("·" in old_slug and scene_name) else scene_name
            sets.append("scene_slug=?")
            vals.append(slug)
            if isinstance(body.get("dialogue_lines"), list):
                dlg = [{"sp": (x.get("sp") or "").strip(), "line": (x.get("line") or "").strip()}
                       for x in body["dialogue_lines"] if (x.get("line") or "").strip()]
                sets.append("dialogue_json=?")
                vals.append(json.dumps(dlg, ensure_ascii=False))
            with _db_lock:
                db().execute("UPDATE shots SET %s, updated_at=? WHERE id=?" % ", ".join(sets),
                             (*vals, now(), sid))
                db().commit()
            fresh = rowdict(db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone())
            errs = packet_errors(project, fresh, assets)
            return self._ok({"ok": not errs, "errors": errs, "packet": get_packet(project, fresh, assets),
                             "state": compute_state(shot["project_id"])})

        m = re.match(r"/api/episodes/(\d+)/packet_init$", p)
        if m:
            eid = int(m.group(1))
            ep = get_episode(eid)
            if not ep:
                return self._err("剧集不存在", 404)
            project = get_project(ep["project_id"])
            assets = list_assets(ep["project_id"])
            n = 0
            with _db_lock:
                for s in list_shots(eid):
                    pk = get_packet(project, s, assets)
                    db().execute("UPDATE shots SET packet_json=?, updated_at=? WHERE id=?",
                                 (json.dumps(pk, ensure_ascii=False), now(), s["id"]))
                    n += 1
                db().commit()
            log_activity(ep["project_id"], "edit", "按资产库预绑定并确认本集 %d 镜数据包" % n)
            errs = []
            for s in list_shots(eid):
                errs.extend(packet_errors(project, s, assets))
            return self._ok({"ok": not errs, "bound": n, "errors": errs,
                             "state": compute_state(ep["project_id"])})

        m = re.match(r"/api/episodes/(\d+)/packet_check$", p)
        if m:
            eid = int(m.group(1))
            ep = get_episode(eid)
            if not ep:
                return self._err("剧集不存在", 404)
            project = get_project(ep["project_id"])
            assets = list_assets(ep["project_id"])
            errs = []
            for s in list_shots(eid):
                errs.extend(packet_errors(project, s, assets))
            by_shot = {}
            for e in errs:
                by_shot.setdefault(str(e["shot"]), []).append(e)
            return self._ok({"ok": not errs, "errors": errs, "by_shot": by_shot,
                             "state": compute_state(ep["project_id"])})

        if p == "/api/upload":
            data = (body.get("data_url") or "")
            mm = re.match(r"data:(image|video)/(\w+);base64,(.*)$", data, re.S)
            if not mm:
                return self._err("只接受图片或视频文件")
            kind, ext, b64 = mm.group(1), mm.group(2).lower(), mm.group(3)
            if ext == "jpeg":
                ext = "jpg"
            if ext not in ("png", "jpg", "webp", "gif", "mp4", "webm", "mov"):
                return self._err("不支持的格式：%s" % ext)
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return self._err("文件解码失败")
            if len(raw) > 60 * 1024 * 1024:
                return self._err("文件超过 60MB")
            name = "up_%s.%s" % (hashlib.sha1(raw).hexdigest()[:12], ext)
            with open(os.path.join(MEDIA_DIR, name), "wb") as f:
                f.write(raw)
            return self._ok({"url": "/media/" + name, "kind": kind})

        # 镜头
        m = re.match(r"/api/shots/(\d+)$", p)
        if m:
            sid = int(m.group(1))
            if "duration" in body:
                try:
                    d = int(float(body["duration"]))
                except Exception:
                    return self._err("时长必须是数字")
                if d < 1 or d > 15:
                    return self._err("单镜时长只允许 1–15 秒（当前 %s）" % body["duration"])
                body["duration"] = d
            allowed = ("duration", "shot_size", "camera", "movement", "scene_slug", "action",
                       "emotion", "transition", "locked", "status", "res_tier", "ref_prev",
                       "perf_lock", "prompt_lock", "seed", "parts_json")
            sets, vals = [], []
            for k in allowed:
                if k in body:
                    sets.append(f"{k}=?")
                    vals.append(body[k])
            if "perf" in body:
                sets.append("perf_json=?")
                vals.append(json.dumps(body["perf"], ensure_ascii=False))
            for k, col in (("chars", "chars"), ("props", "props"), ("dialogue", "dialogue_json")):
                if k in body:
                    sets.append(f"{col}=?")
                    vals.append(json.dumps(body[k], ensure_ascii=False))
            if "blocking" in body:
                sets.append("blocking_json=?")
                vals.append(json.dumps(body["blocking"], ensure_ascii=False))
            for k, col in (("prompt_cn", "prompt_cn"), ("prompt_en", "prompt_en"), ("prompt_neg", "prompt_neg")):
                if k in body:
                    sets.append(f"{col}=?")
                    vals.append(body[k])
            if sets:
                with _db_lock:
                    db().execute(f"UPDATE shots SET {', '.join(sets)}, updated_at=? WHERE id=?",
                                 (*vals, now(), sid))
                    db().commit()
            # 编辑后重算签名基准：被改的那一级重新算"已生成"，其下游仍会因签名不匹配而失效
            shot = rowdict(db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone())
            project = get_project(shot["project_id"])
            assets = list_assets(shot["project_id"])
            with _db_lock:
                if "blocking" in body:
                    db().execute("UPDATE shots SET sig1_at_block=? WHERE id=?",
                                 (shot_sig1(project, shot, assets), sid))
                if any(k in body for k in ("prompt_cn", "prompt_en", "prompt_neg")):
                    db().execute("UPDATE shots SET sig2_at_prompt=? WHERE id=?",
                                 (shot_sig2(shot), sid))
                db().commit()
            return self._ok({"ok": True})

        # PRD 五.4：复制镜头 / 拖拽换序
        m = re.match(r"/api/shots/(\d+)/copy$", p)
        if m:
            sid = int(m.group(1))
            s = rowdict(db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone())
            if not s:
                return self._err("镜头不存在", 404)
            eid = s["episode_id"]
            with _db_lock:
                for r in [r["id"] for r in db().execute(
                        "SELECT id FROM shots WHERE episode_id=? AND idx>? ORDER BY idx DESC", (eid, s["idx"]))]:
                    db().execute("UPDATE shots SET idx=idx+1 WHERE id=?", (r,))
                cur = db().execute(
                    "INSERT INTO shots(project_id,episode_id,idx,duration,shot_size,camera,movement,scene_slug,"
                    "chars,props,action,emotion,dialogue_json,transition,workflow,status,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s["project_id"], eid, int(s["idx"]) + 1, s["duration"], s["shot_size"], s["camera"],
                     s["movement"], s["scene_slug"], s["chars"], s["props"], s["action"], s["emotion"],
                     s["dialogue_json"], s["transition"], s.get("workflow") or "", "preview", now(), now()))
                db().commit()
                nsid = cur.lastrowid
            return self._ok({"ok": True, "id": nsid, "state": compute_state(s["project_id"])})

        m = re.match(r"/api/shots/(\d+)/move$", p)
        if m:
            sid = int(m.group(1))
            s = rowdict(db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone())
            if not s:
                return self._err("镜头不存在", 404)
            eid, pid = s["episode_id"], s["project_id"]
            rows = [r["id"] for r in db().execute(
                "SELECT id FROM shots WHERE episode_id=? ORDER BY idx", (eid,))]
            pos = rows.index(sid)
            to = body.get("to")
            if to is None:
                to = pos + (1 if int(body.get("dir") or 1) > 0 else -1)
            to = max(0, min(len(rows) - 1, int(to)))
            if to == pos:
                return self._ok({"ok": True, "state": compute_state(pid)})
            order = [x for x in rows if x != sid]
            order.insert(to, sid)
            with _db_lock:
                for i, x in enumerate(order, 1):
                    db().execute("UPDATE shots SET idx=? WHERE id=?", (i, x))
                db().commit()
            return self._ok({"ok": True, "state": compute_state(pid)})

        # 生成 / 优化
        m = re.match(r"/api/generate/(\w+)", p)
        if m:
            kind = m.group(1)
            pid = int(body.get("project_id") or 0)
            eid = int(body.get("episode_id") or 0)
            shot_id = body.get("shot_id")
            if not pid:
                return self._err("缺 project_id")
            project = get_project(pid)
            if not project:
                return self._err("项目不存在")
            if not eid:
                ep = db().execute("SELECT id FROM episodes WHERE project_id=? ORDER BY idx", (pid,)).fetchone()
                if ep:
                    eid = ep["id"]
                else:
                    with _db_lock:
                        cur = db().execute("INSERT INTO episodes(project_id,idx,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                                           (pid, 1, "第1集", now(), now()))
                        db().commit()
                        eid = cur.lastrowid
            if kind in ("outline", "novel", "script", "board") and not project.get("target_locked"):
                return self._err("目标还没锁定：先锁死题材/画幅/集数/风格，再让 AI 动手")
            jid = new_job(pid, eid, kind, "shot" if shot_id else "episode")
            if kind == "outline":
                run_async(do_outline, jid, pid, eid)
            elif kind == "novel":
                run_async(do_novel, jid, pid, eid)
            elif kind == "script":
                run_async(do_script, jid, pid, eid)
            elif kind == "board":
                run_async(do_board, jid, pid, eid)
            elif kind == "blocking":
                run_async(do_blocking, jid, pid, eid, shot_id)
            elif kind == "prompt":
                run_async(do_prompt, jid, pid, eid, shot_id)
            elif kind == "video":
                run_async(do_video, jid, pid, eid, shot_id)
            elif kind == "optimize":
                run_async(do_optimize, jid, pid, eid, body.get("stage") or "prompt", shot_id)
            else:
                return self._err("未知生成类型 " + kind)
            return self._ok({"job_id": jid})

        # 合集
        m = re.match(r"/api/reel/export", p)
        if m:
            pid = int(body.get("project_id") or 0)
            eid = body.get("episode_id")
            name, path = export_reel(pid, eid, body.get("shot_ids"))
            return self._ok({"file": name, "url": "/exports/" + urllib.parse.quote(name)})
        if p == "/api/reel/ffmpeg":
            pid = int(body.get("project_id") or 0)
            name = concat_with_ffmpeg(pid, body.get("episode_id"), body.get("shot_ids"))
            return self._ok({"file": name, "url": "/exports/" + urllib.parse.quote(name)})

        # ── PRD 七：应用 / 取消 —— 只有点【应用】才覆盖本镜原内容
        m = re.match(r"/api/shots/(\d+)/chat/apply$", p)
        if m:
            sid = int(m.group(1))
            row = db().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err("镜头不存在", 404)
            shot = dict(row)
            apply_shot_updates(shot, body.get("updates") or {})
            with _db_lock:
                db().execute("INSERT INTO shot_chat(project_id,shot_id,role,text,created_at) VALUES(?,?,?,?,?)",
                             (shot["project_id"], sid, "assistant", "已应用到本镜（②③④ 标记需重跑）", now()))
                db().commit()
            log_activity(shot["project_id"], "chat", "镜头 %d 应用改稿" % shot["idx"])
            return self._ok({"ok": True, "state": compute_state(shot["project_id"])})

        # ── PRD 六：视频工作流预设（后台可配，不用改代码）
        m = re.match(r"/api/wf/([A-Za-z0-9_]+)$", p)
        if m and m.group(1) != "test":
            key = m.group(1)
            row = db().execute("SELECT * FROM wf_presets WHERE key=?", (key,)).fetchone()
            if not row:
                return self._err("工作流不存在", 404)
            sets, vals = [], []
            for k in ("name", "sub", "wf_file", "prefix_cn", "prefix_en", "neg_extra", "shot_rule", "params_json"):
                if k in body:
                    sets.append("%s=?" % k)
                    vals.append(str(body.get(k) or ""))
            if sets:
                with _db_lock:
                    db().execute("UPDATE wf_presets SET %s WHERE key=?" % ", ".join(sets), (*vals, key))
                    db().commit()
            return self._ok({"ok": True, "state": compute_state(body.get("project_id") or None)})

        m = re.match(r"/api/projects/(\d+)/workflow$", p)
        if m:
            pid = int(m.group(1))
            with _db_lock:
                db().execute("UPDATE projects SET video_wf=?, updated_at=? WHERE id=?",
                             (body.get("key") or "director", now(), pid))
                db().commit()
            log_activity(pid, "workflow", "项目默认视频工作流 → %s" % body.get("key"))
            return self._ok({"ok": True, "state": compute_state(pid)})

        m = re.match(r"/api/shots/(\d+)/workflow$", p)
        if m:
            sid = int(m.group(1))
            row = db().execute("SELECT project_id FROM shots WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err("镜头不存在", 404)
            with _db_lock:
                db().execute("UPDATE shots SET workflow=?, updated_at=? WHERE id=?",
                             (body.get("key") or "", now(), sid))
                db().commit()
            return self._ok({"ok": True, "state": compute_state(row["project_id"])})

        m = re.match(r"/api/episodes/(\d+)/workflow$", p)
        if m:
            eid = int(m.group(1))
            ep = get_episode(eid)
            if not ep:
                return self._err("集不存在", 404)
            with _db_lock:
                db().execute("UPDATE shots SET workflow=?, updated_at=? WHERE episode_id=?",
                             (body.get("key") or "", now(), eid))
                db().commit()
            return self._ok({"ok": True, "state": compute_state(ep["project_id"])})

        # ── PRD 八：角色音色（每个角色档案绑定固定音色）
        if p == "/api/voices":
            pid = int(body.get("project_id") or 0)
            if not pid:
                return self._err("缺 project_id")
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO voices(project_id,asset_id,name,engine,sample_url,params_json,viseme,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (pid, int(body.get("asset_id") or 0), body.get("name") or "新音色",
                     body.get("engine") or "local", "", json.dumps(body.get("params") or {}, ensure_ascii=False),
                     body.get("viseme") or "", now(), now()))
                db().commit()
                vid = cur.lastrowid
                if body.get("asset_id"):
                    db().execute("UPDATE assets SET voice_id=?, updated_at=? WHERE id=?",
                                 (vid, now(), int(body["asset_id"])))
                    db().commit()
            return self._ok({"ok": True, "id": vid, "state": compute_state(pid)})

        m = re.match(r"/api/voices/(\d+)$", p)
        if m:
            vid = int(m.group(1))
            v = voice_row(vid)
            if not v:
                return self._err("音色不存在", 404)
            sets, vals = [], []
            for k in ("name", "engine", "viseme", "sample_url", "asset_id"):
                if k in body:
                    sets.append("%s=?" % k)
                    vals.append(body[k])
            if "params" in body:
                sets.append("params_json=?")
                vals.append(json.dumps(body["params"], ensure_ascii=False))
            if sets:
                with _db_lock:
                    db().execute("UPDATE voices SET %s, updated_at=? WHERE id=?" % ", ".join(sets),
                                 (*vals, now(), vid))
                    db().commit()
            return self._ok({"ok": True, "state": compute_state(v["project_id"])})

        m = re.match(r"/api/voices/(\d+)/sample$", p)
        if m:
            vid = int(m.group(1))
            v = voice_row(vid)
            if not v:
                return self._err("音色不存在", 404)
            text = (body.get("text") or "这是%s的试听音色，全程固定不变。" % (v.get("name") or "角色")).strip()
            base = (get_setting("tts_base") or "").strip()
            if not base:
                # 本机没有 TTS 通道：明确报未接入，不假装出声
                return self._err("未接入配音引擎：系统设置里填 TTS 地址后才能生成合音"
                                 "（当前不会用占位音频冒充）", 400)
            try:
                req = urllib.request.Request(
                    base.rstrip("/") + "/audio/speech",
                    data=json.dumps({"model": "tts-1", "input": text, "voice": v.get("engine") or "alloy"},
                                    ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + (get_setting("tts_key") or "")})
                with urllib.request.urlopen(req, timeout=90) as r:
                    audio = r.read()
                fn = os.path.join(MEDIA_DIR, "voice_%d_%s.mp3" % (vid, uuid.uuid4().hex[:8]))
                with open(fn, "wb") as f:
                    f.write(audio)
                url = "/media/" + os.path.basename(fn)
                with _db_lock:
                    db().execute("UPDATE voices SET sample_url=?, updated_at=? WHERE id=?", (url, now(), vid))
                    db().commit()
                return self._ok({"ok": True, "url": url})
            except Exception as e:
                return self._err("TTS 调用失败：%s: %s" % (type(e).__name__, str(e)[:100]))

        m = re.match(r"/api/assets/(\d+)/voice$", p)
        if m:
            aid = int(m.group(1))
            a = rowdict(db().execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())
            if not a:
                return self._err("资产不存在", 404)
            with _db_lock:
                db().execute("UPDATE assets SET voice_id=?, updated_at=? WHERE id=?",
                             (int(body.get("voice_id") or 0), now(), aid))
                db().commit()
            return self._ok({"ok": True, "state": compute_state(a["project_id"])})

        # ── PRD 九：音乐创作 / 音乐播放（固定本地 ComfyUI）
        if p == "/api/music":
            pid = int(body.get("project_id") or 0)
            if not pid:
                return self._err("缺 project_id")
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO music(project_id,kind,name,style,mood,duration,bpm,prompt,status,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,'draft',?,?)",
                    (pid, body.get("kind") or "bgm", body.get("name") or "未命名配乐",
                     body.get("style") or "影视配乐", body.get("mood") or "温馨",
                     int(body.get("duration") or 30), int(body.get("bpm") or 90),
                     body.get("prompt") or "", now(), now()))
                db().commit()
                mid = cur.lastrowid
            log_activity(pid, "music", "新建音乐 %s" % body.get("name"))
            return self._ok({"ok": True, "id": mid, "state": compute_state(pid)})

        m = re.match(r"/api/music/(\d+)/generate$", p)
        if m:
            mid = int(m.group(1))
            mrow = music_row(mid)
            if not mrow:
                return self._err("音乐不存在", 404)
            jid = new_job(mrow["project_id"], 0, "music", "music")
            with _db_lock:
                db().execute("UPDATE music SET job_id=?, status='queued', updated_at=? WHERE id=?", (jid, now(), mid))
                db().commit()
            run_async(do_music, jid, mid)
            return self._ok({"job_id": jid, "id": mid, "state": compute_state(mrow["project_id"])})

        m = re.match(r"/api/music/(\d+)/link$", p)
        if m:
            mid = int(m.group(1))
            mrow = music_row(mid)
            if not mrow:
                return self._err("音乐不存在", 404)
            with _db_lock:
                db().execute("UPDATE music SET linked_ep=?, updated_at=? WHERE id=?",
                             (int(body.get("episode_id") or 0), now(), mid))
                db().commit()
            return self._ok({"ok": True, "state": compute_state(mrow["project_id"])})

        m = re.match(r"/api/music/(\d+)$", p)
        if m:
            mid = int(m.group(1))
            mrow = music_row(mid)
            if not mrow:
                return self._err("音乐不存在", 404)
            sets, vals = [], []
            for k in ("name", "style", "mood", "prompt", "kind"):
                if k in body:
                    sets.append("%s=?" % k)
                    vals.append(str(body.get(k) or ""))
            for k in ("duration", "bpm", "linked_ep"):
                if k in body:
                    sets.append("%s=?" % k)
                    vals.append(int(body.get(k) or 0))
            if sets:
                with _db_lock:
                    db().execute("UPDATE music SET %s, updated_at=? WHERE id=?" % ", ".join(sets),
                                 (*vals, now(), mid))
                    db().commit()
            return self._ok({"ok": True, "state": compute_state(mrow["project_id"])})

        # ── PRD 十：系统设置（7 个子项共用一份配置）
        if p == "/api/settings":
            saved = save_settings(body)
            return self._ok({"ok": True, "saved": saved, "settings": settings_payload(),
                             "server": server_info()})
        if p == "/api/settings/test":
            ok, msg = test_endpoint(body.get("kind") or "llm", body)
            return self._ok({"ok": ok, "msg": msg})
        if p == "/api/settings/models":
            ok, msg, models = fetch_model_list(body.get("base") or "", body.get("key") or "",
                                               body.get("provider") or "")
            return self._ok({"ok": ok, "msg": msg, "models": models})

        # ── PRD 十三：复制集（新增空集 / 插入集 / 复制集）
        m = re.match(r"/api/episodes/(\d+)/copy$", p)
        if m:
            eid = int(m.group(1))
            src = get_episode(eid)
            if not src:
                return self._err("集不存在", 404)
            pid = src["project_id"]
            nxt = db().execute("SELECT MAX(idx) m FROM episodes WHERE project_id=?", (pid,)).fetchone()["m"] or 0
            new_idx = int(nxt) + 1
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO episodes(project_id,idx,title,outline_json,novel_text,script_json,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (pid, new_idx, (src.get("title") or "") + "（副本）", src.get("outline_json") or "",
                     src.get("novel_text") or "", src.get("script_json") or "", now(), now()))
                db().commit()
                neid = cur.lastrowid
                for s in list_shots(eid):
                    db().execute(
                        "INSERT INTO shots(project_id,episode_id,idx,duration,shot_size,camera,movement,scene_slug,"
                        "chars,props,action,emotion,dialogue_json,transition,workflow,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, neid, s["idx"], s["duration"], s["shot_size"], s["camera"], s["movement"],
                         s["scene_slug"], s["chars"], s["props"], s["action"], s["emotion"],
                         s["dialogue_json"], s["transition"], s.get("workflow") or "", now(), now()))
                db().execute("UPDATE projects SET episode_count=?, updated_at=? WHERE id=?",
                             (new_idx, now(), pid))
                db().commit()
            return self._ok({"ok": True, "id": neid, "idx": new_idx, "state": compute_state(pid)})

        # PRD 十三：新增空集 / 插入集（after 指定插在某集之后）
        if p == "/api/episodes":
            e_pid = int(body.get("project_id") or 0)
            if not e_pid:
                return self._err("缺 project_id")
            after = int(body.get("after") or 0)
            after_idx = 0
            if after:
                e = get_episode(after)
                if e and e["project_id"] == e_pid:
                    after_idx = int(e["idx"] or 0)
            if not after:
                after_idx = db().execute("SELECT MAX(idx) m FROM episodes WHERE project_id=?",
                                         (e_pid,)).fetchone()["m"] or 0
            new_idx = after_idx + 1
            with _db_lock:
                for r in [r["id"] for r in db().execute(
                        "SELECT id FROM episodes WHERE project_id=? AND idx>=? ORDER BY idx DESC",
                        (e_pid, new_idx))]:
                    db().execute("UPDATE episodes SET idx=idx+1 WHERE id=?", (r,))
                cur = db().execute(
                    "INSERT INTO episodes(project_id,idx,title,outline_json,novel_text,script_json,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (e_pid, new_idx, body.get("title") or ("第 %d 集" % new_idx), "", "", "", now(), now()))
                db().commit()
                neid = cur.lastrowid
                cnt = db().execute("SELECT COUNT(*) c FROM episodes WHERE project_id=?",
                                   (e_pid,)).fetchone()["c"]
                db().execute("UPDATE projects SET episode_count=?, updated_at=? WHERE id=?",
                             (cnt, now(), e_pid))
                db().commit()
            return self._ok({"ok": True, "id": neid, "idx": new_idx, "state": compute_state(e_pid)})

        m = re.match(r"/api/episodes/(\d+)/delete$", p)
        if m:
            eid = int(m.group(1))
            e = get_episode(eid)
            if not e:
                return self._err("集不存在", 404)
            pid = e["project_id"]
            removed = 0
            with _db_lock:
                for s in list_shots(eid):
                    for u in (s.get("video_url"), s.get("thumb_url")):
                        if u and "/media/" in str(u):
                            fp = os.path.join(MEDIA_DIR, str(u).split("/media/")[-1])
                            try:
                                if os.path.isfile(fp):
                                    os.remove(fp)
                                    removed += 1
                            except Exception:
                                pass
                    db().execute("DELETE FROM shot_chat WHERE shot_id=?", (s["id"],))
                db().execute("DELETE FROM shots WHERE episode_id=?", (eid,))
                db().execute("DELETE FROM episodes WHERE id=?", (eid,))
                db().commit()
                left = [r["id"] for r in db().execute(
                    "SELECT id FROM episodes WHERE project_id=? ORDER BY idx", (pid,))]
                for i, x in enumerate(left, 1):
                    db().execute("UPDATE episodes SET idx=? WHERE id=?", (i, x))
                db().commit()
                db().execute("UPDATE projects SET episode_count=?, updated_at=? WHERE id=?",
                             (len(left), now(), pid))
                db().commit()
            return self._ok({"ok": True, "removed_files": removed, "state": compute_state(pid)})

        return self._err("未知接口 " + p, 404)

    def serve_file(self, path, ctype=None, stream=False):
        if not os.path.exists(path) or not os.path.isfile(path):
            return self._err("文件不存在 " + path, 404)
        ext = os.path.splitext(path)[1].lower()
        ctype = ctype or {
            ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
            ".mp4": "video/mp4", ".webm": "video/webm", ".png": "image/png",
            ".jpg": "image/jpeg", ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng and ext in (".mp4", ".webm"):
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1)) if m else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                self.wfile.write(f.read(end - start + 1))
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 256 * 1024)


# ---------------------------------------------------------------- 首次种子数据

def seed_demo():
    if get_setting("seeded") == "1":
        return
    if db().execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]:
        set_setting("seeded", "1")
        return
    pid = None
    with _db_lock:
        cur = db().execute(
            "INSERT INTO projects(title,premise,genre,platform,audience,ratio,episode_count,episode_length,"
            "style_lock,forbidden,target_locked,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("念念的三个月", "五岁半的念念被豪门赶出门，靠半块糖和一张旧照片，一步步把自己的名字要回来",
             "萌娃团宠", "抖音 / 番茄短剧", "25-45 岁女性", "9:16", 12, "每集 75-95 秒",
             "冷调写实 + 暖光收尾；35mm 与 85mm 交替；轻微手持", "不写血腥、不写未成年恋爱、不写真实品牌",
             1, now(), now()))
        db().commit()
        pid = cur.lastrowid
        eid = db().execute("INSERT INTO episodes(project_id,idx,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (pid, 1, "第1集", now(), now())).lastrowid
        db().commit()
    project = get_project(pid)
    episode = get_episode(eid)
    outline = engine.local_outline(ctx_for(project, episode))
    write_outline(pid, eid, outline)
    episode = get_episode(eid)
    novel = engine.local_novel(ctx_for(project, episode))
    with _db_lock:
        db().execute("UPDATE episodes SET novel_text=?, novel_sig=?, updated_at=? WHERE id=?",
                     (novel, sha(json.dumps(outline, ensure_ascii=False)), now(), eid))
        db().commit()
    episode = get_episode(eid)
    script = engine.local_script(ctx_for(project, episode))
    with _db_lock:
        db().execute("UPDATE episodes SET script_json=?, script_sig=?, updated_at=? WHERE id=?",
                     (json.dumps(script, ensure_ascii=False), sha(novel), now(), eid))
        db().commit()
    ensure_scene_assets(pid, script)
    episode = get_episode(eid)
    shots = engine.local_storyboard(ctx_for(project, episode))
    with _db_lock:
        for i, s in enumerate(shots):
            db().execute(
                "INSERT INTO shots(project_id,episode_id,idx,duration,shot_size,camera,movement,scene_slug,"
                "chars,props,action,emotion,dialogue_json,transition,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, eid, i + 1, s.get("duration"), s.get("shot_size"), s.get("camera"), s.get("movement"),
                 s.get("scene_slug"), json.dumps(s.get("chars") or [], ensure_ascii=False),
                 json.dumps(s.get("props") or [], ensure_ascii=False), s.get("action"), s.get("emotion"),
                 json.dumps(s.get("dialogue") or [], ensure_ascii=False), s.get("transition"), now(), now()))
        db().execute("UPDATE episodes SET board_sig=?, updated_at=? WHERE id=?",
                     (sha(json.dumps(script, ensure_ascii=False)), now(), eid))
        db().commit()
    # 前 6 镜做完整示范：调度 + 提示词
    assets = list_assets(pid)
    for shot in list_shots(eid)[:6]:
        b = engine.local_blocking(ctx_for(project, episode, shot))
        p = (engine.local_prompt_v2({"project": project, "assets": assets, "shot": shot,
                                     "blocking": b, "perf": perf_of(shot)})
             if (project.get("prompt_mode") or "cn") == "h3ref"
             else engine.local_prompt({"project": project, "assets": assets, "shot": shot, "blocking": b}))
        with _db_lock:
            db().execute("UPDATE shots SET blocking_json=?, prompt_cn=?, prompt_en=?, prompt_neg=?,"
                         "sig1_at_block=?, sig2_at_prompt=?, updated_at=? WHERE id=?",
                         (json.dumps(b, ensure_ascii=False), p["cn"], p["en"], p["neg"],
                          shot_sig1(project, shot, assets), shot_sig2(shot), now(), shot["id"]))
            db().commit()
    set_setting("seeded", "1")
    print(f"[seed] 已创建示例项目 {project['title']} · {len(list_shots(eid))} 镜")


def main():
    init_db()
    seed_demo()
    # ComfyUI 与平台同生共死：后台守护，端口掉了自动拉起
    threading.Thread(target=comfy_watchdog, daemon=True).start()
    host = os.environ.get("STUDIO_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, PORT), Handler)
    print("=" * 56)
    print(f"  AI 视频创作系统 ·  http://{host}:{PORT}")
    print(f"  数据目录：{DATA_DIR}")
    print("  关闭此窗口即停止服务")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # 服务重启会把上一轮 in-flight 的任务线程掐掉：启动时把这些僵尸任务标成中断，别让界面一直「运行中」
    try:
        with _db_lock:
            db().execute("UPDATE jobs SET status='failed', message='服务重启中断（重跑一次即可）', updated_at=? "
                         "WHERE status IN ('running','queued')", (now(),))
            db().execute("UPDATE shots SET status='preview', error='生成被服务重启中断，重跑这一镜即可' "
                         "WHERE status='running'")
            db().commit()
    except Exception:
        pass
    main()
