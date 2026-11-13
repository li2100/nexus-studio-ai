# -*- coding: utf-8 -*-
"""分镜预演帧渲染：把「场面调度」的结构化数据直接画成 16:9 / 9:16 预演图。

这不是装饰性占位图，而是调度结果的可视化：
- 景别 → 画框裁切与人物占画比
- 机位高度/轴线 → 地平线高度与影子方向
- 构图 → 三分线 / 前景区隔 / 负空间
- 光位与色温 → 明暗与色偏
- 走位 → 人物 anchor 与站位
- 台词 → 底部字幕条
"""
from __future__ import annotations

import html
import math

RATIOS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080),
          "4:3": (1440, 1080), "3:4": (1080, 1440), "21:9": (1920, 823)}


def _temp_color(temp: str) -> tuple[str, str]:
    t = (temp or "").replace("K", "").strip()
    try:
        kelvin = int(t)
    except Exception:
        kelvin = 5000
    if kelvin <= 3600:
        return "#f2a45c", "#2a1a12"   # 暖
    if kelvin >= 5200:
        return "#8fb8d6", "#12181f"   # 冷
    return "#cfd6de", "#171a1e"


def render(shot: dict, project: dict, blocking: dict) -> str:
    ratio = (project.get("ratio") or "9:16").replace("：", ":")
    W, H = RATIOS.get(ratio, RATIOS["9:16"])
    size = shot.get("shot_size") or "中景"
    light = (blocking or {}).get("light") or {}
    accent, bg = _temp_color(light.get("temp") or "5000K")
    cam = (blocking or {}).get("camera") or {}
    talent = (blocking or {}).get("talent") or []
    dur = int(shot.get("duration") or 5)
    slug = shot.get("scene_slug") or ""
    dlg = shot.get("dialogue") or []
    line = " / ".join(f"{d.get('sp')}：{d.get('line')}" for d in dlg)
    idx = shot.get("idx") or 0
    move = shot.get("movement") or "固定"
    side = cam.get("side") or "轴线左侧"

    # 人物占画比（景别决定：占画面高度的比例）
    occ = {"大全景": 0.30, "全景": 0.52, "中景": 0.66, "中近景": 0.78,
           "近景": 0.96, "特写": 1.32, "大特写": 1.75}.get(size, 0.68)

    def person(x, y, name, alpha=1.0, z=1.0):
        """x/y 是脚底落点（归一化）；近景/特写自动改成头顶落在上三分之一。"""
        px = x * W
        th = H * occ * z
        hr = th / 15.0                     # 头半径：全身约 7.5 头身
        if occ <= 0.78:
            feet = y * H
            head_cy = feet - th + hr
        else:
            head_cy = H * (0.40 if occ >= 1.0 else 0.34)
            feet = head_cy + th - hr
        sh_cy = head_cy + hr * 1.45          # 肩线（留一小段脖子，不留空洞）
        sh_w = hr * 2.1
        waist_y = sh_cy + th * 0.34
        waist_w = hr * 1.65
        s = []
        s.append(f'<g opacity="{alpha}">')
        if occ <= 0.78:
            s.append(f'<ellipse cx="{px:.0f}" cy="{feet + 4:.0f}" rx="{sh_w * 1.5:.0f}" '
                     f'ry="{max(4, th * 0.022):.0f}" fill="#000" opacity="0.42"/>')
            s.append(f'<ellipse cx="{px:.0f}" cy="{feet + 2:.0f}" rx="{sh_w * 2.4:.0f}" '
                     f'ry="{max(6, th * 0.05):.0f}" fill="{accent}" opacity="0.10"/>')
        s.append(f'<path d="M {px - sh_w:.0f} {sh_cy:.0f} '
                 f'Q {px - sh_w * 0.9:.0f} {waist_y:.0f} {px - waist_w:.0f} {waist_y:.0f} '
                 f'L {px - waist_w * 0.92:.0f} {feet:.0f} L {px + waist_w * 0.92:.0f} {feet:.0f} '
                 f'L {px + waist_w:.0f} {waist_y:.0f} '
                 f'Q {px + sh_w * 0.9:.0f} {waist_y:.0f} {px + sh_w:.0f} {sh_cy:.0f} Z" '
                 f'fill="#1c2733" stroke="{accent}" stroke-opacity="0.9" stroke-width="2.4"/>')
        s.append(f'<circle cx="{px:.0f}" cy="{head_cy:.0f}" r="{hr:.0f}" '
                 f'fill="#1c2733" stroke="{accent}" stroke-opacity="0.95" stroke-width="2.4"/>')
        s.append(f'<text x="{px:.0f}" y="{min(H - 8, feet + 22):.0f}" fill="{accent}" '
                 f'font-size="{max(12, W * 0.016):.0f}" text-anchor="middle" opacity="0.85" '
                 f'font-family="Microsoft YaHei">{html.escape(str(name))}</text>')
        s.append("</g>")
        return "".join(s)

    g = []
    g.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
    g.append(f"""<defs>
<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{bg}"/><stop offset="0.42" stop-color="#182430"/><stop offset="1" stop-color="#0a0f14"/>
</linearGradient>
<radialGradient id="key" cx="0.18" cy="0.25" r="0.75">
<stop offset="0" stop-color="{accent}" stop-opacity="0.30"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/>
</radialGradient>
</defs>""")
    g.append(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>')
    g.append(f'<rect width="{W}" height="{H}" fill="url(#key)"/>')
    # 地平线（机位高度决定）
    hz = 0.62 if "平视" in (cam.get("height") or "") else (
        0.72 if "俯" in (cam.get("height") or "") else 0.52)
    g.append(f'<line x1="0" y1="{hz * H:.0f}" x2="{W}" y2="{hz * H:.0f}" stroke="{accent}" '
             f'stroke-opacity="0.18" stroke-width="2"/>')
    # 环境暗示线
    for i in range(1, 5):
        y = hz * H + i * i * H * 0.012
        g.append(f'<line x1="0" y1="{y:.0f}" x2="{W}" y2="{y:.0f}" stroke="#ffffff" stroke-opacity="0.03"/>')
    # 三分线
    for f in (1 / 3, 2 / 3):
        g.append(f'<line x1="{W * f:.0f}" y1="0" x2="{W * f:.0f}" y2="{H}" stroke="#ffffff" stroke-opacity="0.06" stroke-dasharray="6 10"/>')
        g.append(f'<line x1="0" y1="{H * f:.0f}" x2="{W}" y2="{H * f:.0f}" stroke="#ffffff" stroke-opacity="0.06" stroke-dasharray="6 10"/>')
    # 前景遮挡（增加空间层次）
    if size in ("全景", "中景", "大全景"):
        g.append(f'<rect x="0" y="0" width="{W * 0.07:.0f}" height="{H}" fill="#000" opacity="0.55"/>')
        g.append(f'<rect x="0" y="0" width="{W * 0.07:.0f}" height="{H}" fill="none" stroke="{accent}" stroke-opacity="0.25"/>')
    # 人物
    for i, t in enumerate(talent):
        pos = t.get("pos") or [0.5, 0.6]
        z = 1.0 if i == 0 else 0.82
        alpha = 1.0 if i == 0 else 0.75
        g.append(person(pos[0], pos[1], t.get("name") or "主角", alpha, z))
    if not talent:
        g.append(person(0.5, 0.62, "主体"))

    # 顶部信息条
    fs = max(13, int(W * 0.017))
    g.append(f'<rect x="{W*0.04:.0f}" y="{H*0.035:.0f}" width="{W*0.62:.0f}" height="{fs*2.6:.0f}" fill="#000" opacity="0.42"/>')
    g.append(f'<text x="{W*0.055:.0f}" y="{H*0.035+fs*1.0:.0f}" fill="#e8eef5" font-size="{fs}" '
             f'font-family="Microsoft YaHei" letter-spacing="1">SHOT {idx:02d} · {html.escape(size)} · {dur}s</text>')
    g.append(f'<text x="{W*0.055:.0f}" y="{H*0.035+fs*2.1:.0f}" fill="{accent}" font-size="{fs*0.82:.0f}" '
             f'font-family="Microsoft YaHei" opacity="0.9">{html.escape(slug[:30])} · {html.escape(side)}</text>')

    # 焦距 / 运镜 角标
    g.append(f'<text x="{W*0.96:.0f}" y="{H*0.035+fs*1.4:.0f}" fill="#ffffff" opacity="0.6" '
             f'font-size="{fs*0.9:.0f}" text-anchor="end" font-family="Consolas,monospace">'
             f'{html.escape((blocking or {}).get("lens") or "35mm")}</text>')
    g.append(f'<text x="{W*0.96:.0f}" y="{H*0.035+fs*2.6:.0f}" fill="{accent}" opacity="0.9" '
             f'font-size="{fs*0.8:.0f}" text-anchor="end" font-family="Microsoft YaHei">'
             f'{html.escape(move)} ↗</text>')

    # 光位标
    lx = W * 0.5
    g.append(f'<g opacity="0.75"><circle cx="{W*0.10:.0f}" cy="{H*0.86:.0f}" r="{fs*0.55:.0f}" fill="{accent}"/>')
    g.append(f'<text x="{W*0.14:.0f}" y="{H*0.868:.0f}" fill="#ffffff" opacity="0.65" font-size="{fs*0.72:.0f}" '
             f'font-family="Microsoft YaHei">{html.escape(light.get("dir") or "侧光")} / {html.escape(light.get("temp") or "5000K")}</text></g>')

    # 字幕条
    if line:
        bh = fs * 3.4
        ty = H - bh - H * 0.035
        g.append(f'<rect x="{W*0.04:.0f}" y="{ty:.0f}" width="{W*0.92:.0f}" height="{bh:.0f}" fill="#000" opacity="0.55" rx="{fs*0.3:.0f}"/>')
        txt = line if len(line) <= 34 else line[:33] + "…"
        g.append(f'<text x="{W*0.5:.0f}" y="{ty + bh*0.62:.0f}" fill="#f2f5f8" font-size="{fs*1.02:.0f}" '
                 f'text-anchor="middle" font-family="Microsoft YaHei">{html.escape(txt)}</text>')

    # 画框安全线
    g.append(f'<rect x="{W*0.02:.0f}" y="{H*0.02:.0f}" width="{W*0.96:.0f}" height="{H*0.96:.0f}" fill="none" '
             f'stroke="{accent}" stroke-opacity="0.16"/>')
    # 顶部渐变压暗
    g.append(f'<rect x="0" y="0" width="{W}" height="{H*0.14:.0f}" fill="#000" opacity="0.25"/>')
    g.append("</svg>")
    return "".join(g)
