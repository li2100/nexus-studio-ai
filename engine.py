# -*- coding: utf-8 -*-
"""本地创作引擎（Local Creative Engine）

设计目标：
1) 有 LLM 时，app.py 优先走模型；
2) 无 Key / 余额不足 / 断网时，本引擎必须仍能产出结构完整、可继续加工的创作稿；
3) 所有产出都是"结构化数据"，方便下游（场面调度 / 提示词 / 生成）逐级引用与失效重跑。
"""
from __future__ import annotations

import hashlib
import json
import random
import re

# ---------------------------------------------------------------- 基础工具

def rng_of(*parts) -> random.Random:
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return random.Random(int(h[:12], 16))


def _as_list(v) -> list:
    """DB 里的数组字段是 JSON 字符串，直接 join 会变成逐字拼接，这里统一还原。"""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            return json.loads(v)
        except Exception:
            return []
    return []


def _target_seconds(project: dict) -> float:
    s = (project or {}).get("episode_length") or ""
    nums = [int(x) for x in re.findall(r"\d+", s)]
    if nums:
        return sum(nums) / len(nums)
    return 90.0


GENRES = [
    ("团宠", "萌娃团宠"), ("萌娃", "萌娃团宠"), ("寻亲", "萌娃团宠"),
    ("豪门", "豪门恩怨"), ("总裁", "都市情感"), ("重生", "重生复仇"),
    ("穿越", "穿越逆袭"), ("战神", "都市战神"), ("赘婿", "赘婿逆袭"),
    ("医妃", "古装医妃"), ("宫廷", "古装权谋"), ("悬疑", "悬疑推理"),
    ("复仇", "复仇爽剧"), ("甜宠", "甜宠"), ("修仙", "东方玄幻"),
    ("职场", "都市职场"), ("校园", "青春校园"), ("末日", "末世求生"),
    ("科幻", "科幻"), ("民间", "民间奇谈"),
]

NAME_BANK = ["念念", "小满", "阿棠", "沈砚", "陆知白", "周慕", "苏晚",
             "江烬", "林拾月", "顾南舟", "叶行舟", "温以宁", "陆星野", "许砚青",
             "陈屿", "白露", "何九斤", "宋晚棠", "程野", "夏见微"]

PROP_BANK = {
    "萌娃团宠": [("红色小书包", "装着全部家当，也是被认出身份的信物"),
                 ("半块糖", "舍不得吃，用来跟人换消息"),
                 ("旧照片", "边角被水泡过，藏着真正的关系")],
    "豪门恩怨": [("股权文件", "决定谁留下谁出局"),
                 ("母亲的戒指", "唯一没被收走的东西"),
                 ("录音笔", "一句话就能掀翻的局面")],
    "重生复仇": [("旧日记", "上一世写到一半就断了"),
                 ("半张车票", "回到那天的凭证"),
                 ("磨旧的钥匙", "开一扇不该开的门")],
    "默认": [("旧背包", "装着走之前全部的家当"),
             ("一封没寄出的信", "写了三遍，撕了两遍"),
             ("一把小钥匙", "对应一个还没被打开的房间")],
}


def detect_genre(text: str) -> str:
    for key, label in GENRES:
        if key in (text or ""):
            return label
    return "都市情感"


def pick_names(premise: str, genre: str, rng: random.Random) -> dict:
    """尽量从用户设定的创意里取名字，取不到就用题材化名库。"""
    found = []
    for cand in re.findall(r"[\u4e00-\u9fa5]{2,4}", premise or ""):
        if cand in NAME_BANK:
            found.append(cand)
    if not found:
        order = NAME_BANK[:]
        rng.shuffle(order)
        found = order[:4]
    else:
        extra = [n for n in NAME_BANK if n not in found]
        rng.shuffle(extra)
        found += extra[: max(0, 4 - len(found))]
    lead = found[0]
    rival = found[1] if len(found) > 1 else "周慕"
    kin = found[2] if len(found) > 2 else "苏晚"
    helper = found[3] if len(found) > 3 else "陆知白"
    return {"lead": lead, "rival": rival, "kin": kin, "helper": helper}


def _place(genre: str, rng: random.Random) -> str:
    bank = {
        "萌娃团宠": ["城南旧巷的杂货铺", "雨还没停的幼儿园门口", "城郊的老宅厨房", "夜市的糖水摊"],
        "豪门恩怨": ["顶层公寓的落地窗前", "家族会议室外走廊", "地下车库", "老宅的书房"],
        "重生复仇": ["上辈子咽气的那间病房", "重新亮灯的出租屋", "冰冷的董事会走廊", "雨夜的旧车站"],
        "古装医妃": ["冷宫偏殿", "太医院药房", "王府后园的假山后", "宫门外的雪地"],
        "都市战神": ["城中村的早点摊", "写字楼地下二层", "码头集装箱", "急诊室走廊"],
        "末日求生": ["停电的超市冷柜前", "楼顶水箱旁", "废弃地铁口", "雨里的高架桥"],
        "默认": ["凌晨的便利店", "老楼的楼梯间", "雨天的公交站台", "堆满纸箱的出租屋"],
    }
    key = genre if genre in bank else "默认"
    return rng.choice(bank[key])


# ---------------------------------------------------------------- 大纲

def local_outline(ctx: dict) -> dict:
    premise = (ctx.get("premise") or "").strip()
    project = ctx.get("project") or {}
    genre = project.get("genre") or detect_genre(premise)
    ep_count = int(project.get("episode_count") or 12)
    ep_count = max(6, min(30, ep_count))
    rng = rng_of("outline", premise, genre, ep_count)
    names = pick_names(premise, genre, rng)
    lead, rival, kin, helper = names["lead"], names["rival"], names["kin"], names["helper"]
    core = premise[:36] if premise else f"一个被轻视的{lead}，用三个月把自己重新写了一遍"

    act_names = ["第一幕 · 失去", "第二幕 · 交换", "第三幕 · 反打"]
    arc_titles = {
        "萌娃团宠": ["被赶出门的那天", "第一口热饭", "认错人的豪门", "会读心的幼儿园",
                     "半块糖换来的消息", "雨夜里的证明", "全网都在找她", "真凶是自己人",
                     "旧照片上的第二个孩子", "一场公开的对质", "她不叫原来的名字", "门口又停了一辆车"],
        "豪门恩怨": ["继承权被抽走", "会议室的第二把椅子", "旧账本", "她开始反击",
                     "订婚宴上的空位", "被换掉的鉴定报告", "父亲留下的录音", "同盟倒戈",
                     "董事会突袭", "谁在说谎", "最后一票", "门开的时候"],
        "重生复仇": ["回到出事前七天", "第一笔钱", "把背叛者扶上位", "提前拆掉那颗雷",
                     "旧日记的下一页", "第二次葬礼", "她学会了对手的手法", "证人改口",
                     "宴会上的录像", "所有人的位置互换", "她不再解释", "这次由她关门"],
        "默认": ["天没亮就走了", "第一笔定金", "被换掉的合同", "有人替她挡了一下",
                 "旧发票里的名字", "夜里的那次谈判", "内鬼浮出水面", "公开的道歉",
                 "她把证据推过去", "对面开始慌", "结算日", "重新开张"],
    }
    bank = arc_titles.get(genre, arc_titles["默认"])

    episodes = []
    acts = {0: [], 1: [], 2: []}
    for i in range(ep_count):
        title = bank[i] if i < len(bank) else f"{genre} · 第{i + 1}阶段"
        act_i = 0 if i < ep_count / 3 else (1 if i < ep_count * 2 / 3 else 2)
        hook = [
            f"{lead}刚把最后一个箱子搬出门，身后那扇门就被人从里面锁死了。",
            f"她第一次拿到钱的那天，{rival}恰好也在场。",
            f"所有人以为她输了，只有她知道那张纸上的签名不对。",
            f"电话那头的人只说了一句：{lead}，你被换了。",
        ][i % 4]
        climax = [
            f"{lead}把那份文件正面推到桌面中央，一句废话都没有。",
            f"{rival}当众改口，{kin}的手在桌下抖了一下。",
            f"灯光被人打开，{lead}站在门口，所有人都得抬头看她。",
            f"门外传来第二声喇叭，{helper}推门进来，手里拎着那个旧背包。",
        ][(i + 1) % 4]
        payoff = [
            "这一集结尾，观众第一次确认：她不是被同情的那一方，她是握着证据的那一方。",
            "钩子落在下集：名单上还有第三个名字，而那个人就在饭桌上。",
            f"结尾定格在{lead}的侧脸，她笑了一下，镜头却没有跟着她转向门口。",
            "所有人都松了口气，只有观众看见桌角那张纸被悄悄收进了口袋。",
        ][(i + 2) % 4]
        item = {
            "idx": i + 1,
            "title": f"第{i + 1}集 · {title}",
            "hook": hook,
            "beats": [
                f"开场 3 秒：{_place(genre, rng)}，{lead}处于最不利的位置。",
                f"中段反转：{rival}以为自己赢了，把话说满。",
                f"信息增量：观众拿到一个新证据（道具线索）。",
            ],
            "climax": climax,
            "payoff": payoff,
            "duration": "75-95 秒",
        }
        acts[act_i].append(item)

    props = PROP_BANK.get(genre, PROP_BANK["默认"])
    outline = {
        "title": (project.get("title") or f"《{lead}的三个月》"),
        "genre": genre,
        "logline": core,
        "target": {
            "platform": project.get("platform") or "抖音 / 番茄短剧",
            "audience": project.get("audience") or "25-45 岁女性为主，通勤碎片时间观看",
            "ratio": project.get("ratio") or "9:16",
            "episode_count": ep_count,
            "episode_length": project.get("episode_length") or "每集 75-95 秒",
            "style_lock": project.get("style_lock") or "冷调写实 + 一点点暖光收尾；手持与固定交替；克制配乐",
            "forbidden": project.get("forbidden") or "不写血腥、不写未成年恋爱、不写真实品牌",
        },
        "acts": [
            {"name": act_names[0], "beat": "把主角打到最低点，让观众站到她这边", "episodes": acts[0]},
            {"name": act_names[1], "beat": "主角开始用手里的东西换筹码，反派露出破绽", "episodes": acts[1]},
            {"name": act_names[2], "beat": "公开对质，位置互换，最后一镜留钩子", "episodes": acts[2]},
        ],
        "cast": [
            {"name": lead, "role": "主角", "age": ("5 岁半" if "萌" in genre else "28 岁"),
             "traits": "不哭闹，先观察再动手；说话短，句子像钉子",
             "look": "米白色小熊卫衣洗到发白，红色小书包，齐刘海，圆脸大眼，左眉尾一颗小痣",
             "want": "拿回属于自己的名字和位置", "secret": "她记得所有细节，包括谁先动的手"},
            {"name": rival, "role": "对手", "age": "30+",
             "traits": "体面、话密、擅长把规则说成道理",
             "look": "深色三件套西装，无框眼镜，头发梳得服帖，右手常插在口袋里",
             "want": "把主角从牌桌上清出去",
             "secret": "他手里那份文件，签字页是别人代签的"},
            {"name": kin, "role": "摇摆的亲人", "age": "40+",
             "traits": "心软但怕事，习惯性站在强者那边",
             "look": "驼色针织开衫，右手一枚旧玉戒，说话前先摸一下杯子",
             "want": "保住现有的安稳",
             "secret": "她见过真东西，却选择了不说"},
            {"name": helper, "role": "关键助力", "age": "26",
             "traits": "话少，动作快，只在关键节点出现",
             "look": "黑色连帽卫衣，肩头常落着雨，左手拎一只旧帆布包",
             "want": "还一个人情",
             "secret": "他认识照片上的第二个人"},
        ],
        "locations": [
            {"name": "主场", "desc": f"{_place(genre, rng)}，冷光为主，地面有反光"},
            {"name": "对手场", "desc": f"{_place(genre, rng)}，空间大、人少，压迫感来自空"},
            {"name": "中转场", "desc": f"{_place(genre, rng)}，人多、嘈杂，适合藏信息"},
        ],
        "props": [{"name": p[0], "desc": p[1]} for p in props],
        "style": {
            "palette": "青灰 / 暖橙 对冲，肤色不过曝",
            "camera": "35mm 与 85mm 交替；关键台词用近景，情绪句用特写",
            "music": "低音弦 + 环境声，反转处留 1 秒静音",
        },
        "risk": [
            "每集必须在 3 秒内给出画面冲突，不要用台词开场。",
            "单镜不超过 10 秒，长对白必须拆镜。",
            "不许出现真实品牌、真实人名、可识别门牌。",
        ],
    }
    return outline


# ---------------------------------------------------------------- 小说正文

_CLOTH_WORDS = ("卫衣", "外套", "上衣", "衬衫", "衣", "裤", "裙", "鞋", "书包", "帽", "围巾", "背心", "斗篷")


def _look_text(canon):
    """从资产 canon 里只抽「穿什么」，别把年龄/性格整段塞进句子里（此前出过「阿棠把阿棠｜5 岁半…裹紧」这种病句）。"""
    s = (canon or "").strip()
    if "服装与外形：" in s:
        s = s.split("服装与外形：", 1)[1]
    s = s.split("｜")[0].replace("；", "，").replace("。", "，")
    parts = [p.strip() for p in s.split("，") if p.strip()]
    cloth = [p for p in parts if any(w in p for w in _CLOTH_WORDS)]
    return "，".join(cloth[:3])


def local_novel(ctx: dict) -> str:
    project = ctx.get("project") or {}
    outline = ctx.get("outline") or {}
    episode = ctx.get("episode") or {}
    assets = ctx.get("assets") or []
    premise = (project.get("premise") or "").strip()
    genre = project.get("genre") or outline.get("genre") or detect_genre(premise)
    idx = int(episode.get("idx") or 1)
    target_ep = None
    for act in outline.get("acts", []):
        for e in act.get("episodes", []):
            if int(e.get("idx", 0)) == idx:
                target_ep = e
    if not target_ep:
        target_ep = {"title": f"第{idx}集", "hook": "她刚把门关上，门外就有人敲门。",
                     "beats": ["开场：最不利的位置。", "中段：对手把话说满。"],
                     "climax": "她把证据推过去。", "payoff": "镜头停在门口的第二道影子。"}

    rng = rng_of("novel", premise, genre, idx, target_ep.get("title"))
    cast = outline.get("cast") or []
    lead = cast[0]["name"] if cast else "念念"
    rival = cast[1]["name"] if len(cast) > 1 else "周慕"
    kin = cast[2]["name"] if len(cast) > 2 else "苏晚"
    helper = cast[3]["name"] if len(cast) > 3 else "陆知白"
    place = _place(genre, rng)

    char_asset = next((a for a in assets if a.get("kind") == "char" and lead in (a.get("name") or "")), None)
    look = _look_text((char_asset or {}).get("canon")) or "米白色旧卫衣，袖口洗得发白，背一个红色小书包"

    openers = [
        f"{place}的灯只亮了一半。{lead}站在光与暗的分界线上，{look}，像一件被放错地方的旧物。",
        f"雨是斜着下的。{lead}把{look}裹紧，先数了三遍门牌，才抬手敲门。",
        f"那天{place}格外安静，安静到{lead}能听见自己鞋底碾过水的声音。",
    ]
    bodies = [
        f"{rival}没有抬头，只把桌上的东西往前推了半寸。那半寸，就是全部态度。\n"
        f"「按规定，你不该出现在这里。」他说得很慢，像在给一句话上锁。\n"
        f"{lead}没有争。她把背包放到椅子边，拉开拉链，取出一样东西，放在那半寸的空位上。\n"
        f"「那按另一条规定，」她说，「你也不该坐在这把椅子上。」",

        f"{kin}在旁边一直没说话。她的手指在杯壁上敲了两下，停住。\n"
        f"这个动作{lead}见过——上一次见，是在很多年前的一个夜里，家里所有人都以为她睡着了。\n"
        f"「{lead}，」{kin}终于开口，「你先把东西收起来，别在这儿。」\n"
        f"「我不是来吵架的。」{lead}说，「我是来把账算清楚的。」\n"
        f"这句话落地，屋里所有声音都退了一步。",

        f"门外传来第二声车响。\n"
        f"{helper}推门进来，肩上落着雨，手里拎着那个谁都不愿意再碰的旧包。\n"
        f"他没有看{rival}，只把包放在桌上，拉链朝上。\n"
        f"「东西我带来了。」他说，「要不要当着所有人的面打开，你们定。」",

        f"{lead}把最后一份文件摊开，纸边压着桌沿，正好压住{rival}的影子。\n"
        f"上面的签名页有一个很小的破绽：笔锋回勾的方向反了。那不是一个习惯用右手的人写的。\n"
        f"「这份东西，」她抬眼，「不是他签的。」\n"
        f"{rival}的手第一次离开了桌面。",
    ]
    closers = [
        f"这一集到这里就断了。镜头最后停在{lead}的侧脸上，她没有笑，也没有哭。\n"
        f"门外还有第三个人的脚步声，越来越近，却在门槛前停住。\n"
        f"观众不知道那是谁。{lead}知道。",
        f"所有人都以为事情到这里就结束了。\n"
        f"{lead}把椅子推回原位，弯腰捡起地上那张被踩过的纸，折成很小的一块，塞进口袋。\n"
        f"她走出门，雨已经停了。远处有一辆车没熄火。",
        f"结尾是一个空镜：桌上那只杯子还在轻轻震动，说明门刚刚被重重关上。\n"
        f"字幕浮出来一行小字——「名单上还有第三个名字」。",
    ]
    title = target_ep.get("title") or f"第{idx}集"
    hook = target_ep.get("hook") or ""
    beats = target_ep.get("beats") or []

    parts = [f"【第{idx}集 · {title.replace('第' + str(idx) + '集 · ', '')}】", ""]
    parts.append(rng.choice(openers))
    parts.append("")
    if hook:
        parts.append(hook)
        parts.append("")
    for b in beats[:2]:
        parts.append(b)
    parts.append("")
    for b in rng.sample(bodies, 3):
        parts.append(b)
        parts.append("")
    parts.append(target_ep.get("climax") or "")
    parts.append("")
    parts.append(rng.choice(closers))
    return "\n".join(parts).replace("\n\n\n", "\n\n").strip()


# ---------------------------------------------------------------- 剧本

def local_script(ctx: dict) -> dict:
    project = ctx.get("project") or {}
    outline = ctx.get("outline") or {}
    episode = ctx.get("episode") or {}
    novel = (ctx.get("novel") or "").strip()
    premise = project.get("premise") or ""
    genre = project.get("genre") or outline.get("genre") or detect_genre(premise)
    idx = int(episode.get("idx") or 1)
    rng = rng_of("script", genre, idx, novel[:80])
    cast = outline.get("cast") or []
    lead = cast[0]["name"] if cast else "念念"
    rival = cast[1]["name"] if len(cast) > 1 else "周慕"
    kin = cast[2]["name"] if len(cast) > 2 else "苏晚"
    helper = cast[3]["name"] if len(cast) > 3 else "陆知白"
    places = []
    for _ in range(30):
        p = _place(genre, rng)
        if p not in places:
            places.append(p)
        if len(places) >= 3:
            break
    while len(places) < 3:
        places.append(places[-1] if places else "凌晨的便利店")

    scenes = [
        {
            "no": 1, "slug": f"外景 · {places[0]} · 雨", "time": "傍晚", "location": places[0],
            "cast": [lead], "purpose": "3 秒内给出最不利的位置",
            "action": f"{lead}被拦在门外，鞋尖是一小滩水。她抬头看门牌，门内的灯比门外暖。",
            "dialogue": [
                {"sp": lead, "line": "我东西还在里面。", "emotion": "克制"},
                {"sp": "门内声音（画外）", "line": "放门口就行，别进来了。", "emotion": "冷淡"},
            ],
        },
        {
            "no": 2, "slug": f"内景 · {places[1]} · 夜", "time": "夜", "location": places[1],
            "cast": [lead, rival], "purpose": "对手把话说满，观众站到主角这边",
            "action": f"{lead}把背包放在椅子边，取出一样东西，放在桌子正中。{rival}没有抬头。",
            "dialogue": [
                {"sp": rival, "line": "按规定，你不该出现在这里。", "emotion": "体面、慢"},
                {"sp": lead, "line": "那按另一条规定，你也不该坐这把椅子。", "emotion": "平静"},
                {"sp": rival, "line": "你手里那点东西，证明不了什么。", "emotion": "轻蔑"},
                {"sp": lead, "line": "证明不了，就先放着。", "emotion": "更平静"},
            ],
        },
        {
            "no": 3, "slug": f"内景 · {places[2]} · 夜", "time": "夜", "location": places[2],
            "cast": [lead, rival, kin], "purpose": "亲人摇摆，信息量给出",
            "action": f"{kin}的指尖在杯壁上敲了两下，停住。{lead}看着这个动作，没有说话。",
            "dialogue": [
                {"sp": kin, "line": f"{lead}，你先把东西收起来，别在这儿。", "emotion": "心软、怕事"},
                {"sp": lead, "line": "我不是来吵架的，我是来把账算清楚的。", "emotion": "稳"},
                {"sp": kin, "line": "有些账，算清了就回不去了。", "emotion": "低"},
            ],
        },
        {
            "no": 4, "slug": f"内景 · {places[1]} · 夜（接）", "time": "夜", "location": places[1],
            "cast": [lead, rival, helper], "purpose": "助力入场，局势第一次翻面",
            "action": f"门被推开，{helper}拎着旧包进来，包上的雨水滴在地毯上。{rival}第一次把手离开桌面。",
            "dialogue": [
                {"sp": helper, "line": "东西我带来了。要不要当着所有人的面打开，你们定。", "emotion": "冷"},
                {"sp": rival, "line": "你知不知道你在做什么。", "emotion": "压着火"},
                {"sp": lead, "line": "他知道。他不知道的是，里面不止一样东西。", "emotion": "轻"},
            ],
        },
        {
            "no": 5, "slug": f"内景 · {places[1]} · 夜（续）", "time": "夜", "location": places[1],
            "cast": [lead, rival], "purpose": "公开对质，位置互换",
            "action": f"{lead}把签名页推过去，指尖停在笔锋回勾的位置。灯光从侧面压下来，两人影子在桌上交叠。",
            "dialogue": [
                {"sp": lead, "line": "这笔锋是反的。习惯用右手的人，不会这么收笔。", "emotion": "钉住"},
                {"sp": rival, "line": "……你从哪弄来的。", "emotion": "破音"},
                {"sp": lead, "line": "你签的时候，手在抖。", "emotion": "不看他"},
            ],
        },
        {
            "no": 6, "slug": f"外景 · {places[0]} · 夜", "time": "深夜", "location": places[0],
            "cast": [lead], "purpose": "留钩子，下一集入口",
            "action": f"{lead}走出门，雨停了。远处有一辆车没熄火，车灯把她的影子拉得很长。她没有回头。",
            "dialogue": [
                {"sp": lead, "line": "（对着电话）名单上还有第三个名字。", "emotion": "很轻"},
            ],
        },
    ]
    for s in scenes:
        s["duration"] = "8-14 秒" if len(s["dialogue"]) <= 2 else "14-22 秒"
    return {
        "episode": f"第{idx}集",
        "logline": (outline.get("logline") or "")[:60],
        "scenes": scenes,
        "note": "对白按 4.5 字/秒折算时长；单镜不超过 10 秒，长对白必须拆镜。",
    }


# ---------------------------------------------------------------- 分镜

SHOT_SIZES = ["大全景", "全景", "中景", "中近景", "近景", "特写", "大特写"]
MOVES = ["固定", "缓慢推近", "缓慢拉远", "横移跟拍", "侧向跟移", "手持跟随", "轻微升降", "环绕半圈"]
MICRO_BEATS = ["目光钉在对方脸上，手停在半空",
               "说到一半停住，指节在桌沿敲了一下",
               "把袖子往下拽了拽，声音压低半度",
               "侧过半张脸，只看对方的嘴",
               "往前半步，肩膀先到、话后到",
               "把手里东西折进掌心，不让人看见"]


def local_storyboard(ctx: dict) -> list:
    """把剧本场次拆成 4-10 秒的可生成镜头。"""
    project = ctx.get("project") or {}
    script = ctx.get("script") or {}
    assets = ctx.get("assets") or []
    rng = rng_of("storyboard", json.dumps(script, ensure_ascii=False)[:400])
    chars = [a["name"] for a in assets if a.get("kind") == "char"]
    prop_names = [a["name"] for a in assets if a.get("kind") == "prop"]
    scenes = script.get("scenes") or []
    shots = []
    idx = 1
    for sc in scenes:
        loc_asset = next((a for a in assets if a.get("kind") == "scene" and (a.get("name") or "") in (sc.get("location") or "")), None)
        sc_cast = [c for c in sc.get("cast", []) if c in chars] or chars[:2]
        dl = sc.get("dialogue") or []
        # 每个场次：1 个建立镜头 + 每个对白 1 镜 + 1 个反应/空镜
        plan = [("建立", None)]
        for d in dl:
            plan.append(("对白", d))
        plan.append(("反应", dl[-1] if dl else None))

        for kind, d in plan:
            if kind == "建立":
                size, move, dur = "全景", rng.choice(["固定", "轻微升降"]), rng.choice([4, 5, 5])
                action = f"建立 {sc.get('location')}：{sc.get('action', '')[:26]}"
                dial = []
            elif kind == "对白":
                n = len(d.get("line") or "")
                size = "近景" if n <= 12 else "中近景"
                move = rng.choice(["固定", "缓慢推近"])
                dur = max(4, min(10, round(n / 4.5 + 1.8)))
                action = f"{d.get('sp')} 开口前{MICRO_BEATS[idx % len(MICRO_BEATS)]}，台词见对白轨"
                dial = [{"sp": d.get("sp"), "line": d.get("line"), "emotion": d.get("emotion", "")}]
            else:
                size, move, dur = "特写", "缓慢推近", 4
                action = f"反应镜头：把情绪压在脸上，不给台词。{sc.get('purpose', '')}"
                dial = []
            shots.append({
                "idx": idx, "scene_slug": sc.get("slug"), "shot_size": size,
                "camera": f"{sc.get('location')} / {move}", "movement": move, "duration": dur,
                "chars": sc_cast[:3] if kind != "反应" else sc_cast[:1],
                "props": [prop_names[0]] if prop_names and kind == "对白" else [],
                "action": action, "emotion": (d or {}).get("emotion", "") if d else sc.get("purpose", ""),
                "dialogue": dial, "transition": "硬切" if kind != "建立" else "淡入",
            })
            idx += 1
    # 控制总量：单集 12-26 镜
    if len(shots) > 26:
        shots = shots[:26]
    # 对齐单集目标时长：优先砍掉「反应镜头」，其次砍最长的那一镜
    target = _target_seconds(project)
    while len(shots) > 10 and sum(int(s.get("duration") or 5) for s in shots) > target:
        cut = next((i for i, s in enumerate(shots) if str(s.get("action") or "").startswith("反应")), None)
        if cut is None:
            cut = max(range(len(shots)), key=lambda i: int(shots[i].get("duration") or 5))
        shots.pop(cut)
    for i, s in enumerate(shots):
        s["idx"] = i + 1
        # 相邻同景别自动错开，避免跳切
        if i and s["shot_size"] == shots[i - 1]["shot_size"]:
            s["shot_size"] = "近景" if s["shot_size"] in ("中景", "中近景") else "中近景"
    return shots


# ---------------------------------------------------------------- 场面调度

def local_blocking(ctx: dict) -> dict:
    shot = ctx.get("shot") or {}
    project = ctx.get("project") or {}
    assets = ctx.get("assets") or []
    rng = rng_of("blocking", shot.get("action"), shot.get("shot_size"), shot.get("duration"))
    names = _as_list(shot.get("chars"))
    size = shot.get("shot_size") or "中景"
    move = shot.get("movement") or "固定"
    dur = int(shot.get("duration") or 5)
    side = rng.choice(["轴线左侧", "轴线右侧"])
    height = {"大全景": "高机位俯拍 20°", "全景": "平视略俯 8°", "中景": "平视",
              "中近景": "平视", "近景": "平视略仰 5°", "特写": "平视", "大特写": "平视微仰 8°"}.get(size, "平视")
    lens = {"大全景": "24mm", "全景": "28mm", "中景": "35mm", "中近景": "50mm",
            "近景": "85mm", "特写": "100mm", "大特写": "135mm"}.get(size, "50mm")
    primary = names[0] if names else "主角"
    secondary = names[1] if len(names) > 1 else ""
    anchor = {"全景": "画面右侧三分之一，脚底压在下三分线",
              "中景": "画面中央偏右，头顶留 1/3 空间",
              "中近景": "画面中央偏左，肩线略低于画幅中线",
              "近景": "画面右侧，眼睛落在上三分线",
              "特写": "画面正中，眼睛在上三分之一",
              "大特写": "眼睛占画幅左上，留出呼吸空间"}.get(size, "画面中央")
    pos = {"大全景": [0.55, 0.78], "全景": [0.62, 0.76], "中景": [0.55, 0.66],
           "中近景": [0.44, 0.58], "近景": [0.58, 0.45], "特写": [0.5, 0.42], "大特写": [0.38, 0.35]}.get(size, [0.5, 0.6])
    comp = {
        "大全景": "环境优先，人物只占画面 1/6，用来交代空间与孤立感",
        "全景": "人物全身 + 环境线，前景留一道遮挡（门框/桌沿）",
        "中景": "三分法，人物半身，桌沿压在下三分线，形成对质关系",
        "中近景": "中心偏左，留右侧负空间给对手的反应",
        "近景": "过肩构图，前景留对手肩线一小块虚影",
        "特写": "中心构图，眼部对焦，背景压暗，只留一条高光",
        "大特写": "局部特写（眼睛/指尖/纸边），用微距质感和呼吸感",
    }.get(size, "三分法")
    light = {
        "特写": ("侧逆光 45°", "3200K 暖", "压暗背景，只给眼下一道光"),
        "近景": ("侧光 60°", "4200K 中性", "脸部一半在暗，情绪更硬"),
        "全景": ("顶光 + 环境窗光", "5600K 冷", "地面反光拉长影子"),
    }.get(size, ("侧逆光 30°", "5000K 偏冷", "低对比，保留暗部细节"))

    beats = [
        {"t": "0.0-1.0s", "what": f"{primary} 保持在{anchor}，先不动，让观众认出她"},
        {"t": "1.0-2.5s", "what": f"{primary} 有一个小动作（抬眼 / 手指收紧 / 呼气），动作只到一半"},
        {"t": f"2.5-{max(3, dur - 1)}s", "what": f"{move}：{('镜头' + move) if move != '固定' else '机位不动，让人物在画面里走完这段情绪'}"},
        {"t": f"{max(3, dur - 1)}-{dur}s", "what": "留 0.8 秒空拍给观众反应；台词说完不要立刻切"},
    ]
    move_txt = (f"{primary} 从画面右侧偏后起步，向{side}的桌沿靠近半步；" if secondary == "" else
                f"{primary} 固定在{anchor}，{secondary} 从画面左侧前景入画，停在轴线{side}，两人之间留一只手的距离；")
    text = (
        f"{size}，{lens}，{height}，{side}，{move}。{move_txt}"
        f"构图：{comp}。光：主光{light[0]}，色温{light[1]}，{light[2]}。"
        f"节拍：先给静止 1 秒，再给半个动作，最后 0.8 秒空拍收住。转场：{shot.get('transition') or '硬切'}。"
    )
    return {
        "camera": {"height": height, "side": side, "distance": size, "move": move},
        "lens": lens,
        "talent": [
            {"name": primary, "anchor": anchor, "pos": pos, "move": "靠近半步后停住"},
            *([{"name": secondary, "anchor": "左侧前景", "pos": [0.26, 0.62], "move": "入画后不再移动"}] if secondary else []),
        ],
        "comp": comp,
        "light": {"dir": light[0], "temp": light[1], "mood": light[2]},
        "beats": beats,
        "transition": shot.get("transition") or "硬切",
        "text": text,
        "axis": side,
    }


# ---------------------------------------------------------------- 视频提示词

def local_prompt(ctx: dict) -> dict:
    shot = ctx.get("shot") or {}
    project = ctx.get("project") or {}
    assets = ctx.get("assets") or []
    blocking = ctx.get("blocking") or {}
    style = (project.get("style_lock") or "").strip()
    ratio = project.get("ratio") or "9:16"
    dur = int(shot.get("duration") or 5)

    def canon(kind, name):
        for a in assets:
            if a.get("kind") == kind and (a.get("name") or "") == name:
                return (a.get("canon") or a.get("desc") or name)
        for a in assets:
            if a.get("kind") == kind and name and name in (a.get("name") or ""):
                return (a.get("canon") or a.get("desc") or name)
        return name

    # canon 原文进提示词，但控制长度：模型对过长描述会丢重点
    char_lines = "；".join(canon("char", n)[:96] for n in _as_list(shot.get("chars"))) or "主角"
    prop_lines = "；".join(canon("prop", p)[:48] for p in _as_list(shot.get("props"))) or "—"
    scene_line = "—"
    slug = shot.get("scene_slug") or ""
    loc = slug.split("·")[1].strip() if "·" in slug else slug
    scene_line = canon("scene", loc)[:104] if loc else "—"
    cam = blocking.get("camera") or {}
    light = blocking.get("light") or {}
    dialogue = " / ".join(f"{d.get('sp')}：{d.get('line')}" for d in _as_list(shot.get("dialogue")))

    cn = (
        f"【主体】{char_lines}。"
        f"【场景】{scene_line}。"
        f"【道具】{prop_lines}。"
        f"【动作】{shot.get('action') or ''}"
        f"【镜头】{shot.get('shot_size')}，{cam.get('height', '')}，{cam.get('side', '')}，{blocking.get('lens', '')}，{cam.get('move', '')}。"
        f"【构图】{blocking.get('comp', '')}。"
        f"【光线】主光{light.get('dir', '')}，{light.get('temp', '')}，{light.get('mood', '')}。"
        f"【调度】{blocking.get('text', '')}"
        f"【风格】{style or '冷调写实，胶片颗粒轻，肤色自然'}；{ratio} 竖屏；时长 {dur}s；动作连续，不跳帧。"
        f"【对白】{dialogue or '无对白，仅环境声'}"
    )
    en = (
        f"{shot.get('shot_size')} shot, {blocking.get('lens','35mm')} lens, {light.get('dir','side light')}, "
        f"{'locked character design, consistent face and outfit, ' if shot.get('chars') else ''}"
        f"{'consistent location, ' if scene_line != '—' else ''}"
        f"cinematic realistic, subtle film grain, natural skin, {ratio} vertical, {dur}s continuous motion"
    )
    neg = ("画面文字, 水印, logo, 字幕硬字, 多余手指, 变形脸, 换装, 发型突变, 年龄变化, 双胞胎重影, "
           "镜头抖动, 人物穿模, 背景突变, 真实品牌标识, 血腥")
    return {"cn": re.sub(r"\s+", " ", cn).strip(), "en": en, "neg": neg}


def local_prompt_v2(ctx: dict) -> dict:
    """H3 官方「全参考模式」六段式提示词（结构照 MiniMax 官方 VIDEO_PROMPT_WRITING_GUIDE_ref）。

    段落：subject_definitions / summary / retention_analysis / detailed_description /
    overall_soundscape / non_diegetic_music。台词用 <d>[语言] …</d> 并带 (S1) 说话人编号，
    参考图用 <Subject N> + <Picture N> 标签，与 ComfyUI H3 Ref2Video 的用法一致。
    """
    shot = ctx.get("shot") or {}
    project = ctx.get("project") or {}
    assets = ctx.get("assets") or []
    blocking = ctx.get("blocking") or {}
    perf = ctx.get("perf") or {}
    style = (project.get("style_lock") or "").strip()
    ratio = project.get("ratio") or "9:16"
    dur = int(shot.get("duration") or 5)
    lang_tag = {"zh": "Chinese", "yue": "Cantonese", "hak": "Hakka",
                "en": "English", "nan": "Hokkien", "sichuan": "Sichuanese"}.get(
        (project.get("dialogue_lang") or "zh"), "Chinese")

    def find(kind, name):
        for a in assets:
            if a.get("kind") == kind and (a.get("name") or "") == name:
                return a
        for a in assets:
            if a.get("kind") == kind and name and name in (a.get("name") or ""):
                return a
        return None

    # ---- subject_definitions：角色/场景/道具各自一个 Subject，参考图挂 <Picture N>
    subs, pics = [], []
    idx = 0
    used = {}
    for cname in _as_list(shot.get("chars"))[:4]:
        a = find("char", cname)
        idx += 1
        used[cname] = idx
        sub = f"<Subject {idx}> 是「{cname}」"
        if a:
            sub += f"，外形按 {a.get('canon') or ''}"
            pics.append(f"<Picture {len(pics) + 1}> 是「{cname}」的角色参考图（{'四视图' if 'four' in (a.get('image_mode') or '') else '定妆图'}），作为 <Subject {idx}> 的身份锚点。")
        subs.append(sub + "。")
    slug = shot.get("scene_slug") or ""
    loc = slug.split("·")[1].strip() if "·" in slug else slug
    if loc:
        a = find("scene", loc)
        idx += 1
        used["__scene__"] = idx
        subs.append(f"<Subject {idx}> 是场景「{loc}」" + (f"，环境按 {a.get('canon') or ''}" if a else "") + "。")
        if a:
            pics.append(f"<Picture {len(pics) + 1}> 是「{loc}」的场景参考图，作为 <Subject {idx}> 的环境锚点。")
    for pname in _as_list(shot.get("props"))[:3]:
        a = find("prop", pname)
        if not a or used.get("prop:" + pname):
            continue
        idx += 1
        used["prop:" + pname] = idx
        subs.append(f"<Subject {idx}> 是道具「{pname}」，外观按 {a.get('canon') or pname}。")
        pics.append(f"<Picture {len(pics) + 1}> 是「{pname}」的道具参考图，作为 <Subject {idx}> 的外观锚点。")

    dlg = _as_list(shot.get("dialogue")) or _as_list(shot.get("dialogue_json"))
    speakers, sp_ids = [], {}
    for d in dlg:
        sp = (d.get("sp") or "旁白").strip()
        if sp not in sp_ids:
            sp_ids[sp] = "S%d" % (len(sp_ids) + 1)
            speakers.append(sp)
    if not dlg:
        sp_ids = {}

    # ---- summary
    names = "、".join(f"<Subject {used[c]}>" for c in _as_list(shot.get("chars"))[:4] if c in used) or "主体"
    summary = (f"[reference generation] 目标视频共 1 个镜头、{dur} 秒、{ratio}，" + 
               f"以 {names} 为锚点重建「{shot.get('action') or '当前动作'}」，"
               + (f"台词语言为 {lang_tag}。" if dlg else "无对白，只保留环境声。"))

    # ---- retention_analysis
    ret = []
    for c in _as_list(shot.get("chars"))[:4]:
        if c in used:
            ret.append(f"<Subject {used[c]}>（出现在 [Shot 1]）：fully_preserved - 「{c}」的脸型、发型、服装、体态全部保留，不换脸不换装。")
    if loc and "__scene__" in used:
        ret.append(f"<Subject {used['__scene__']}>（出现在 [Shot 1]）：fully_preserved - 「{loc}」的空间结构、材质、陈设与窗光方向保留。")
    for pname in _as_list(shot.get("props"))[:3]:
        if used.get("prop:" + pname):
            ret.append(f"<Subject {used['prop:' + pname]}>（出现在 [Shot 1]）：fully_preserved - 道具「{pname}」的材质、颜色与磨损细节保留。")

    # ---- detailed_description
    cam = blocking.get("camera") or {}
    light = blocking.get("light") or {}
    cam_line = "，".join(x for x in [shot.get("shot_size") or "", cam.get("height") or "",
                                     cam.get("side") or "", blocking.get("lens") or "",
                                     cam.get("move") or ""] if x)
    body = []
    body.append(f"目标视频风格为 {style or '冷调写实，胶片颗粒轻，肤色自然'}，{ratio} 竖屏，24fps。")
    body.append(f"[Shot 1]（0.000–{dur:.3f}s）")
    if cam_line:
        body.append(f"{cam_line} 机位。")
    if blocking.get("comp"):
        body.append(f"构图：{blocking.get('comp')}。")
    if light:
        body.append(f"布光：主光 {light.get('dir','')}，{light.get('temp','')}，{light.get('mood','')}。")
    if blocking.get("text"):
        body.append(f"走位：{blocking.get('text')}")
    if shot.get("action"):
        body.append(f"动作：{shot['action']}。")
    if perf:
        body.append(f"表演：情绪 {perf.get('tone','克制')}、强度 {perf.get('intensity','3')}/5；"
                    f"表情 {perf.get('face','眼神先动、嘴角后动')}；音量 {perf.get('volume','中')}、语速 {perf.get('rate','中')}；"
                    f"眼神与肢体 {perf.get('eye','看向对方')}、{perf.get('gesture','小幅动作')}；口型 {perf.get('lip','与台词逐字对齐')}。")
    for d in dlg:
        sp = (d.get("sp") or "旁白").strip()
        sid = sp_ids.get(sp, "")
        sub = f"<Subject {used[sp]}>（{sid}）" if sp in used else f"声音（{sid}）"
        line = (d.get("line") or "").replace("\n", " ").strip()
        body.append(f"{sub} 说，<d>[{lang_tag}] {line}</d>。")
    if not dlg:
        body.append("本镜无对白，人物呼吸与布料摩擦声保留。")
    detailed = "".join(body)

    sound = ("室内低频环境声与远处街道声持续；对白为同期声，不做后期配音感。" if dlg
             else "只有环境声：室内低频底噪与细微布料摩擦，不加人声。")
    music = "N/A（本片不加非环境背景音乐）"

    text = "\n".join([
        "subject_definitions:", *subs, *pics, "",
        "summary:", summary, "",
        "retention_analysis:", *ret, "",
        "detailed_description:", detailed, "",
        "overall_soundscape:", sound, "",
        "non_diegetic_music:", music,
    ])
    neg = ("画面文字, 水印, logo, 字幕硬字, 多余手指, 变形脸, 换装, 发型突变, 年龄变化, "
           "双胞胎重影, 镜头抖动, 人物穿模, 背景突变, 真实品牌标识, 血腥")
    return {"cn": text, "en": text, "neg": neg}


# ---------------------------------------------------------------- 深度质检 / 优化

def audit_prompt(ctx: dict) -> dict:
    """对提示词做可执行的规则体检，返回问题清单与修复后的版本。"""
    shot = ctx.get("shot") or {}
    prompt = ctx.get("prompt") or {}
    blocking = ctx.get("blocking") or {}
    assets = ctx.get("assets") or []
    issues = []
    cn = prompt.get("cn") or ""
    neg = prompt.get("neg") or ""
    dur = int(shot.get("duration") or 5)
    dlg = shot.get("dialogue") or []
    chars_in_dlg = sum(len(d.get("line") or "") for d in dlg)

    # 1. 语速与时长
    if chars_in_dlg and chars_in_dlg / max(1, dur) > 5.2:
        need = round(chars_in_dlg / 4.5 + 1.2)
        issues.append({"level": "warn", "code": "PACE",
                       "msg": f"对白 {chars_in_dlg} 字 / {dur}s，语速超过 5.2 字每秒，观众听不完",
                       "fix": f"建议时长改为 {need}s，或把台词拆到下一镜"})
        shot["duration"] = min(10, max(dur, need))
        dur = shot["duration"]

    # 2. 单镜过长
    if dur > 10:
        issues.append({"level": "error", "code": "LEN", "msg": f"单镜 {dur}s 超过 10s 上限",
                       "fix": "已压到 10s，长镜头请在分镜层拆开"})
        shot["duration"] = 10

    # 3. 角色锁定缺失
    missing = []
    for n in shot.get("chars") or []:
        a = next((x for x in assets if x.get("kind") == "char" and n in (x.get("name") or "")), None)
        if not a or not (a.get("canon") or "").strip():
            missing.append(n)
    if missing:
        issues.append({"level": "error", "code": "LOCK", "msg": f"角色 {', '.join(missing)} 没有锁定特征",
                       "fix": "已在资产库生成 canon 描述，请在资产面板确认后锁定"})

    # 4. 负面提示
    for must in ["水印", "文字", "变形"]:
        if must not in neg:
            issues.append({"level": "warn", "code": "NEG", "msg": f"负面提示缺少「{must}」", "fix": "已补入通用负面词"})
            neg = (neg + ", " + must).strip(", ")

    # 5. 调度缺失
    if not (blocking.get("text") or "").strip():
        issues.append({"level": "error", "code": "BLOCK", "msg": "提示词里没有调度信息，生成的走位会失控",
                       "fix": "请先执行 ② 场面调度"})

    # 6. 提示词长度
    if len(cn) > 520:
        issues.append({"level": "warn", "code": "LONG", "msg": f"提示词 {len(cn)} 字，模型会稀释重点",
                       "fix": "已保留主体/场景/动作/镜头/光/风格六段，删掉重复形容"})
        cn = cn[:518].rstrip("，。、；") + "。"

    # 7. H3 六段式结构质检（仅 h3ref 格式生效；对照 MiniMax H3 Ref2VA 导演规则）
    if "subject_definitions:" in cn:
        SEG_ORDER = ["subject_definitions:", "summary:", "retention_analysis:",
                     "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]
        pos = []
        for seg in SEG_ORDER:
            m = re.search(r"^" + re.escape(seg), cn, re.M)
            pos.append(m.start() if m else -1)
        missing = [SEG_ORDER[i] for i, p in enumerate(pos) if p < 0]
        if missing:
            issues.append({"level": "error", "code": "H3_SEG",
                           "msg": "六段式缺段：" + "、".join(missing),
                           "fix": "按 subject_definitions/summary/retention_analysis/detailed_description/overall_soundscape/non_diegetic_music 补齐"})
        present = [p for p in pos if p >= 0]
        if present != sorted(present):
            issues.append({"level": "error", "code": "H3_ORDER",
                           "msg": "六段标题顺序错乱", "fix": "按固定顺序重排六段"})

        m_def = re.search(r"^subject_definitions:\s*([\s\S]*?)(?=^summary:)", cn, re.M)
        head = m_def.group(1) if m_def else ""
        defined = set(re.findall(r"<(?:Subject|Picture|Video|Audio) \d+>", head))
        referenced = set(re.findall(r"<(?:Subject|Picture|Video|Audio) \d+>", cn))
        undefined = referenced - defined
        if undefined:
            issues.append({"level": "warn", "code": "H3_REF",
                           "msg": "正文引用了未在定义段出现的素材：" + "、".join(sorted(undefined)),
                           "fix": "在 subject_definitions 补定义，或删掉未定义引用"})

        sx = sorted({int(x) for x in re.findall(r"\(S(\d+)\)", cn)})
        if sx and sx != list(range(1, len(sx) + 1)):
            issues.append({"level": "warn", "code": "H3_S",
                           "msg": f"说话人编号不连续：{sx}", "fix": "按首现顺序从 (S1) 重新编号"})

        for d in re.findall(r"<d>([^<]*)</d>", cn):
            if not re.match(r"\s*\[(Chinese|English|Japanese|Cantonese|Hakka|Hokkien|Sichuanese|Mandarin singing)", d):
                issues.append({"level": "warn", "code": "H3_DLG",
                               "msg": "对白缺少语言标签 <d>[语言] …</d>",
                               "fix": "在 <d> 内补 [Chinese] 等口语语言标签"})

        for bad in ["@CHR", "@SCN", "@SUB", "七段"]:
            if bad in cn:
                issues.append({"level": "warn", "code": "H3_LEGACY",
                               "msg": f"正文残留旧格式标记 {bad}", "fix": "删除旧栏名/@锚点，只保留 H3 六段"})

        def _seg_body(name: str) -> str:
            m = re.search(r"^" + re.escape(name) + r"\s*([\s\S]*?)(?=^[a-z_]+:|\Z)", cn, re.M)
            return m.group(1) if m else ""

        if "BGM" in _seg_body("overall_soundscape:") or "配乐" in _seg_body("overall_soundscape:"):
            issues.append({"level": "warn", "code": "H3_SOUND",
                           "msg": "环境音段混入了配乐", "fix": "配乐移到 non_diegetic_music 段"})
        if "<d>" in _seg_body("non_diegetic_music:"):
            issues.append({"level": "warn", "code": "H3_MUSIC",
                           "msg": "配乐段复述了台词", "fix": "non_diegetic_music 只写乐器/强弱/进出点，不复述台词"})

    fixed = {"cn": cn, "en": prompt.get("en") or "", "neg": neg}
    return {"issues": issues, "prompt": fixed, "shot_patch": {"duration": shot.get("duration")}}


def audit_storyboard(shots: list, assets: list) -> dict:
    """分镜层体检：连续性、景别重复、角色锁、时长。"""
    issues = []
    for i, s in enumerate(shots):
        dur = int(s.get("duration") or 5)
        if dur < 3 or dur > 10:
            issues.append({"level": "warn", "code": "DUR", "msg": f"第{s.get('idx')}镜时长 {dur}s 不合理",
                           "fix": "已归一到 4-10s"})
            s["duration"] = max(4, min(10, dur))
        if not s.get("chars"):
            issues.append({"level": "warn", "code": "EMPTY_CAST", "msg": f"第{s.get('idx')}镜没有角色",
                           "fix": "空镜可保留，但建议补一个道具主体"})
        if i and s.get("shot_size") == shots[i - 1].get("shot_size") and s.get("scene_slug") == shots[i - 1].get("scene_slug"):
            issues.append({"level": "info", "code": "JUMPCUT",
                           "msg": f"第{s.get('idx')}镜与上一镜同景别同场景，容易跳切",
                           "fix": f"已把景别改为 {'近景' if s.get('shot_size') in ('中景', '中近景') else '中近景'}"})
            s["shot_size"] = "近景" if s.get("shot_size") in ("中景", "中近景") else "中近景"
    total = sum(int(s.get("duration") or 5) for s in shots)
    return {"issues": issues, "shots": shots, "total_duration": total}


def audit_script(script: dict) -> dict:
    issues = []
    for sc in script.get("scenes", []):
        for d in sc.get("dialogue", []):
            if len(d.get("line") or "") > 26:
                issues.append({"level": "info", "code": "LINE",
                               "msg": f"{sc.get('slug')} 台词 {len(d['line'])} 字偏长，单镜装不下",
                               "fix": "建议在分镜层拆成两镜"})
    if not script.get("scenes"):
        issues.append({"level": "error", "code": "EMPTY", "msg": "剧本没有场次", "fix": "请先执行剧本生成"})
    return {"issues": issues, "script": script}


def audit_outline(outline: dict) -> dict:
    issues = []
    eps = sum(len(a.get("episodes", [])) for a in outline.get("acts", []))
    if eps < 6:
        issues.append({"level": "error", "code": "EPS", "msg": "集数不足，短剧起不到钩子节奏",
                       "fix": "已建议补到 12 集"})
    for a in outline.get("acts", []):
        for e in a.get("episodes", []):
            if not (e.get("hook") or "").strip():
                issues.append({"level": "warn", "code": "HOOK", "msg": f"{e.get('title')} 缺开场钩子",
                               "fix": "已补默认钩子"})
                e["hook"] = "第一秒就给冲突：门在她面前被关上了。"
    return {"issues": issues, "outline": outline}
