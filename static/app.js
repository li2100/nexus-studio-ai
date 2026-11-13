/* NEXUS STUDIO · 前端工作台（无构建，原生 JS） */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
/* PRD 十三 1：文本默认只读预览，点【编辑】才切输入框，【保存】才写回 */
function roText(value, field, id, opts) {
  const o = opts || {};
  const v = value == null ? '' : String(value);
  return `<div class="robox">
    <div class="rotxt${o.cls ? ' ' + o.cls : ''}">${v ? esc(v).replace(/\n/g, '<br>') : '<i>空</i>'}</div>
    <textarea class="roinput" data-field="${field}" data-id="${id}"${o.rows ? ` rows="${o.rows}"` : ''}>${esc(v)}</textarea>
    <div class="roacts"><button class="btn sm ghost" data-act="roEdit">编辑</button></div>
  </div>`;
}

const parse = (s, d) => { try { return s ? JSON.parse(s) : d; } catch (e) { return d; } };

const S = {
  state: null, pid: null, view: 0, editing: null, dirty: 0, eid: null,
  rev: '', lastSync: null, consoleTab: 'jobs', consoleBig: false,
  reel: null, reelIdx: 0, playing: false, reelTimer: null, reelLoading: false,
  jobsOpen: {}, chatOpen: {}, styles: null, styleQuery: '', libQuery: '', libKind: '',
  perfOpen: {},
  wizStep: 1, wizSrt: '', wizBusy: '',
  set: null, setTab: 'text', chatShot: 0, chatMsgs: {}, chatProposal: null,
  projNew: false, musicForm: null, playIdx: 0, musicFilter: 'all',
};

/* ------------------------------------------------ API */
async function api(path, { method = 'GET', body } = {}) {
  const opt = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

async function ensureSettings(force) {
  if (S.set && !force) return S.set;
  try {
    const r = await api('/api/settings');
    S.set = r.settings; S.srv = r.server;
  } catch (e) { toast('读取配置失败：' + e.message); }
  return S.set;
}

async function ensureChat(sid) {
  if (S.chatMsgs[sid]) return;
  try {
    const r = await api('/api/shots/' + sid + '/chat');
    S.chatMsgs[sid] = (r.messages || []).map((m) => ({ role: m.role, text: m.text }));
  } catch (e) { S.chatMsgs[sid] = []; }
}

function revOf(st) {
  if (!st || !st.project) return '0';
  const sh = st.shots.map((x) => [x.id, x.updated_at, x.status, x.duration, x.shot_size,
    (x.action || '').length, (x.blocking_json || '').length, (x.prompt_cn || '').length, x.locked].join(':')).join('|');
  const ep = st.episodes.map((x) => [x.updated_at, (x.novel_text || '').length, (x.script_json || '').length,
    (x.outline_json || '').length].join(':')).join('|');
  const jb = st.jobs.map((x) => [x.id, x.status, x.progress, x.updated_at].join(':')).join('|');
  return [st.project.updated_at, st.project.target_locked, st.project.premise, ep, sh, jb].join('#');
}

/* ------------------------------------------------ 启动 */
document.addEventListener('DOMContentLoaded', boot);

async function boot() {
  S.pid = Number(localStorage.getItem('nexus.pid') || 0) || null;
  const raw = localStorage.getItem('nexus.view') || '0';
  if (raw === 'wiz') { S.view = 'wiz'; S.wizStep = Number(localStorage.getItem('nexus_wiz_step')) || 1; }
  else { const v0 = Number(raw); S.view = Number.isFinite(v0) ? v0 : 0; }
  S.eid = Number(localStorage.getItem('nexus.eid') || 0) || null;
  bindEvents();
  await refresh(true);
  if (!S.styles) api('/api/styles').then((r) => { S.styles = r.groups; renderWork(); }).catch(() => {});
  setInterval(tick, 2500);
  setTimeout(() => { if (S.state && S.state.project) toast('已恢复上次进度：' + (S.state.steps[S.view] || '上次步骤') + ' · ' + (S.state.project.title || '')); }, 600);
  setTimeout(() => { if (localStorage.getItem('nexus.guide') !== '1') showGuide(false); }, 400);
  setTimeout(autoCheckUpdate, 900);
}

async function autoCheckUpdate() {
  try {
    const r = await api('/api/update_check');
    if (r && r.available) {
      if (localStorage.getItem('nexus.updateDismissed') !== r.latest) showUpdateModal(r);
    }
  } catch (e) { /* 离线/检查失败不打扰 */ }
}

async function refresh(force = false) {
  try {
    const st = await api('/api/state' + (S.pid ? '?project=' + S.pid : ''));
    const changed = revOf(st) !== S.rev;
    S.state = st;
    if (st.project) S.pid = st.project.id;
    S.lastSync = new Date();
    syncState('ok');
    if (changed || force) {
      S.rev = revOf(st);
      renderAll();
    } else {
      renderStatus();
    }
  } catch (e) {
    syncState('bad', e.message);
  }
}

async function tick() {
  if (document.hidden) return;
  if (S.editing || S.dirty > 0) { renderStatus(); return; }
  try {
    const st = await api('/api/state' + (S.pid ? '?project=' + S.pid : ''));
    const changed = revOf(st) !== S.rev;
    S.state = st;
    S.lastSync = new Date();
    syncState('ok');
    if (changed) { S.rev = revOf(st); renderAll(); }
    else renderStatus();
  } catch (e) { syncState('bad', e.message); }
}

function syncState(kind, msg) {
  const el = $('#sync');
  if (!el) return;
  el.className = 'sync' + (kind === 'busy' ? ' busy' : kind === 'bad' ? ' bad' : '');
  const t = S.lastSync ? S.lastSync.toTimeString().slice(0, 8) : '—';
  const txt = kind === 'busy' ? '保存中…' : kind === 'bad' ? ('连接失败 · ' + (msg || '')) :
    (S.dirty > 0 ? '未保存 ' + S.dirty : '数据已同步 · ' + t);
  el.innerHTML = '<i></i><span>' + esc(txt) + '</span>';
}

/* ------------------------------------------------ 渲染入口 */
function renderAll() {
  document.body.classList.toggle('wizmode', S.view === 'wiz');
  renderTop();
  renderRail();
  renderWork();
  renderAssets();
  renderConsole();
  renderStatus();
  const ls = $('#langSel');
  if (ls && S.state && S.state.project) ls.value = S.state.project.dialogue_lang || 'zh';
}

function renderStatus() {
  const st = S.state;
  if (!st || !st.project) return;
  const chips = $('#chips');
  const p = st.project, sv = st.server, stats = st.stats || {};
  const llmCls = sv.llm.ok ? 'ok' : 'warn';
  const cmfCls = sv.comfy.ok ? 'ok' : 'bad';
  chips.innerHTML = `
    <div class="chip ${p.target_locked ? 'ok' : 'warn'}"><span class="dot"></span>目标 <b>${p.target_locked ? '已锁定' : '未锁定'}</b></div>
    <div class="chip ${llmCls}"><span class="dot"></span><b>${sv.llm.ok ? '模型在线' : '本地引擎'}</b> · ${esc((sv.llm.ok ? S.state.config.llm_model : sv.llm.msg).slice(0, 26))}</div>
    <div class="chip ${cmfCls}"><span class="dot"></span>ComfyUI <b>${sv.comfy.ok ? '在线' : '离线'}</b>${sv.comfy.ok ? ' · ' + esc(sv.comfy.version) : ''}</div>
    <div class="chip"><span class="dot"></span>镜头 <b>${stats.shots || 0}</b> · 已生成 <b>${stats.generated || 0}</b> · 待重跑 <b>${(stats.need_block || 0) + (stats.need_prompt || 0)}</b></div>`;
  $('#sync') && syncState('ok');
}

function renderTop() {
  const st = S.state;
  const sel = $('#projSel');
  if (!st) return;
  sel.innerHTML = (st.projects || []).map((p) =>
    `<option value="${p.id}" ${p.id === (st.project && st.project.id) ? 'selected' : ''}>${esc(p.title)}</option>`).join('');
  renderEpBar();
  renderStatus();
}

/* 每一集一个分组：横向切换，切到哪集，下面 02-08 全部只看这一集 */
function renderEpBar() {
  const bar = $('#epbar');
  if (!bar) return;
  const st = S.state;
  if (!st || !st.project) { bar.innerHTML = ''; return; }
  const cur = ep0();
  const eps = (st.episodes || []).filter((e) => !Number(e.archived));
  const plan = Number(st.project.episode_count || 0);
  const items = eps.map((e) => {
    const es = (st.ep_state || []).find((x) => x.id === e.id) || {};
    const hasNovel = (e.novel_text || '').length > 0;
    const shots = es.shots || 0;
    const label = es.status_label || (shots ? (es.generated >= shots ? '画面已生成' : '制作中') : (hasNovel ? '制作中' : '待制作'));
    return `<div class="epchip ${cur && cur.id === e.id ? 'on' : ''}" data-act="pickEp" data-id="${e.id}">
      <b>EP${String(e.idx).padStart(2, '0')}</b>
      <span class="t">${esc((e.title || '').slice(0, 10) || '未写')}</span>
      <span class="d ${shots ? (es.generated >= shots ? 'ok' : 'mid') : (hasNovel ? 'mid' : '')}">${label}${shots ? ' · ' + es.generated + '/' + shots + '镜' : ''}</span>
    </div>`;
  }).join('');
  bar.innerHTML = `<div class="epbar-in">
    <span class="lbl">分集</span>${items}
    ${eps.length < Math.min(plan, 60) ? `<button class="btn sm" data-act="syncEps">按大纲补齐 ${plan} 集</button>` : ''}
    <button class="btn sm ghost" data-act="dlNovelAll" data-all="1">下载小说全文</button>
  </div>`;
}

/* ------------------------------------------------ 左栏：主导航 + 项目内流水线 */
function renderRail() {
  const st = S.state;
  const navItems = [
    [10, '项目管理', '多项目隔离 / 切换'],
    [9, '资产库', '角色 · 场景 · 道具'],
    [11, '音乐创作', '本地 ComfyUI 生成'],
    [12, '音乐播放', '试听 / 关联成片'],
    [13, '系统设置', '接口 · 默认 · 更新'],
  ];
  const nav = navItems.map(([i, name, sub]) => `
    <div class="step mainnav ${S.view === i ? 'on' : ''}" data-act="goMain" data-view="${i}">
      <div class="no">${i === 10 ? 'P' : i === 9 ? 'A' : i === 11 ? 'M' : i === 12 ? '♪' : '⚙'}</div>
      <div><div class="lbl">${name}</div><div class="sub">${sub}</div></div>
    </div>`).join('');

  if (!st || !st.project) {
    $('#rail').innerHTML = `<h4>WORKSPACE</h4>${nav}
      <div class="prog"><div class="big">0<small>% 成片</small></div>
      <div class="row"><span>还没有项目</span><span>先去项目管理建一个</span></div></div>`;
    return;
  }
  const p = st.project, e = (st.ep_state || [])[0] || {}, stats = st.stats;
  const epCount = st.episodes.length;
  const facts = [];
  const put = (i, name, flag, sub) => facts.push(
    `<div class="step ${S.view === i ? 'on' : ''}" data-act="goStep" data-step="${i}">
      <div class="no">${String(i).padStart(2, '0')}</div>
      <div><div class="lbl">${name}</div><div class="sub">${sub || ''}</div></div>
      ${flag ? `<div class="flag ${flag.c}">${flag.t}</div>` : ''}
    </div>`);

  put(0, '目标锁定', p.target_locked ? { t: 'LOCK', c: 'ok' } : { t: '必做', c: '' }, p.genre || '待设定');
  put(1, '小说大纲', !st.episodes[0].outline_json ? { t: '待生成', c: '' } :
    (e.outline_stale ? { t: '需重跑', c: '' } : { t: '已完成', c: 'ok' }),
    ((st.episodes[0].outline_json || '').length ? '三幕 ' + (parse(st.episodes[0].outline_json, {}).acts || []).length + ' 段' : '先写结构'));
  put(2, '小说正文', !st.episodes[0].novel_text ? { t: '待生成', c: '' } :
    (e.novel_stale ? { t: '需重跑', c: '' } : { t: (st.episodes[0].novel_text || '').length + '字', c: 'ok' }), '记忆点与钩子');
  put(3, '剧本生成', !st.episodes[0].script_json ? { t: '待生成', c: '' } :
    (e.script_stale ? { t: '需重跑', c: '' } : { t: (parse(st.episodes[0].script_json, {}).scenes || []).length + '场', c: 'ok' }), '场次 / 动作 / 对白');
  put(4, '分镜设计', !e.shots ? { t: '待生成', c: '' } :
    (e.board_stale ? { t: '需重跑', c: '' } : { t: e.shots + '镜', c: 'ok' }), '4-10 秒可生成');
  put(5, '场面调度', e.need_block ? { t: '待重跑 ' + e.need_block, c: '' } : (e.shots ? { t: '已调度', c: 'ok' } : null), '机位 / 走位 / 光');
  put(6, '视频提示词', e.need_prompt ? { t: '待重跑 ' + e.need_prompt, c: '' } : (e.shots ? { t: '已生成', c: 'ok' } : null), '锁定资产注入');
  put(7, '生成视频', e.need_gen ? { t: '待重跑 ' + e.need_gen, c: '' } : (e.shots ? { t: '已就绪', c: 'ok' } : null), 'ComfyUI / 预演');
  put(8, '成片合集', { t: (e.generated || 0) + '/' + (e.shots || 0), c: (e.generated === e.shots && e.shots) ? 'ok' : '' }, '自动串片');

  const pct = stats.shots ? Math.round(stats.generated / stats.shots * 100) : 0;
  const wfNow = (st.wf || []).find((w) => w.key === (p.video_wf || 'director')) || {};
  $('#rail').innerHTML = `
    <h4>WORKSPACE</h4>
    ${nav}
    <h4>PIPELINE / ${epCount} 集已建</h4>
    ${facts.join('')}
    <div class="prog">
      <div class="big">${pct}<small>% 成片</small></div>
      <div class="mini"><i style="width:${pct}%"></i></div>
      <div class="row"><span>已生成 ${stats.generated}/${stats.shots}</span><span>${stats.duration}s</span></div>
      <div class="row"><span>锁定镜头</span><span>${stats.locked}</span></div>
      <div class="row"><span>本集工作流</span><span>${esc((wfNow.name || '导演台').slice(0, 8))}</span></div>
    </div>
    <div class="legend">
      链路：① 资产/分镜/对白 → ② 调度 → ③ 提示词 → ④ 生成<br>
      改动 <span>①</span> 会让 ②③④ 立刻标记需重跑，不会悄悄沿用旧结果。
    </div>`;
}

/* ------------------------------------------------ 视图头部 */
function vhead(no, title, sub, actions) {
  return `<div class="viewhead">
    <div><div class="vh-t">${esc(no)} ${esc(title)} <small>${esc((S.state.steps[S.view] || '').toUpperCase())}</small></div>
    <div class="vh-d">${sub}</div></div>
    <div class="vh-a">${actions || ''}</div>
  </div>`;
}

const ep0 = () => {
  const eps = (S.state && S.state.episodes) || [];
  return eps.find((e) => e.id === S.eid) || eps[0] || null;
};
const proj = () => (S.state && S.state.project) || null;

/* 视觉风格库：30 种，按 写实 / 2D / 3D / 东方 / 幻想 / 绘画 / 风格化 分组，可搜索、可改词 */
const STYLE_FALLBACK = [
  ['写实', '真人写实', '实拍电影质感，真实皮肤纹理与毛孔，35mm 胶片色彩，自然光比'],
  ['写实', '电影感', '电影级宽银幕质感，高动态范围，浅景深，冷暖对撞的布光，轻微颗粒'],
  ['写实', '纪实摄影', '手持纪实感，现场光，低饱和，轻微晃动，真实环境声'],
  ['2D 动画', '日系动漫', '日式赛璐璐上色，清晰线稿，柔和渐变天空，大眼睛高光，2D 平涂'],
  ['2D 动画', '卡通', '粗描边卡通，明快纯色，夸张表情与弹性动作，简笔背景'],
  ['2D 动画', '暗黑漫画风格', '高反差黑白漫画，粗墨线，网点阴影，压迫构图，克制的红点缀'],
  ['2D 动画', '国漫条漫', '现代国漫条漫风，细线稿 + 赛璐璐，暖色滤镜，都市感'],
  ['2D 动画', '皮影戏插画风格', '皮影剪影质感，镂空纹理，暖黄幕布光，横向叙事构图'],
  ['3D 动画', '3D 国风动漫', '三维国风角色，布料与发丝物理质感，柔光棚拍，浅景深，青绿山水背景'],
  ['3D 动画', '3D 卡通渲染', '皮克斯式三维卡通，圆润造型，大面积柔光，饱和但不刺眼'],
  ['3D 动画', '3D 写实渲染', '影视级三维写实，PBR 材质，全局光照，次表面散射皮肤'],
  ['3D 动画', '黏土定格风', '黏土定格质感，手捏肌理，微缩布景，暖棚灯'],
  ['东方美学', '古风', '东方古典美学，绢本设色，留白构图，烛光与窗棂投影，衣袂飘逸'],
  ['东方美学', '神话仙侠', '仙侠玄幻，灵气粒子，飘带与云海，冷青 + 金高光，体积光穿透'],
  ['东方美学', '中国神话风格', '上古神话气质，青铜与朱砂配色，夸张神性造型，烟雾缭绕，仰视威压构图'],
  ['东方美学', '武侠水墨', '水墨武侠，墨色晕染，飞白笔触，雨雾竹林，动势留痕'],
  ['幻想', '奇幻', '西式奇幻，魔法辉光，繁复纹理服饰，丁达尔光束，史诗构图'],
  ['幻想', '赛博朋克', '霓虹雨夜，青紫与洋红对撞，体积雾，反射湿地，机械义体细节'],
  ['幻想', '硬科幻', '冷白工业光，金属与玻璃材质，全息界面，精确几何构图'],
  ['幻想', '蒸汽朋克', '黄铜齿轮，蒸汽与煤烟，暖橙光源，繁复机械结构'],
  ['绘画', '油画', '古典油画笔触，厚涂肌理，伦勃朗式明暗，暖褐调'],
  ['绘画', '水彩', '透明水彩，湿边晕染，纸纹颗粒，清淡高调'],
  ['绘画', '素描', '铅笔素描，排线结构，灰阶层次，纸面质感'],
  ['绘画', '浮世绘', '浮世绘版画，平涂色块，粗轮廓线，波纹与云纹装饰'],
  ['风格化', '像素风', '16-bit 像素美术，有限色板，硬边阴影，网格对齐'],
  ['风格化', '低多边形', '低多边形三维，块面切分，单色渐变，极简光影'],
  ['风格化', '蒸汽波', '粉紫渐变，网格地平线，复古扫描线，故障色散'],
  ['风格化', '扁平插画', '扁平矢量插画，无描边，几何概括，柔和阴影'],
  ['风格化', '胶片颗粒', '高感光胶片，明显颗粒，色彩偏移，漏光边缘'],
  ['风格化', '黑白黑色电影', '黑白高反差，硬光百叶窗阴影，低机位，雾气质感'],
];

function styleCards() {
  const groups = S.styles || null;
  if (groups && groups.length) return groups.map((g) => ({ name: g.name, styles: g.styles.map((s) => ({ n: s.n, t: s.t })) }));
  const map = {};
  STYLE_FALLBACK.forEach(([g, n, t]) => { (map[g] = map[g] || []).push({ n, t }); });
  return Object.keys(map).map((k) => ({ name: k, styles: map[k] }));
}

function stylePicker(p) {
  const q = (S.styleQuery || '').trim();
  const cur = (p.style_lock || '').trim();
  const groups = styleCards().map((g) => ({
    name: g.name,
    styles: g.styles.filter((s) => !q || (s.n + s.t).includes(q)),
  })).filter((g) => g.styles.length);
  return `<div class="field"><label>视觉风格（点一张卡 = 直接写入风格锁；也可以自己改下面的文字）</label>
    <div class="styletool">
      <input id="styleSearch" placeholder="搜风格：写实 / 动漫 / 3D / 仙侠 / 赛博 / 油画 …" value="${esc(S.styleQuery)}">
      <span class="tiny">${groups.reduce((n, g) => n + g.styles.length, 0)} 种</span>
    </div>
    <div class="stylegrid">
      ${groups.map((g) => `<div style="grid-column:1/-1" class="stylecat">${esc(g.name)}</div>` +
        g.styles.map((s) => `<div class="stylecard ${cur && cur.startsWith(s.n) ? 'on' : ''}"
          data-act="pickStyle" data-text="${esc(s.n + '：' + s.t)}"><b>${esc(s.n)}</b><span>${esc(s.t)}</span></div>`).join('')).join('')}
      ${groups.length ? '' : '<div class="tiny" style="padding:10px">没有匹配的风格，直接在下面写你自己的风格锁。</div>'}
    </div>
  </div>`;
}


function viewTarget() {
  const p = proj();
  if (!p) return emptyView();
  const risks = [];
  if (!p.premise) risks.push('没有一句话创意，模型只能空转');
  if (Number(p.episode_count) < 6) risks.push('集数少于 6 集，钩子节奏撑不住');
  if (!p.style_lock) risks.push('风格锁为空，各镜头画面会漂移');
  if (!p.forbidden) risks.push('没有禁区，模型可能越界');
  if (!risks.length) risks.push('目标字段齐全，可以锁定');
  return vhead('00', '目标锁定', '把题材、平台、画幅、集数、风格和禁区一次写死。锁死之后，后面每一步的产出都会被拿来跟这张卡比对；改这张卡 = 全链路重跑。',
    `<button class="btn" data-act="generate" data-kind="outline">重新生成大纲</button>`) +
    `<div class="grid2">
      <div class="card">
        <h3>创作目标 / TARGET BRIEF</h3>
        <div class="field"><label>项目标题</label><input data-field="project.title" data-id="${p.id}" value="${esc(p.title)}"></div>
        <div class="field"><label>项目描述（一句话创意 / 小说内核）</label>
          <textarea data-field="project.premise" data-id="${p.id}" style="min-height:88px">${esc(p.premise)}</textarea></div>
        <div class="frow">
          <div class="field"><label>题材</label><input data-field="project.genre" data-id="${p.id}" value="${esc(p.genre)}"></div>
          <div class="field"><label>平台</label><input data-field="project.platform" data-id="${p.id}" value="${esc(p.platform)}"></div>
        </div>
        <div class="frow">
          <div class="field"><label>受众</label><input data-field="project.audience" data-id="${p.id}" value="${esc(p.audience)}"></div>
          <div class="field"><label>画面尺寸</label>
            <select data-field="project.ratio" data-id="${p.id}">
              ${['9:16', '16:9', '1:1', '4:3', '3:4', '21:9'].map((r) => `<option ${p.ratio === r ? 'selected' : ''}>${r}</option>`).join('')}
            </select></div>
        </div>
        <div class="frow">
          <div class="field"><label>规划总集数</label>
            <div class="inrow"><input type="number" min="1" max="300" data-field="project.episode_count" data-id="${p.id}" value="${p.episode_count}">
            <button class="btn sm ghost" data-act="setEps" data-n="6">6</button>
            <button class="btn sm ghost" data-act="setEps" data-n="12">12</button>
            <button class="btn sm ghost" data-act="setEps" data-n="24">24</button></div></div>
          <div class="field"><label>单集时长区间</label><input data-field="project.duration_range" data-id="${p.id}" value="${esc(p.duration_range || p.episode_length || '75–95s')}"></div>
        </div>
        <div class="frow">
          <div class="field"><label>帧率</label>
            <select data-field="project.fps" data-id="${p.id}">
              ${[24, 25, 30, 60].map((f) => `<option value="${f}" ${Number(p.fps) === f ? 'selected' : ''}>${f} fps</option>`).join('')}
            </select></div>
          <div class="field"><label>画质等级</label>
            <select data-field="project.quality" data-id="${p.id}">
              ${['预览', '标准', '高清', '电影级'].map((q) => `<option ${p.quality === q ? 'selected' : ''}>${q}</option>`).join('')}
            </select></div>
        </div>
        <div class="frow">
          <div class="field"><label>避雷词模板</label>
            <select data-field="project.avoid_tpl" data-id="${p.id}">
              ${['标准版', '平台合规版', '道德安全版', '自定义'].map((q) => `<option ${p.avoid_tpl === q ? 'selected' : ''}>${q}</option>`).join('')}
            </select></div>
          <div class="field"><label>默认视频工作流（本集所有镜头继承）</label>
            <select data-field="project.video_wf" data-id="${p.id}">
              ${((S.state && S.state.wf) || []).map((w) => `<option value="${w.key}" ${p.video_wf === w.key ? 'selected' : ''}>${esc(w.name)}</option>`).join('')}
            </select></div>
        </div>
        <div class="field"><label>风格锁（每一镜都会带上这句）</label>
          <textarea data-field="project.style_lock" data-id="${p.id}">${esc(p.style_lock)}</textarea></div>
        ${stylePicker(p)}
        <div class="field"><label>禁区（负面约束）</label>
          <textarea data-field="project.forbidden" data-id="${p.id}">${esc(p.forbidden)}</textarea></div>
      </div>
      <div class="card lockcard">
        <h3>目标卡 / LOCKED SPEC</h3>
        <div class="lockstate ${p.target_locked ? '' : 'unlocked'}">
          <span class="dot ${p.target_locked ? 'ok' : 'warn'}"></span>
          <b>${p.target_locked ? '目标已锁定' : '目标未锁定'}</b>
          <span class="tiny">${p.target_locked ? esc(p.target_note || '') : '锁定后才能让 AI 动手'}</span>
        </div>
        <div class="kv">
          <div class="k">题材</div><div class="v">${esc(p.genre)}</div>
          <div class="k">画幅</div><div class="v">${esc(p.ratio)}</div>
          <div class="k">集数</div><div class="v">${p.episode_count} 集 · ${esc(p.duration_range || p.episode_length)}</div>
          <div class="k">帧率 / 画质</div><div class="v">${Number(p.fps) || 30} fps · ${esc(p.quality || '电影级')}</div>
          <div class="k">避雷模板</div><div class="v">${esc(p.avoid_tpl || '标准版')}</div>
          <div class="k">默认工作流</div><div class="v">${esc((((S.state || {}).wf || []).find((w) => w.key === (p.video_wf || 'director')) || {}).name || '导演台工作流')}</div>
          <div class="k">平台</div><div class="v">${esc(p.platform)}</div>
          <div class="k">风格</div><div class="v">${esc(p.style_lock)}</div>
          <div class="k">禁区</div><div class="v">${esc(p.forbidden)}</div>
        </div>
        <div style="display:flex;gap:8px;margin-top:14px">
          ${p.target_locked ? `<button class="btn warn" data-act="unlockTarget">解除锁定（全链路重跑）</button>`
      : `<button class="btn primary" data-act="lockTarget">锁定目标</button>`}
        </div>
        <div style="margin-top:14px">
          <h3>锁定前体检</h3>
          ${risks.map((r) => `<div class="hint">· ${esc(r)}</div>`).join('')}
        </div>
      </div>
    </div>`;
}

/* ------------------------------------------------ 视图 1 大纲 */
function viewOutline() {
  const ep = ep0();
  if (!ep) return emptyView();
  const outline = parse(ep.outline_json, null);
  if (!outline) {
    return vhead('01', '小说大纲', '还没有大纲。先生成结构，再逐条改。', '') +
      `<div class="notice">第 ${ep.idx} 集还没有大纲结构。点右上角「生成大纲」，会按目标卡里的题材/集数/平台生成三幕结构，并把角色、场景、道具同步进右侧资产库。</div>`;
  }
  const acts = (outline.acts || []).map((a, ai) => `
    <div class="act">
      <div class="act-h"><b>${esc(a.name)}</b><span class="tag">${esc(a.beat)}</span><span class="line"></span>
        <span class="tiny">${(a.episodes || []).length} 集</span></div>
      <div class="epgrid">
        ${(a.episodes || []).map((e) => `
          <div class="ep">
            <div class="t"><span class="n">EP${String(e.idx).padStart(2, '0')}</span>
              <div style="flex:1">${roText(e.title, 'outline.title', e.idx, { cls: 'mono' })}</div>
            </div>
            <div class="tiny" style="margin-bottom:4px">开场钩子（前 3 秒）</div>
            ${roText(e.hook, 'outline.hook', e.idx, { rows: 2 })}
            <div class="tiny" style="margin:6px 0 4px">节拍（每行一条）</div>
            ${roText((e.beats || []).join('\n'), 'outline.beats', e.idx, { rows: 3 })}
            <div class="tiny" style="margin:6px 0 4px">高潮 / 收尾钩子</div>
            ${roText(e.climax, 'outline.climax', e.idx, { rows: 2 })}
            ${roText(e.payoff, 'outline.payoff', e.idx, { rows: 2 })}
          </div>`).join('')}
      </div>
    </div>`).join('');

  const staleNote = S.state.ep_state[0] && S.state.ep_state[0].outline_stale
    ? `<div class="notice">目标卡被改过 → 大纲标记「需重跑」。当前文本仍保留，你可以直接改，或点「重新生成」。</div>` : '';

  return vhead('01', '小说大纲', `三幕结构 · ${(outline.acts || []).reduce((n, a) => n + (a.episodes || []).length, 0)} 集 · 角色与场景已同步到资产库。改任意一条，下游剧本/分镜都会标记需重跑。`,
    `<button class="btn" data-act="optimize" data-stage="outline">AI 深度优化</button>
     <button class="btn primary" data-act="generate" data-kind="outline">重新生成大纲</button>`) +
    staleNote +
    `<div class="card" style="margin-bottom:14px">
      <div class="kv">
        <div class="k">LOGLINE</div><div class="v">${esc(outline.logline)}</div>
        <div class="k">风格锁</div><div class="v">${esc(outline.style && outline.style.camera || '')}</div>
        <div class="k">风险</div><div class="v">${(outline.risk || []).map(esc).join(' · ')}</div>
      </div>
    </div>` + acts;
}

/* ------------------------------------------------ 视图 2 小说（按集分组 + 全文下载） */
function viewNovel() {
  const ep = ep0();
  if (!ep) return emptyView();
  const eps = S.state.episodes || [];
  const text = ep.novel_text || '';
  const cards = eps.map((e) => {
    const es = (S.state.ep_state || []).find((x) => x.id === e.id) || {};
    const t = e.novel_text || '';
    const isCur = e.id === ep.id;
    return `<div class="card" style="margin-bottom:10px;border-color:${isCur ? '#2a4356' : 'var(--line)'}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
        <button class="btn sm ${isCur ? 'primary' : 'ghost'}" data-act="pickEp" data-id="${e.id}">EP${String(e.idx).padStart(2, '0')}</button>
        <b style="font-size:12.5px">${esc(e.title || ('第' + e.idx + '集'))}</b>
        <span class="tag">${t.length} 字</span>
        ${es.novel_stale ? '<span class="tag" style="color:#e2a75a">需重跑</span>' : ''}
        <span style="flex:1"></span>
        <button class="dlbtn" data-act="dlNovel" data-ep="${e.id}">下载本集</button>
      </div>
      <div class="tiny" style="white-space:pre-wrap;line-height:1.85;color:#a9b8c6;max-height:${isCur ? 'none' : '150px'};overflow:auto">${esc(t.slice(0, isCur ? 100000 : 900) || '（本集还没写正文）')}</div>
    </div>`;
  }).join('');
  return vhead('02', '小说正文', '按集分组：每一集独立正文、独立剧本、独立分镜。切集只影响视图，不影响数据；正文一改，本集的剧本/分镜/调度/提示词全部标记需重跑。',
    `<button class="dlbtn" data-act="dlNovel" data-all="1">下载全书</button>
     <button class="btn" data-act="epNew">+ 新增空集</button>
     <button class="btn" data-act="epCopy" data-id="${ep.id}">复制本集</button>
     <button class="btn" data-act="epDel" data-id="${ep.id}" data-idx="${ep.idx}">删除本集</button>
     <button class="btn" data-act="optimize" data-stage="novel">AI 深度优化</button>
     <button class="btn primary" data-act="generate" data-kind="novel">生成 / 重写本集</button>`) +
    (text ? '' : `<div class="notice">EP${String(ep.idx).padStart(2, '0')} 还没有正文。点「生成 / 重写本集」，会按大纲的钩子、节拍、爽点铺开。</div>`) +
    `<div class="card" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <span class="tag ok">正在编辑 EP${String(ep.idx).padStart(2, '0')} · ${esc(ep.title || '')}</span>
        <span class="tag">${text.length} 字</span>
        <span class="tiny">自动保存</span>
      </div>
      ${roText(text, 'episode.novel', ep.id, { rows: 20 })}
    </div>
    <div class="tiny" style="margin-bottom:6px">分集归档（共 ${eps.length} 集，点 EP 切换 / 下载单集）</div>
    ${cards}`;
}

/* ------------------------------------------------ 视图 3 剧本 */
function viewScript() {
  const ep = ep0();
  if (!ep) return emptyView();
  const script = parse(ep.script_json, null);
  if (!script || !(script.scenes || []).length) {
    return vhead('03', '剧本生成', '把正文拆成场次：地点、时间、人物、动作、对白。', '') +
      `<div class="notice">还没有剧本。点右上角「生成剧本」，会把正文拆成 6 场左右，台词按 4.5 字/秒控长。</div>`;
  }
  const scenes = script.scenes.map((s) => {
    const dl = (s.dialogue || []).map((d) => `${d.sp}｜${d.line}｜${d.emotion || ''}`).join('\n');
    return `<div class="scene">
      <div class="sh">
        <span class="tag blue">SC${String(s.no).padStart(2, '0')}</span>
        <div style="flex:1">${roText(s.slug, 'script.slug', s.no)}</div>
        <span class="tag">${esc(s.duration || '')}</span>
        <span class="tiny">${esc(s.purpose || '')}</span>
      </div>
      <div class="b">
        <div class="field"><label>动作 / 画面</label>${roText(s.action, 'script.action', s.no, { rows: 2 })}</div>
        <div class="field"><label>对白（格式：角色｜台词｜情绪，一行一条）</label>
          ${roText(dl, 'script.dialogue', s.no, { rows: Math.max(3, (s.dialogue || []).length), cls: 'mono' })}</div>
      </div>
    </div>`;
  }).join('');

  return vhead('03', '剧本生成', `${script.scenes.length} 场 · 台词单句 ≤ 24 字。改剧本会让分镜、调度、提示词全部标记需重跑。`,
    `<button class="btn" data-act="optimize" data-stage="script">AI 深度优化</button>
     <button class="btn primary" data-act="generate" data-kind="script">重新生成剧本</button>
     <button class="btn" data-act="generate" data-kind="board">生成分镜</button>`) +
    `<div class="card" style="margin-bottom:12px"><div class="kv">
      <div class="k">LOGLINE</div><div class="v">${esc(script.logline || '')}</div>
      <div class="k">编剧备注</div><div class="v">${esc(script.note || '')}</div>
    </div></div>` + scenes;
}

/* ------------------------------------------------ 视图 4 分镜 */
function viewShots() {
  const ep = ep0();
  if (!ep) return emptyView();
  const shots = S.state.shots.filter((s) => s.episode_id === ep.id);
  if (!shots.length) {
    return vhead('04', '分镜设计', '按场次拆成 4-10 秒的可生成镜头。', '') +
      `<div class="notice">还没有分镜。点「生成分镜」，会按剧本拆出建立镜 / 对白镜 / 反应镜，并自动错开相邻景别。</div>`;
  }
  const sizes = ['大全景', '全景', '中景', '中近景', '近景', '特写', '大特写'];
  const rows = shots.map((s) => {
    const f = s.flags || {};
    const chain = `<div class="chain">
      <b class="${f.block_stale ? 'stale' : ''}" title="② 场面调度">②</b>
      <b class="${f.prompt_stale ? 'stale' : ''}" title="③ 提示词">③</b>
      <b class="${f.gen_stale ? 'stale' : ''}" title="④ 生成">④</b>
    </div>`;
    return `<tr draggable="true" data-sid="${s.id}" data-idx="${s.idx}" title="拖动这一行可以换顺序">
      <td><span class="idx">${String(s.idx).padStart(2, '0')}</span></td>
      <td><select data-field="shot.shot_size" data-id="${s.id}">
        ${sizes.map((z) => `<option ${s.shot_size === z ? 'selected' : ''}>${z}</option>`).join('')}</select></td>
      <td><input data-field="shot.movement" data-id="${s.id}" value="${esc(s.movement || '')}"></td>
      <td><input type="number" min="1" max="15" data-field="shot.duration" data-id="${s.id}" value="${s.duration}"></td>
      <td><div class="tiny" style="color:#9fb0c0">${esc((s.chars_list || []).join(' / ') || '—')}</div>
          <div class="tiny">${esc((s.props_list || []).join(' / '))}</div></td>
      <td>${roText(s.action, 'shot.action', s.id, { rows: 2 })}
          ${roText((s.dialogue || []).map((d) => d.sp + '：' + d.line).join(' / '), 'shot.dialogue', s.id, { rows: 2 })}</td>
      <td>
        <select data-field="shot.workflow" data-id="${s.id}" title="这一镜单独覆盖工作流">
          <option value="" ${!s.workflow ? 'selected' : ''}>继承本集</option>
          ${((S.state.wf) || []).map((w) => `<option value="${w.key}" ${s.workflow === w.key ? 'selected' : ''}>${esc(w.name.replace('工作流', ''))}</option>`).join('')}
        </select>
      </td>
      <td>${chain}</td>
      <td><button class="lockbtn ${s.locked ? 'on' : ''}" data-act="shotLock" data-id="${s.id}" title="锁定这一镜">${s.locked ? '🔒' : '🔓'}</button>
        <button class="btn sm" data-act="shotChatOpen" data-id="${s.id}" data-idx="${s.idx}" title="只改这一镜">AI改镜</button>
        <button class="btn sm ghost" data-act="shotCopy" data-id="${s.id}" title="复制成新的一镜">复制</button>
        <button class="btn sm ghost" data-act="shotDel" data-id="${s.id}" data-idx="${s.idx}" title="删除这一镜">删</button></td>
    </tr>`;
  }).join('');

  const stale = S.state.stats.need_block;
  return vhead('04', '分镜设计', `${shots.length} 镜 · 合计 ${S.state.stats.duration}s。这一层是 ①，任何改动都会让 ②③④ 失效（看每行右侧链路灯）。`,
    `<button class="btn" data-act="optimize" data-stage="board">深度优化分镜</button>
     <button class="btn primary" data-act="generate" data-kind="board">重新生成分镜</button>
     <button class="btn warn" data-act="generate" data-kind="blocking">全部做②场面调度</button>
     <button class="btn" data-act="addShot">+ 新增一镜</button>`) +
    `<div class="card" style="padding:10px 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <span class="tiny">本集统一工作流</span>
      <select id="epWf">
        <option value="">跟随项目默认（${esc((((S.state.wf || []).find((w) => w.key === (proj().video_wf || 'director')) || {}).name || '导演台工作流'))}</option>
        ${((S.state.wf) || []).map((w) => `<option value="${w.key}">${esc(w.name)} · ${esc(w.shot_rule)}</option>`).join('')}
      </select>
      <button class="btn sm primary" data-act="wfSetEpisode" data-id="${ep.id}">应用本集</button>
      <span class="tiny">单镜下拉可单独覆盖；工作流决定 ②③④ 的风格前缀、负面词、镜头规范与参数</span>
    </div>` +
    (stale ? `<div class="notice">${stale} 个镜头的 ② 场面调度已失效（上游分镜/对白被改过）。点「全部做②场面调度」重跑。</div>` : '') +
    `<div class="card" style="padding:0;overflow:auto">
      <table class="tbl" id="shotTbl"><thead><tr>
        <th>#</th><th>景别</th><th>运镜</th><th>时长</th><th>角色 / 道具</th><th>动作 / 对白</th><th>工作流</th><th>链路</th><th>锁 / 改镜</th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>` + chatPanel();
}

/* ------------------------------------------------ PRD 七：单镜头 AI 对话（应用 / 取消） */
function chatPanel() {
  const sid = S.chatShot;
  if (!sid) return '';
  const s = (S.state.shots || []).find((x) => x.id === sid) || {};
  const msgs = S.chatMsgs[sid] || [];
  const prop = S.chatProposal && S.chatProposal.sid === sid ? S.chatProposal : null;
  const keyName = { action: '动作', dialogue: '对白', duration: '时长', shot_size: '景别', movement: '运镜', emotion: '情绪' };
  return `<div class="card chatcard" id="shotChat">
    <h3 style="display:flex;align-items:center;gap:8px">AI 对话 — 第 ${s.idx || ''} 镜
      <small style="color:#8b9aa8;font-weight:400">只改这一镜，不动别的镜头</small>
      <button class="btn sm ghost" style="margin-left:auto" data-act="shotChatClose">收起</button></h3>
    <div class="chatlog">
      <div class="cm sys"><b>原描述（只读）</b><div>${esc((s.action || '（空）').slice(0, 120))}</div></div>
      ${msgs.map((m) => `<div class="cm ${m.role}"><b>${m.role === 'user' ? '你' : 'AI'}</b><div>${esc(m.text)}</div></div>`).join('')
      || '<div class="hint">说一句就行，例如「改成她推门进来，先愣住再哭，时长加到 8 秒」</div>'}
    </div>
    ${prop ? `<div class="notice">AI 改稿：${esc((prop.changed || []).map((k) => keyName[k] || k).join(' / '))} —— 点【应用】才写入本镜，点【取消】全丢掉</div>` : ''}
    <div class="chatinput">
      <textarea id="chatText" rows="2" placeholder="只改第 ${s.idx || ''} 镜…（Enter 发送，Shift+Enter 换行）"></textarea>
      <div class="chbtns">
        <button class="btn primary" data-act="shotChatSend" data-id="${sid}">发送</button>
        ${prop ? `<button class="btn warn" data-act="shotChatApply" data-id="${sid}">应用</button>
        <button class="btn ghost" data-act="shotChatDiscard">取消</button>` : ''}
      </div>
    </div>
  </div>`;
}

/* ------------------------------------------------ 视图 5 调度 */
function viewBlocking() {
  const ep = ep0();
  const shots = S.state.shots.filter((s) => s.episode_id === ep.id);
  if (!shots.length) return emptyView();
  const p = proj();
  const cards = shots.map((s) => {
    const b = s.blocking || {};
    const cam = b.camera || {};
    const light = b.light || {};
    const stale = (s.flags || {}).block_stale;
    const v = s.updated_at ? encodeURIComponent(s.updated_at) : '0';
    return `<div class="sbox">
      <div class="frame ${p.ratio === '16:9' ? 'land' : ''}">
        <img src="/api/shots/${s.id}/frame.svg?v=${v}" loading="lazy" alt="预演帧">
        <div class="badges"><span class="tag ${stale ? 'warn' : 'ok'}">${stale ? '需重跑' : '已调度'}</span>
          <span class="tag">SHOT ${String(s.idx).padStart(2, '0')}</span></div>
      </div>
      <div class="body">
        <div class="h"><div class="t">${esc(s.shot_size)} · ${s.duration}s</div>
          <button class="lockbtn ${s.locked ? 'on' : ''}" data-act="shotLock" data-id="${s.id}">${s.locked ? '🔒' : '🔓'}</button></div>
        <div class="chipsline">
          <span class="tag">${esc(cam.height || '—')}</span><span class="tag">${esc(cam.side || '—')}</span>
          <span class="tag">${esc(b.lens || '—')}</span><span class="tag">${esc(cam.move || '')}</span>
        </div>
        <div class="aprops">
          <div>主光 <b>${esc(light.dir || '—')}</b></div><div>色温 <b>${esc(light.temp || '—')}</b></div>
          <div>构图 <b>${esc((b.comp || '').slice(0, 22))}</b></div><div>转场 <b>${esc(b.transition || '—')}</b></div>
        </div>
        <div class="beats">${(b.beats || []).map((x) => `<div><b>${esc(x.t)}</b>${esc(x.what)}</div>`).join('')}</div>
        ${roText(b.text || '', 'blocking.text', s.id, { rows: 3 })}
      </div>
      <div class="sfoot">
        <button class="btn sm" data-act="genShot" data-kind="blocking" data-id="${s.id}">重跑这一镜</button>
        <button class="btn sm" data-act="optimize" data-stage="blocking" data-id="${s.id}">深度优化</button>
        <button class="btn sm" data-act="genShot" data-kind="prompt" data-id="${s.id}">→ 出提示词</button>
      </div>
    </div>`;
  }).join('');
  return vhead('05', '场面调度', '把「说台词」变成可拍的调度：机位高度、轴线、焦距、走位、构图、光位、节拍。右侧预演帧就是按这些参数画的——调度一改，图立刻变。',
    `<button class="btn primary" data-act="generate" data-kind="blocking">全部重跑调度</button>
     <button class="btn warn" data-act="generate" data-kind="prompt">全部生成③提示词</button>`) +
    `<div class="sgrid">${cards}</div>`;
}

/* ------------------------------------------------ 视图 6 提示词 */
function viewPrompts() {
  const ep = ep0();
  const shots = S.state.shots.filter((s) => s.episode_id === ep.id);
  if (!shots.length) return emptyView();
  const cards = shots.map((s) => {
    const f = s.flags || {};
    const v = s.updated_at ? encodeURIComponent(s.updated_at) : '0';
    return `<div class="sbox">
      <div class="frame ${proj().ratio === '16:9' ? 'land' : ''}" style="max-height:220px">
        <img src="/api/shots/${s.id}/frame.svg?v=${v}" loading="lazy">
        <div class="badges"><span class="tag ${f.prompt_stale ? 'warn' : 'ok'}">${f.prompt_stale ? '③需重跑' : '③已生成'}</span>
          <span class="tag">SHOT ${String(s.idx).padStart(2, '0')}</span></div>
      </div>
      <div class="body">
        <div class="field" style="margin:0"><label>中文主提示（锁定资产已注入）</label>
          ${roText(s.prompt_cn, 'shot.prompt_cn', s.id, { rows: 5 })}</div>
        <div class="field" style="margin:0"><label>负面提示</label>
          ${roText(s.prompt_neg, 'shot.prompt_neg', s.id, { rows: 2 })}</div>
        <div class="tiny">英文补充：${esc((s.prompt_en || '').slice(0, 90))}${(s.prompt_en || '').length > 90 ? '…' : ''}</div>
        <div class="h3box">
          <div class="h3top"><span>H3 提交原文（逐字送进 ComfyUI）</span>
            <b>${s.h3_refs || 0} 张参考图</b></div>
          <pre>${esc(s.h3_prompt || '（先生成 ③ 才会出现）')}</pre>
        </div>
      </div>
      <div class="sfoot">
        <button class="btn sm" data-act="openParts" data-id="${s.id}">拆窗编辑（11 栏）</button>
        <button class="btn sm" data-act="copy" data-kind="h3" data-id="${s.id}">复制 H3 原文</button>
        <button class="btn sm" data-act="copy" data-kind="prompt_cn" data-id="${s.id}">复制主提示</button>
        <button class="btn sm" data-act="optimize" data-stage="prompt" data-id="${s.id}">深度优化</button>
        <button class="btn sm" data-act="genShot" data-kind="prompt" data-id="${s.id}">重出</button>
        <button class="btn sm" data-act="genShot" data-kind="video" data-id="${s.id}">生成这一镜</button>
      </div>
    </div>`;
  }).join('');
  const mode = (proj().prompt_mode || 'cn');
  return vhead('06', '视频提示词', `提示词 = 主体 + 场景 + 道具 + 动作 + 镜头 + 构图 + 光线 + 风格 + 对白 + 表演。当前格式：${mode === 'h3ref' ? 'H3 官方六段式（参考模式）' : '中文自由式'}。`,
    `${mode === 'h3ref'
      ? `<button class="btn" data-act="setPromptMode" data-mode="cn">切回中文自由式</button>`
      : `<button class="btn" data-act="setPromptMode" data-mode="h3ref">切成 H3 官方六段式</button>`}
     <button class="btn" data-act="contAll" data-value="1">全镜开启镜头续接</button>
     <button class="btn" data-act="contAll" data-value="0">关闭续接</button>
     <button class="btn primary" data-act="generate" data-kind="prompt">全部重新生成</button>
     <button class="btn warn" data-act="generate" data-kind="video">全部生成④视频</button>`) +
    (() => {
      const miss = shots.filter((s) => {
        const lines = (s.dialogue || []).map((d) => d.line || '').filter(Boolean).join('；');
        return lines && !(s.prompt_cn || '').includes(lines);
      });
      if (!miss.length) return '';
      return `<div class="notice warn"><b>有 ${miss.length} 镜的台词没写进提示词</b>（历史 bug：对白存了 JSON 但生成时没读出来，这些镜头 H3 是「无对白」跑出来的）。
        <button class="btn sm primary" data-act="repairDialogue">补齐这 ${miss.length} 镜台词提示词</button>
        <span class="tiny">补齐后这些镜会标「④待重跑」，重跑才有真台词和口型。</span></div>`;
    })() +
    `<div class="sgrid">${cards}</div>`;
}

/* ------------------------------------------------ 拆窗提示词编辑器 */
const PART_LABELS = [
  ['size_move', '景别/运镜'], ['video_ref', '视频参考'], ['shot_desc', '镜头描述'],
  ['chars', '角色'], ['scene', '场景'], ['blocking', '场面调度'],
  ['h3', 'H3视频提示词（手动补充）'], ['dialogue', '对白'], ['assets', '关联资产'], ['angle', '场景视角'],
];

function openPartsEditor(sid) {
  const s = S.state.shots.find((x) => x.id === sid);
  if (!s) return;
  const p = s.parts || {};
  const seed = s.seed === undefined || s.seed === null || s.seed < 0 ? '' : s.seed;
  let mask = document.getElementById('partsMask');
  if (!mask) {
    mask = document.createElement('div');
    mask.id = 'partsMask';
    mask.className = 'mask hidden';
    document.body.appendChild(mask);
  }
  mask.innerHTML = `<div class="pmodal partmodal">
    <div class="mhead"><b>SHOT ${String(s.idx).padStart(2, '0')} · 拆窗提示词</b>
      <span class="tiny">11 栏独立可改；保存直接改写 H3 原文，这镜标④待重跑</span>
      <button class="btn sm" data-act="closeParts">关闭</button></div>
    <div class="pgrid">
      ${PART_LABELS.map(([k, label]) => `<label class="pl"><span>${label}</span>
        <textarea data-part="${k}" rows="${k === 'h3' ? 4 : 2}">${esc(p[k] || '')}</textarea></label>`).join('')}
      <label class="pl"><span>随机种子（留空=随机，固定数字=可复现）</span>
        <input data-part="seed" value="${seed}" placeholder="例如 123456"></input></label>
    </div>
    <div class="mfoot">
      <button class="btn primary" data-act="saveParts" data-id="${s.id}">保存拆窗</button>
      <button class="btn" data-act="saveParts" data-id="${s.id}" data-regen="1">保存并重出④</button>
    </div>
  </div>`;
  mask.classList.remove('hidden');
}

async function saveParts(sid, regen) {
  const mask = document.getElementById('partsMask');
  if (!mask) return;
  const parts = {};
  mask.querySelectorAll('textarea[data-part]').forEach((ta) => {
    const k = ta.dataset.part;
    if (ta.value.trim()) parts[k] = ta.value.trim();
  });
  const seedRaw = (mask.querySelector('input[data-part="seed"]') || {}).value || '';
  const seed = seedRaw.trim() === '' ? -1 : parseInt(seedRaw.trim(), 10);
  parts.seed = seed >= 0 ? String(seed) : '随机';
  await api(`/api/shots/${sid}`, { method: 'POST', body: { parts_json: JSON.stringify(parts), seed: Number.isFinite(seed) ? seed : -1 } });
  mask.classList.add('hidden');
  toast('拆窗已保存：H3 原文按这 11 栏逐栏生成');
  await refresh(true);
  if (regen) genShot(sid, 'video');
}

/* ------------------------------------------------ 单镜头数据包 */
function pkGet(obj, path) {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : ''), obj);
}
function pkSet(obj, path, val) {
  const keys = path.split('.');
  let o = obj;
  keys.slice(0, -1).forEach((k) => { if (!o[k] || typeof o[k] !== 'object') o[k] = {}; o = o[k]; });
  o[keys[keys.length - 1]] = val;
}
function pkInput(label, path, value, opts = '') {
  return `<label class="pl"><span>${label}</span><input data-pk="${path}" value="${esc(value == null ? '' : value)}" ${opts}></label>`;
}
function pkArea(label, path, value, rows = 2) {
  return `<label class="pl"><span>${label}</span><textarea data-pk="${path}" rows="${rows}">${esc(value || '')}</textarea></label>`;
}
function pkSelect(label, path, value, options) {
  return `<label class="pl"><span>${label}</span><select data-pk="${path}">${options.map(([v, t]) =>
    `<option value="${esc(v)}" ${String(value) === String(v) ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></label>`;
}

async function openPacketEditor(sid) {
  const r = await api(`/api/shots/${sid}/packet`);
  const pk = r.packet || {};
  const assets = (S.state.assets || []);
  const s = (S.state.shots || []).find((x) => x.id === sid) || {};
  const charOpts = (name, cur) => `<option value="">未绑定（禁止渲染）</option>` + assets.filter((a) => a.kind === 'char').map((a) =>
    `<option value="${a.id}" ${String(cur) === String(a.id) ? 'selected' : ''}>${esc(a.name)}${a.locked ? ' · 已锁' : ''}</option>`).join('')
    + (name ? '' : '');
  const sceneOpts = `<option value="">未绑定（禁止渲染）</option>` + assets.filter((a) => a.kind === 'scene').map((a) =>
    `<option value="${a.id}" ${String((pk.bind || {}).scene) === String(a.id) ? 'selected' : ''}>${esc(a.name)}</option>`).join('');
  const propOpts = (name, cur) => `<option value="">未绑定（禁止渲染）</option>` + assets.filter((a) => a.kind === 'prop').map((a) =>
    `<option value="${a.id}" ${String(cur) === String(a.id) ? 'selected' : ''}>${esc(a.name)}</option>`).join('');
  const charRows = (pk.chars || []).map((c) => `<div class="pkrow" data-char="${esc(c.name)}">
    <b>${esc(c.name)}</b>
    <select data-bind-char="${esc(c.name)}">${charOpts(c.name, (pk.bind.chars || {})[c.name])}</select>
    ${['role', 'action', 'position', 'expression', 'costume'].map((k) =>
      `<input data-char-field="${k}" value="${esc(c[k] || '')}" placeholder="${({ role: '主角/配角', action: '本镜动作', position: '站位', expression: '表情', costume: '服装' })[k]}">`).join('')}
  </div>`).join('');
  const propRows = (pk.props || []).map((p) => `<div class="pkrow" data-prop="${esc(p.name)}">
    <b>${esc(p.name)}</b>
    <select data-bind-prop="${esc(p.name)}">${propOpts(p.name, (pk.bind.props || {})[p.name])}</select>
    <input data-prop-note="${esc(p.name)}" value="${esc(p.note || '')}" placeholder="道具状态/用法">
  </div>`).join('');
  let mask = document.getElementById('packetMask');
  if (!mask) {
    mask = document.createElement('div');
    mask.id = 'packetMask';
    mask.className = 'mask hidden';
    document.body.appendChild(mask);
  }
  mask.innerHTML = `<div class="pmodal packetmodal">
    <div class="mhead"><b>SHOT ${String(s.idx || 0).padStart(2, '0')} · 单镜头数据包</b>
      <span class="tiny">单镜字段优先；全局风格只做兜底。缺一项就禁止提交 ComfyUI。</span>
      <button class="btn sm" data-act="closePacket">关闭</button></div>

    <div class="pksec"><h4>A. 镜头基础</h4><div class="pgrid">
      ${pkArea('镜头描述（必填）', 'desc', pk.desc, 2)}
      ${pkArea('拍摄方案', 'plan', pk.plan, 2)}
      ${pkInput('景别', 'shot_size', pk.shot_size)}
      ${pkInput('拍摄角度', 'angle', pk.angle)}
      ${pkInput('运镜方式（必填）', 'movement', pk.movement)}
      ${pkInput('镜头时长 1–15s', 'duration', r.duration, 'type=number min=1 max=15')}
    </div></div>

    <div class="pksec"><h4>B. 画面风格（单镜覆盖全局）</h4><div class="pgrid">
      ${pkInput('焦段', 'lens', pk.lens, 'placeholder=35mm / 85mm')}
      ${pkInput('光影', 'light', pk.light)}
      ${pkInput('构图', 'composition', pk.composition)}
      ${pkInput('色调', 'tone', pk.tone)}
      ${pkInput('镜头质感', 'texture', pk.texture)}
      ${pkInput('单镜风格描述（留空=全局兜底）', 'style_desc', pk.style_desc)}
    </div></div>

    <div class="pksec"><h4>C. 角色与动作 · 逐镜绑定角色图</h4>
      <p class="tip">每个出场人物必须绑定角色资产，此绑定只作用于本镜。</p>
      ${charRows || '<div class="wsub">本镜没有角色（纯空镜）</div>'}
      ${pkArea('人物走位补充', 'blocking.walk', (pk.blocking || {}).walk, 1)}
    </div>

    <div class="pksec"><h4>D. 场景与道具 · 逐镜绑定素材</h4><div class="pgrid">
      ${pkInput('场景名称', 'scene.name', (pk.scene || {}).name)}
      ${pkInput('环境状态', 'scene.env', (pk.scene || {}).env, 'placeholder=雨夜 / 清晨 / 灯全灭')}
      <label class="pl"><span>场景参考图（必绑）</span><select data-bind-scene>${sceneOpts}</select></label>
    </div>${propRows || '<div class="wsub">本镜没有道具</div>'}</div>

    <div class="pksec"><h4>E. 场面调度</h4><div class="pgrid">
      ${pkInput('人物走位', 'blocking.walk', (pk.blocking || {}).walk)}
      ${pkInput('镜头与人物关系', 'blocking.relation', (pk.blocking || {}).relation)}
      ${pkInput('前后景关系', 'blocking.depth', (pk.blocking || {}).depth)}
      ${pkInput('遮挡关系', 'blocking.occlusion', (pk.blocking || {}).occlusion)}
      ${pkInput('运动方向', 'blocking.direction', (pk.blocking || {}).direction)}
    </div></div>

    <div class="pksec"><h4>F. 参考素材（每镜独立上传/绑定）</h4><div class="pgrid">
      <label class="pl"><span>首帧图</span><input data-pk="bind.first_frame" value="${esc((pk.bind || {}).first_frame || '')}" placeholder="/media/...">
        <input type="file" accept="image/*" data-file="bind.first_frame"></label>
      <label class="pl"><span>尾帧图</span><input data-pk="bind.last_frame" value="${esc((pk.bind || {}).last_frame || '')}" placeholder="/media/...">
        <input type="file" accept="image/*" data-file="bind.last_frame"></label>
      <label class="pl"><span>参考视频（取首帧作图像参考，视频本体保留可追溯）</span><input data-pk="bind.ref_video" value="${esc((pk.bind || {}).ref_video || '')}" placeholder="/media/...">
        <input type="file" accept="video/*" data-file="bind.ref_video"></label>
    </div><div class="wsub">上传后真实落到平台 media，并在提交时复制进 ComfyUI input 节点。</div></div>

    <div class="pksec"><h4>G. Mini3 渲染参数</h4><div class="pgrid">
      ${pkArea('正向提示词（必填）', 'params.prompt_cn', (pk.params || {}).prompt_cn, 3)}
      ${pkArea('负面提示词', 'params.prompt_neg', (pk.params || {}).prompt_neg, 2)}
      ${pkInput('随机种子（必填）', 'params.seed', (pk.params || {}).seed, 'type=number min=0')}
      ${pkInput('IP-Adapter 权重', 'params.ip_weight', (pk.params || {}).ip_weight, 'type=number step=0.05 min=0 max=1')}
      ${pkInput('降噪参数', 'params.denoise', (pk.params || {}).denoise, 'type=number step=0.05 min=0 max=1')}
      ${pkSelect('档位', 'params.steps', (pk.params || {}).steps, [['4', '4步'], ['6', '6步'], ['8', '8步']])}
      ${pkInput('帧率', 'params.fps', (pk.params || {}).fps, 'type=number min=12 max=30')}
      ${pkSelect('画质等级', 'params.quality', (pk.params || {}).quality, [['标准', '标准'], ['高', '高'], ['电影级', '电影级']])}
    </div></div>

    <div class="pksec"><h4>H. 对白与口型</h4><div class="pgrid">
      ${pkInput('说话人物', 'dialogue.speaker', (pk.dialogue || {}).speaker)}
      ${pkInput('情绪标签', 'dialogue.emotion', (pk.dialogue || {}).emotion)}
      ${pkSelect('方言配置', 'dialogue.dialect', (pk.dialogue || {}).dialect,
        [['zh', '普通话'], ['yue', '广州白话'], ['hak', '涯话（客家话）'], ['en', '英语']])}
      ${pkArea('旁白文本', 'dialogue.narration', (pk.dialogue || {}).narration, 2)}
      <label class="pl pkwide"><span>对白（每行一条：角色：台词）</span>
        <textarea data-dlg rows="4">${esc((s.dialogue || []).map((d) => (d.sp ? d.sp + '：' : '') + (d.line || '')).join('\n'))}</textarea></label>
    </div></div>

    <div class="mfoot">
      <button class="btn" data-act="regenPacketPrompt" data-id="${s.id}">由字段生成正向提示词</button>
      <button class="btn" data-act="savePacket" data-id="${s.id}">保存数据包</button>
      <button class="btn primary" data-act="savePacket" data-id="${s.id}" data-gen="1">保存并生成本镜</button>
    </div>
  </div>`;
  mask.classList.remove('hidden');
}

function collectPacket(mask) {
  const pk = { chars: [], props: [], bind: { chars: {}, props: {} } };
  mask.querySelectorAll('input[data-pk],textarea[data-pk],select[data-pk]').forEach((el) => {
    let v = el.value;
    if (el.dataset.pk === 'params.seed' || el.dataset.pk === 'params.ip_weight' ||
        el.dataset.pk === 'params.denoise' || el.dataset.pk === 'params.steps' ||
        el.dataset.pk === 'params.fps' || el.dataset.pk === 'duration') v = Number(v);
    pkSet(pk, el.dataset.pk, v);
  });
  mask.querySelectorAll('.pkrow[data-char]').forEach((row) => {
    const name = row.dataset.char;
    const c = { name };
    row.querySelectorAll('input[data-char-field]').forEach((el) => { c[el.dataset.charField] = el.value; });
    const bind = row.querySelector('select[data-bind-char]');
    if (bind && bind.value) pk.bind.chars[name] = Number(bind.value);
    pk.chars.push(c);
  });
  mask.querySelectorAll('.pkrow[data-prop]').forEach((row) => {
    const name = row.dataset.prop;
    const p = { name, note: (row.querySelector('input[data-prop-note]') || {}).value || '' };
    const bind = row.querySelector('select[data-bind-prop]');
    if (bind && bind.value) pk.bind.props[name] = Number(bind.value);
    pk.props.push(p);
  });
  const scene = mask.querySelector('select[data-bind-scene]');
  pk.bind.scene = scene && scene.value ? Number(scene.value) : '';
  return pk;
}

async function savePacket(sid, opts = {}) {
  const mask = document.getElementById('packetMask');
  if (!mask) return;
  const packet = collectPacket(mask);
  const duration = Number(pkGet(packet, 'duration') || 5);
  const dialogue_lines = [];
  (mask.querySelector('textarea[data-dlg]') || { value: '' }).value.split('\n').forEach((line) => {
    const t = line.trim();
    if (!t) return;
    const m = t.split(/：|:/);
    dialogue_lines.push({ sp: m.length > 1 ? m[0].trim() : ((packet.dialogue || {}).speaker || ''), line: (m.length > 1 ? m.slice(1).join(':') : t).trim() });
  });
  const r = await api(`/api/shots/${sid}/packet`, {
    method: 'POST',
    body: { packet, duration, dialogue_lines, regen_prompt: !!opts.regen },
  });
  if (!r.ok) toast('已保存，但仍有 ' + (r.errors || []).length + ' 项缺失：' + ((r.errors || [])[0] || {}).msg);
  else toast(opts.regen ? '数据包已保存，正向提示词已由字段生成' : '数据包已保存：单镜参数已覆盖全局');
  if (!opts.keep) mask.classList.add('hidden');
  await refresh(true);
  if (opts.gen) genShot(sid, 'video');
}



const PERF_DEF = { tone: '克制', intensity: 3, face: '眼神先动、嘴角后动',
  volume: '中', rate: '中', gesture: '小幅、贴身动作', eye: '看向对方眼睛',
  lip: '开口说台词、口型与台词逐字对齐' };
const PERF_OPTS = {
  tone: ['克制', '压抑', '委屈', '愤怒', '爆发', '麻木', '惊慌', '得意', '心虚', '温柔'],
  intensity: ['1', '2', '3', '4', '5'],
  volume: ['气声', '小声', '中', '大声', '喊出来'],
  rate: ['慢', '中', '快', '抢着说'],
  face: ['眼神先动、嘴角后动', '眉心收紧、鼻翼发紧', '嘴角下压、下巴发抖', '瞪眼、眉毛抬起', '眼睛发红、泪水打转', '面无表情、眼神发直'],
  gesture: ['小幅、贴身动作', '手指绞在一起', '后退半步、肩膀缩起', '握拳、手臂绷紧', '冲上前、手先到', '转身背对'],
  eye: ['看向对方眼睛', '躲开视线看地面', '盯着对方的嘴', '望向画外', '闭眼再睁开'],
  lip: ['开口说台词、口型与台词逐字对齐', '咬着牙说、字从牙缝挤出来', '气声说、嘴几乎不张', '边哭边说、气息断续'],
};

function perfVal(s, k) {
  const p = Object.assign({}, PERF_DEF, (s.perf && typeof s.perf === 'object') ? s.perf : {});
  return String(p[k] === undefined ? PERF_DEF[k] : p[k]);
}

function perfPanel(s) {
  const sel = (k, label) => `<label class="pfrow"><span>${label}</span>
    <select data-perf-field="${k}">${PERF_OPTS[k].map((o) => `<option ${perfVal(s, k) === o ? 'selected' : ''}>${o}</option>`).join('')}</select></label>`;
  return `<div class="perfbox" data-pid="${s.id}">
    <div class="pfhead">表演层（会原样写进 H3 提交原文，并锁死口型与语言）
      <span class="tiny">强度 5 = 爆发；音量/语速按真人呼吸来</span></div>
    <div class="pfgrid">
      ${sel('tone', '情绪')}${sel('intensity', '强度')}${sel('face', '表情')}
      ${sel('volume', '音量')}${sel('rate', '语速')}${sel('gesture', '肢体')}
      ${sel('eye', '眼神')}${sel('lip', '口型')}
    </div>
    <div class="pfbar">
      <label class="tiny"><input type="checkbox" data-perf-lock ${Number(s.perf_lock) ? 'checked' : ''}> 锁死表演（重跑不许改）</label>
      <button class="btn sm primary" data-act="applyPerf" data-id="${s.id}">保存表演</button>
      <button class="btn sm ghost" data-act="perfToggle" data-id="${s.id}">收起</button>
    </div>
  </div>`;
}

function viewVideo() {
  const ep = ep0();
  const shots = S.state.shots.filter((s) => s.episode_id === ep.id);
  if (!shots.length) return emptyView();
  const sv = S.state.server;
  const notice = sv.comfy.ok
    ? `<div class="notice ok">ComfyUI 在线（${esc(sv.comfy.version)}）。点生成会真实提交工作流，产物落在 data/media/。</div>`
    : `<div class="notice">ComfyUI 离线。点生成会先产出「调度预演片段」，合集照样能自动串起来；把 ComfyUI 起来后在控制台把地址填对，再点重跑就会换成真视频。</div>`;
  const vjob = (S.state.jobs || []).find((j) => j.kind === 'video' && (j.status === 'running' || j.status === 'queued'));
  const jobbar = vjob ? (() => {
    const line = ((vjob.log || []).slice(-1)[0] || {}).msg || '';
    const eta = (line.match(/预计 (\d+)s/) || [])[1];
    const done = (line.match(/(\d+)s \//) || [])[1];
    return `<div class="jobbar"><div class="jb-top"><span>④ ${esc(vjob.message || '生成中')} · ${esc(line || '提交中')}</span>
      <b>${vjob.progress || 0}%${eta ? ' · 本镜 ' + (done || 0) + '/' + eta + 's' : ''}</b></div>
      <div class="jb"><i style="width:${vjob.progress || 0}%"></i></div></div>`;
  })() : '';
  const cards = shots.map((s) => {
    const f = s.flags || {};
    const v = s.updated_at ? encodeURIComponent(s.updated_at) : '0';
    const st = s.status || 'empty';
    const tier = s.res_tier || '';
    const label = { done: 'REAL', preview: 'PREVIEW', running: 'RUNNING', failed: 'FAILED', empty: '—' }[st] || st;
    const chat = S.chatOpen[s.id] ? `<div class="chatbox">
      <div class="chatlog">${(s.chat || []).length
        ? (s.chat || []).map((m) => `<div class="msg ${m.role === 'user' ? 'user' : 'assistant'}">${esc(m.text)}</div>`).join('')
        : '<div class="tiny">直接说你想怎么改这一镜，系统会把它落成可执行参数（台词/景别/机位/光/时长），并让 ②③④ 失效。</div>'}
      </div>
      <div class="chatinput">
        <textarea data-shot="${s.id}" placeholder="例：这句台词太生硬，改得情绪更强烈点 / 镜头推近一些，拍他的表情（Enter 发送，Shift+Enter 换行）"></textarea>
        <button class="btn sm primary" data-act="chatSend" data-id="${s.id}">发送</button>
      </div></div>` : '';
    return `<div class="gcard">
      <div class="f" data-act="playShot" data-url="${esc((s.video_url || '').match(/\.(mp4|webm)$/i) ? s.video_url : '')}" title="${st === 'done' ? '点击播放真片' : '还没真片，点生成'}">
        <img src="${st === 'done' && s.thumb_url ? s.thumb_url + '?v=' + v : `/api/shots/${s.id}/frame.svg?v=${v}`}" loading="lazy">
        <span class="st ${st}">${label}</span>
        ${(s.video_url || '').match(/\.(mp4|webm)$/i) ? '<span class="playtip">▶ 点击播放</span>' : ''}
      </div>
      <div class="cap"><b>SHOT ${String(s.idx).padStart(2, '0')}</b> · ${esc(s.shot_size)} · ${s.duration}s
        ${s.h3_refs ? `<span class="tag" style="font-size:9px">参考图 ${s.h3_refs}</span>` : ''}
        <span class="tag" style="font-size:9px">${tier || '自动档'}</span>
        ${Number(s.ref_prev) ? '<span class="tag" style="font-size:9px;color:#6fa8ff">接上一镜</span>' : ''}
        ${s.error ? `<div class="tiny" style="color:#e2a75a">${esc(s.error.slice(0, 40))}</div>` : ''}
        ${f.gen_stale ? '<div class="tiny">④ 待重跑</div>' : ''}
      </div>
      <div class="sfoot" style="padding:6px 8px;flex-wrap:wrap">
        <button class="btn sm" data-act="genShot" data-kind="video" data-id="${s.id}">生成</button>
        <select data-act="setResTier" data-id="${s.id}" style="padding:4px 6px;background:#0f151a;border:1px solid var(--line2);border-radius:6px">
          ${[['', '自动档'], ['480p', '480p 快'], ['720p', '720p 标准'], ['1080p', '1080p 高清']].map(([v2, n]) =>
            `<option value="${v2}" ${tier === v2 ? 'selected' : ''}>${n}</option>`).join('')}
        </select>
        <label class="tiny" style="display:flex;align-items:center;gap:4px;cursor:pointer">
          <input type="checkbox" data-act="toggleRefPrev" data-id="${s.id}" ${Number(s.ref_prev) ? 'checked' : ''}> 参考上一镜
        </label>
        <button class="btn sm ghost" data-act="chatToggle" data-id="${s.id}">AI 改镜</button>
        <button class="btn sm ghost" data-act="perfToggle" data-id="${s.id}">表演</button>
        <button class="btn sm ghost ${Number(s.prompt_lock) ? 'on' : ''}" data-act="togglePromptLock" data-id="${s.id}">${Number(s.prompt_lock) ? '提示词已锁' : '锁提示词'}</button>
        ${(s.video_url || '').match(/\.(mp4|webm)$/i) ? `<button class="btn sm" data-act="playShot" data-url="${esc(s.video_url)}">▶ 预览</button>
        <button class="dlbtn" data-act="dlVideo" data-url="${esc(s.video_url)}">下载</button>` : ''}
      </div>
      ${chat}
      ${S.perfOpen[s.id] ? perfPanel(s) : ''}
    </div>`;
  }).join('');
  return vhead('07', '生成视频', '逐镜生成。每一镜都记着提示词签名：提示词一改，这一镜的成品自动标记过期，不会拿旧片子凑合集。点缩略图 = 直接播放真片，卡底还有「▶ 预览」「下载」。可选分辨率档位，也可接上一镜末帧做延续参考。',
    `<select id="vTierAll" style="padding:7px 10px;background:#0f151a;border:1px solid var(--line2);border-radius:7px">
       ${[['', '统一：自动档'], ['480p', '统一：480p 快'], ['720p', '统一：720p 标准'], ['1080p', '统一：1080p 高清']]
         .map(([v2, n]) => `<option value="${v2}">${n}</option>`).join('')}
     </select>
     <button class="btn" data-act="applyTierAll">应用到本集</button>
     <button class="btn primary" data-act="generate" data-kind="video">全部生成</button>
     <button class="btn" data-act="goStep" data-step="8">去看成片合集</button>`) + notice + jobbar +
    `<div class="gen">${cards}</div>`;
}

/* ------------------------------------------------ 视图 8 合集 */
async function loadReel() {
  if (!S.pid) return;
  S.reelLoading = true; renderWork();
  try {
    const r = await api('/api/reel?project=' + S.pid);
    S.reel = r; S.reelIdx = Math.min(S.reelIdx, Math.max(0, r.shots.length - 1));
  } catch (e) { toast('合集加载失败：' + e.message); }
  S.reelLoading = false; renderWork();
}

function reelPlay() {
  const v = document.querySelector('.stage video');
  if (v) { if (v.paused) { v.play().catch(() => {}); } else { v.pause(); } return; }
  S.playing = !S.playing;
  clearTimeout(S.reelTimer);
  if (S.playing) reelNext(true);
  renderWork();
}

function reelShotIds() {
  if (!S.reel) return [];
  const scope = S.reelScope || 'all';
  if (scope === 'all') return [];
  const ready = S.reel.shots.filter((s) => s.status === 'done' && /\.(mp4|webm)$/i.test(s.video || ''));
  if (scope === 'ready') return ready.map((s) => s.id);
  return S.reel.shots.filter((s) => (S.reelPick || {})[s.id] && s.status === 'done'
    && /\.(mp4|webm)$/i.test(s.video || '')).map((s) => s.id);
}

function reelGoto(i) {
  if (!S.reel || !S.reel.shots.length) return;
  const s = S.reel.shots[i];
  if ((S.reelScope || 'all') === 'pick' && s) {
    if (!(s.status === 'done' && /\.(mp4|webm)$/i.test(s.video || ''))) return toast('这一镜还没出真片，不能进合集');
    S.reelPick = S.reelPick || {};
    S.reelPick[s.id] = !S.reelPick[s.id];
    renderWork(); return;
  }
  S.reelIdx = Math.max(0, Math.min(S.reel.shots.length - 1, i));
  clearTimeout(S.reelTimer);
  if (S.playing) reelNext(true);
  renderWork();
}

function reelNext(first) {
  if (!S.reel) return;
  const cur = S.reel.shots[S.reelIdx];
  if (!cur) return;
  if (!first) {
    if (S.reelIdx >= S.reel.shots.length - 1) { S.playing = false; renderWork(); return; }
    S.reelIdx++;
    renderWork();
  }
  S.reelTimer = setTimeout(() => { if (S.playing) { reelNext(false); } },
    (S.reel.shots[S.reelIdx] || cur).duration * 1000);
}

function viewReel() {
  const ep = ep0();
  if (!S.reel) {
    if (!S.reelLoading) loadReel();
    return vhead('08', '成片合集', '把已生成的镜头按镜号自动串成一条时间线。', '') +
      `<div class="notice">正在读取时间线…</div>`;
  }
  const r = S.reel;
  const cur = r.shots[S.reelIdx] || r.shots[0];
  if (!cur) return vhead('08', '成片合集', '', '') + `<div class="notice">还没有镜头。</div>`;
  const ratio = (proj().ratio || '9:16');
  const land = ratio === '16:9';
  const dl = (cur.dialogue || []).map((d) => d.sp + '：' + d.line).join('  ·  ');
  const scope = S.reelScope || 'all';
  const pick = S.reelPick || (S.reelPick = {});
  const ready = r.shots.filter((s) => s.status === 'done' && /\.(mp4|webm)$/i.test(s.video || ''));
  const selShots = scope === 'ready' ? ready
    : scope === 'pick' ? r.shots.filter((s) => pick[s.id] && s.status === 'done' && /\.(mp4|webm)$/i.test(s.video || ''))
      : r.shots;
  const selDur = selShots.reduce((a, s) => a + (s.duration || 0), 0);
  const strip = r.shots.map((s, i) => {
    const has = s.status === 'done' && /\.(mp4|webm)$/i.test(s.video || '');
    const on = scope === 'pick' ? !!pick[s.id] : i === S.reelIdx;
    return `<div class="s ${on ? 'on' : ''} ${scope === 'pick' && !has ? 'dis' : ''}" data-act="reelGoto" data-i="${i}">
      <img src="${s.thumb || s.frame}?v=${encodeURIComponent(s.id + '')}"><b>${String(s.idx).padStart(2, '0')}</b>
      ${scope === 'pick' ? `<i class="pickmark">${pick[s.id] ? '✓' : ''}</i>` : ''}</div>`;
  }).join('');
  const generated = S.state.stats.generated, total = S.state.stats.shots;
  const hasVid = cur.status === 'done' && /\.(mp4|webm)$/i.test(cur.video || '');
  const last = S.reelLast || {};
  return vhead('08', '成片合集', `${r.shots.length} 镜 · 总时长 ${r.total}s · 已生成 ${generated}/${total}。选好范围再导出：可以整集串，也可以只串已出的真片，或者手选几镜。`,
    `<button class="btn primary" data-act="reelExport" ${S.reelBusy ? 'disabled' : ''}>${S.reelBusy ? '正在打包…' : '导出 HTML 合集'}</button>
     <button class="btn" data-act="reelFfmpeg" ${S.reelBusy ? 'disabled' : ''}>${S.reelBusy ? '正在合成…' : 'ffmpeg 合成 MP4'}</button>
     <button class="btn ghost" data-act="reelReload">重载时间线</button>`) +
    `<div class="reelscope">
      <label class="tiny">合集范围</label>
      <button class="chip ${scope === 'all' ? 'on' : ''}" data-act="reelScope" data-scope="all">全部 ${r.shots.length} 镜</button>
      <button class="chip ${scope === 'ready' ? 'on' : ''}" data-act="reelScope" data-scope="ready">仅已生成 ${ready.length} 镜</button>
      <button class="chip ${scope === 'pick' ? 'on' : ''}" data-act="reelScope" data-scope="pick">手选镜头</button>
      <span class="tag mono">已选 ${selShots.length} 镜 · ${selDur}s</span>
      <span class="tiny">${scope === 'pick' ? '点下面条带上的格子选/取消（只能选已出真片的）' : '切到「手选镜头」可以只在合集里放某几镜'}</span>
    </div>` +
    (last.html || last.mp4 ? `<div class="notice ok"><b>刚导出的文件</b>
      ${last.html ? `<a class="btn sm primary" href="${last.html}" target="_blank">打开 HTML 合集</a>
      <a class="btn sm" href="${last.html}" download>下载 HTML</a>` : ''}
      ${last.mp4 ? `<a class="btn sm primary" href="${last.mp4}" target="_blank">播放 MP4</a>
      <a class="btn sm" href="${last.mp4}" download>下载 MP4</a>` : ''}</div>` : '') +
    `<div class="player">
      <div class="stagewrap"><div class="stage ${land ? 'land' : ''}">
        ${hasVid
          ? `<video src="${esc(cur.video)}" controls autoplay playsinline></video>`
          : `<img src="${cur.frame}?v=${encodeURIComponent(cur.id + '-') + (S.reelIdx)}">`}
        ${hasVid ? '' : `<div class="sub">${esc(dl || '（无对白 · 环境声）')}</div>`}
      </div></div>
      <div class="playbar">
        <button class="btn sm" data-act="reelPrev">◀ 上一镜</button>
        <button class="btn primary" data-act="reelPlay">${S.playing ? '暂停' : '播放'}</button>
        <button class="btn sm" data-act="reelNextBtn">下一镜 ▶</button>
        ${hasVid ? `<button class="btn sm ghost" data-act="playShot" data-url="${esc(cur.video)}">大屏播放</button>` : ''}
        <span class="tag mono">${String(cur.idx).padStart(2, '0')} / ${r.shots.length}</span>
        <span class="tag">${cur.duration}s</span>
        <span class="tag ${cur.status === 'done' ? 'ok' : 'warn'}">${cur.status === 'done' ? '实拍' : '预演'}</span>
      </div>
      <div class="strip">${strip}</div>
      <div class="hint">播放器直接播真片段（点画面可开大屏、点下方任一格跳到那一镜）。导出的 HTML 已内联真视频，双击即可离线播放。</div>
    </div>`;
}

/* ------------------------------------------------ 右栏 资产 */
const KIND_LABEL = { char: '角色形象', scene: '场景资产', prop: '道具资产' };
const MODE_LABEL = { four: '四视图', single: '单视图' };

function voiceRow(a) {
  const vs = (S.state && S.state.voices) || [];
  const bound = Number(a.voice_id || 0);
  return `<div class="vrow">
    <div class="field"><label>专属音色（全剧固定，改名跟着档案走）</label>
      <select data-act="voiceBind" data-id="${a.id}">
        <option value="0">未绑定 · 用通用备用音色</option>
        ${vs.map((v) => `<option value="${v.id}" ${bound === v.id ? 'selected' : ''}>${esc(v.name)}${v.engine ? ' · ' + esc(v.engine) : ''}</option>`).join('')}
      </select></div>
    <div class="inrow">
      <button class="btn sm ghost" data-act="voiceNew" data-id="${a.id}" data-name="${esc(a.name)}">新建音色</button>
      <button class="btn sm ghost" data-act="voiceSample" data-id="${a.id}" data-vid="${bound}">试听 / 生成合音</button>
    </div>
  </div>`;
}

function assetCard(a, k) {
  const imgs = a.images || [];
  const chosen = a.chosen_url || a.image_url || '';
  const thumbs = imgs.slice(0, 6).map((im) => `
      <div class="athumb ${im.chosen ? 'on' : ''}">
        <img src="${esc(im.url)}" alt="" data-act="zoom" data-url="${esc(im.url)}" loading="lazy">
        <div class="athops">
          <button data-act="chooseImg" data-iid="${im.id}" title="设为锁定图">${im.chosen ? '✓' : '选'}</button>
          <button data-act="delImg" data-iid="${im.id}" title="删除这张">×</button>
        </div>
      </div>`).join('');
  return `<div class="acard ${a.locked ? 'locked' : ''}" data-aid="${a.id}">
    <div class="aface" data-act="zoom" data-url="${esc(chosen)}">
      ${chosen ? `<img src="${esc(chosen)}" alt="${esc(a.name)}" loading="eager">`
               : `<div class="noface"><span>尚未出图</span><em>点「生成」用 qwen_image 2512 出</em></div>`}
      ${a.locked ? '<div class="lockmask">已锁</div>' : ''}
      ${chosen ? '<div class="zoomtip">点击放大</div>' : ''}
    </div>
    <div class="ah"><span class="nm">${esc(a.name)}</span><span class="k">${KIND_LABEL[k] || k}</span>
      <button class="lockbtn ${a.locked ? 'on' : ''}" style="margin-left:auto" data-act="assetLock" data-id="${a.id}">${a.locked ? '🔒' : '🔓'}</button>
    </div>
    ${roText(a.canon, 'asset.canon', a.id, { rows: 3 })}
    <div class="aplbox">
      <div class="apl"><label>生成图片提示词（qwen_image 2512）</label>
        ${roText(a.prompt_maker || '', 'asset.prompt_maker', a.id, { rows: 3 })}</div>
      <button class="btn sm ghost" data-act="makePrompt" data-id="${a.id}">生成图片提示词</button>
    </div>
    <div class="afrow">
      <div class="field"><label>图像前级工作流</label>
        <select data-field="asset.gen_workflow" data-id="${a.id}">
          ${[['qwen_image2512', 'qwen_image2512（内置）'], ['qwen_image_2_1', 'qwen_image_2_1'], ['krea2_turbo', 'krea2_turbo']]
            .map(([v, n]) => `<option value="${v}" ${(a.gen_workflow || 'qwen_image2512') === v ? 'selected' : ''}>${n}</option>`).join('')}
        </select></div>
      <div class="field"><label>视图模式</label>
        <select data-field="asset.view_mode" data-id="${a.id}">
          ${[['four', '四视图'], ['single', '单图'], ['multi', '多视图']]
            .map(([v, n]) => `<option value="${v}" ${(a.view_mode || 'four') === v ? 'selected' : ''}>${n}</option>`).join('')}
        </select></div>
    </div>
    ${k === 'char' ? voiceRow(a) : ''}
    <div class="agenrow">
      ${k === 'char'
        ? `<button class="btn sm" data-act="genImg" data-id="${a.id}" data-mode="four">出四视图</button>
           <button class="btn sm ghost" data-act="genImg" data-id="${a.id}" data-mode="single">单图</button>`
        : `<button class="btn sm" data-act="genImg" data-id="${a.id}" data-mode="single">生成本图</button>`}
      <button class="btn sm ghost" data-act="genImg" data-id="${a.id}" data-mode="reroll">重新生成</button>
      <button class="btn sm ghost" data-act="dl" data-url="${esc(chosen)}">下载</button>
      <button class="btn sm ghost" data-act="assetUpload" data-id="${a.id}">上传</button>
      <button class="btn sm ghost" data-act="assetRename" data-id="${a.id}" data-name="${esc(a.name)}">改名</button>
      <button class="btn sm ghost" data-act="assetReplace" data-id="${a.id}">换图</button>
    </div>
    ${thumbs ? `<div class="athumbs">${thumbs}</div>` : ''}
    <div class="af"><span>${a.locked ? (chosen ? '已锁定：canon + 参考图 双锁' : '仅文本锁：无参考图，一致性不保证') : '未锁定：可用但不保证一致'}</span>
      <span class="n" style="margin-left:auto">${imgs.length} 版</span>
      <button class="btn sm ghost" data-act="assetDelCheck" data-id="${a.id}">删除</button></div>
  </div>`;
}

function renderAssets() {
  const st = S.state;
  if (!st || !st.project) { $('#assets').innerHTML = ''; return; }
  const p = st.project;
  const groups = ['char', 'scene', 'prop'].map((k) => {
    const list = (st.assets || []).filter((a) => a.kind === k);
    return `<div class="agroup">
      <h4>${KIND_LABEL[k]} <span class="n">${list.length}</span>
        <span style="margin-left:auto" class="gbtns">
          <button class="btn sm ghost" data-act="genAll" data-kind="${k}">整组出图</button>
          <button class="btn sm ghost" data-act="addAsset" data-kind="${k}">+</button>
        </span></h4>
      ${list.map((a) => assetCard(a, k)).join('') || '<div class="hint">暂无</div>'}
    </div>`;
  }).join('');

  const locked = (st.assets || []).filter((a) => a.locked).length;
  const withImg = (st.assets || []).filter((a) => a.image_url).length;
  const imgJob = (st.jobs || []).find((j) => j.kind === 'image' && (j.status === 'running' || j.status === 'queued'));
  $('#assets').innerHTML = `
    <div class="targetcard ${p.target_locked ? 'on' : ''}" data-act="goStep" data-step="0">
      <div class="tcarow"><span class="tag ${p.target_locked ? 'ok' : 'warn'}">${p.target_locked ? '目标已锁' : '目标未锁'}</span>
        <b>${esc(p.title || '未命名')}</b></div>
      <div class="tcgrid">
        <div><span>题材</span><b>${esc(p.genre || '—')}</b></div>
        <div><span>画幅</span><b>${esc(p.ratio || '—')}</b></div>
        <div><span>集数</span><b>${p.episode_count || '—'} 集</b></div>
        <div><span>单集</span><b>${esc(p.episode_length || '—')}</b></div>
        <div style="grid-column:1/-1"><span>风格锁</span><b>${esc((p.style_lock || '—').slice(0, 34))}</b></div>
      </div>
    </div>
    <h4>资产库 / ASSET LIBRARY</h4>
    <div class="hint" style="margin-bottom:8px">出图用本地 qwen_image_2512；角色可出四视图。选中的那张就是锁定的参考图，④ H3 会把它作为图 1/图 2 喂进去，并在提示词里写明「第几张图是谁」。</div>
    ${imgJob ? `<div class="jobbar"><div class="jb-top"><span>出图中 ${esc(imgJob.message || '')}</span><b>${imgJob.progress || 0}%</b></div>
      <div class="jb"><i style="width:${imgJob.progress || 0}%"></i></div>
      <div class="jb-log">${esc(((imgJob.log || []).slice(-1)[0] || {}).msg || '')}</div></div>` : ''}
    <div class="astats"><span>${locked}/${(st.assets || []).length} 已锁</span><span>${withImg} 张主图</span>
      <button class="btn sm ghost" style="margin-left:auto" data-act="genAll">全部出图</button></div>
    ${groups}`;
}

/* ------------------------------------------------ 资产库（全页） */
function viewLibrary() {
  const st = S.state;
  const assets = (st.assets || []);
  const q = (S.libQuery || '').trim();
  const kind = S.libKind || '';
  const counts = { char: 0, scene: 0, prop: 0 };
  assets.forEach((a) => { if (counts[a.kind] !== undefined) counts[a.kind]++; });
  const locked = assets.filter((a) => a.locked).length;
  const withImg = assets.filter((a) => a.image_url || a.chosen_url).length;
  const tabs = [['', '全部', assets.length], ['char', '角色形象', counts.char],
    ['scene', '场景资产', counts.scene], ['prop', '道具资产', counts.prop]]
    .map(([k, n, c]) => `<div class="lt ${kind === k ? 'on' : ''}" data-act="libKind" data-kind="${k}">${n} <b>${c}</b></div>`).join('');
  const kinds = kind ? [kind] : ['char', 'scene', 'prop'];
  const secs = kinds.map((k) => {
    const list = assets.filter((a) => a.kind === k && (!q || (a.name + (a.canon || '')).toLowerCase().includes(q.toLowerCase())));
    if (!list.length) return '';
    return `<h4 class="libsec">${KIND_LABEL[k]} <span class="n">${list.length}</span>
        <span class="gbtns"><button class="btn sm ghost" data-act="genAll" data-kind="${k}">整组出图</button>
        <button class="btn sm ghost" data-act="addAsset" data-kind="${k}">+ 新增</button></span></h4>
      <div class="assetsview">${list.map((a) => assetCard(a, k)).join('')}</div>`;
  }).join('');
  return vhead('资产库', 'ASSET LIBRARY · 角色 / 场景 / 道具 三分类', 
    `共 ${assets.length} 个资产（角色形象 ${counts.char} · 场景资产 ${counts.scene} · 道具资产 ${counts.prop}），已锁 ${locked}，有主图 ${withImg}。锁定的顺是一张参考图 + 一段 canon，④ H3 按「图N = 谁」写进提示词。`,
    `<button class="btn primary" data-act="genAll">全部出图</button>
     <button class="btn" data-act="batchPrompt">批量生成提示词</button>
     <button class="btn" data-act="batchImg">批量生成图片（缺图优先）</button>
     <button class="btn" data-act="goStep" data-step="7">去生成视频</button>
     <button class="btn ghost" data-act="goStep" data-step="0">回目标</button>`) +
    `<div class="libtabs">${tabs}</div>
     <input id="libSearch" class="libsearch" placeholder="搜资产名 / 设定关键词（例如 阿棠、厨房、糖）" value="${esc(q)}">
     ${assets.length ? secs : `<div class="notice">还没有资产。先在 ① 目标里生成资产，或点右上「新增」手写一个。</div>`}`;
}

/* ------------------------------------------------ 主导航：项目管理 / 音乐 / 设置 */
function viewProjects() {
  const st = S.state || {};
  const list = st.projects || [];
  const cur = st.project;
  const cards = list.map((p) => {
    const on = cur && cur.id === p.id;
    return `<div class="pcard ${on ? 'on' : ''}">
      <div class="ph"><b>${esc(p.title || '未命名项目')}</b>
        <span class="tag ${p.target_locked ? 'ok' : 'warn'}">${p.target_locked ? '目标已锁' : '目标未锁'}</span>
        ${on ? '<span class="tag blue">当前打开</span>' : ''}</div>
      <div class="tiny pdesc">${esc((p.premise || '未写创意').slice(0, 72))}</div>
      <div class="pmeta"><span>${esc(p.genre || '—')}</span><span>${esc(p.ratio || '—')}</span>
        <span>${p.episode_count || 0} 集</span><span>${esc(p.duration_range || p.episode_length || '')}</span>
        <span>${Number(p.fps) || 30}fps · ${esc(p.quality || '电影级')}</span></div>
      <div class="pact">
        <button class="btn sm primary" data-act="projOpen" data-id="${p.id}">${on ? '继续制作' : '打开'}</button>
        <button class="btn sm ghost" data-act="projEdit" data-id="${p.id}" data-title="${esc(p.title)}">修改</button>
        <button class="btn sm ghost" data-act="projDel" data-id="${p.id}" data-title="${esc(p.title)}">删除</button>
      </div>
    </div>`;
  }).join('');
  return vhead('P', '项目管理', '每个项目是一套独立世界：剧本、分镜、素材、成片、音乐全部按项目隔离，切换项目不会串台。',
    `<button class="btn primary" data-act="newProject">+ 新建项目</button>`) +
    `<div class="pgrid">${cards || '<div class="notice">还没有项目。点右上「+ 新建项目」，目标锁定字段一次填完。</div>'}</div>`;
}

function musicCard(m, compact) {
  const ep = (S.state.episodes || []).find((e) => e.id === Number(m.linked_ep));
  const stTxt = { draft: '未生成', queued: '排队中', running: '生成中', done: '已完成', failed: '失败', unconfigured: '接口未配置' }[m.status] || m.status;
  return `<div class="mrow ${compact ? 'cp' : ''}">
    <div class="mh"><b>${esc(m.name || '未命名配乐')}</b>
      <span class="tag ${m.status === 'done' ? 'ok' : m.status === 'failed' || m.status === 'unconfigured' ? 'bad' : 'blue'}">${esc(stTxt)}</span>
      ${ep ? `<span class="tag">EP${String(ep.idx).padStart(2, '0')}</span>` : ''}</div>
    <div class="tiny">${esc(m.style || '')} · ${esc(m.mood || '')} · ${m.duration || 30}s · ${m.bpm || 90}BPM</div>
    ${m.url ? `<audio controls preload="none" src="${esc(m.url)}"></audio>` : `<div class="hint">${m.status === 'unconfigured' ? '本地音乐接口未配置，生成会被拒绝，不会用占位音频冒充' : '还没有音频'}</div>`}
    <div class="mact">
      <button class="btn sm" data-act="musicGen" data-id="${m.id}">${m.url ? '重新生成' : '生成'}</button>
      <button class="btn sm ghost" data-act="musicRename" data-id="${m.id}" data-name="${esc(m.name)}">改名</button>
      <button class="btn sm ghost" data-act="musicLink" data-id="${m.id}">关联成片</button>
      ${m.url ? `<a class="btn sm ghost" href="${esc(m.url)}" download>导出</a>` : ''}
      <button class="btn sm ghost" data-act="musicDel" data-id="${m.id}" data-name="${esc(m.name)}">删除</button>
    </div>
  </div>`;
}

function viewMusicCreate() {
  const st = S.state;
  if (!st || !st.project) return emptyView();
  const ms = st.music || [];
  const conf = S.set ? S.set.music_configured : true;
  const f = S.musicForm || {};
  return vhead('M', '音乐创作', '只走本地 ComfyUI 音乐工作流（PRD 明确不接线上音乐服务商）。生成结果自动进资源库，按项目隔离。',
    `<button class="btn ghost" data-act="goMain" data-view="12">去音乐播放</button>`) +
    (!conf ? `<div class="notice err">音乐生成接口未配置：系统设置 → 音乐生成接口填本地 ComfyUI 音乐工作流文件名，否则点生成会被明确拒绝（不会用占位音频冒充）。</div>` : '') +
    `<div class="card">
      <h3>新建配乐 / NEW TRACK</h3>
      <div class="frow">
        <div class="field"><label>曲名</label><input id="mName" value="${esc(f.name || '')}" placeholder="EP01 主题曲"></div>
        <div class="field"><label>类型</label><select id="mKind">
          ${[['bgm', 'BGM 配乐'], ['theme', '主题曲'], ['sfx', '音效']].map(([v, n]) => `<option value="${v}" ${f.kind === v ? 'selected' : ''}>${n}</option>`).join('')}
        </select></div>
      </div>
      <div class="frow">
        <div class="field"><label>风格</label><input id="mStyle" value="${esc(f.style || '')}" placeholder="国风弦乐 / 电子 / 民谣"></div>
        <div class="field"><label>情绪</label><select id="mMood">
          ${['温馨', '虐心', '紧张', '悬疑', '燃', '治愈', '搞笑'].map((x) => `<option ${f.mood === x ? 'selected' : ''}>${x}</option>`).join('')}
        </select></div>
      </div>
      <div class="frow">
        <div class="field"><label>时长（秒，1-120）</label><input id="mDur" type="number" min="1" max="120" value="${f.duration || 30}"></div>
        <div class="field"><label>节拍 BPM</label><input id="mBpm" type="number" min="40" max="200" value="${f.bpm || 90}"></div>
      </div>
      <div class="field"><label>创作描述（写进 ComfyUI 音乐工作流）</label>
        <textarea id="mPrompt" rows="3" placeholder="钢琴单音进，弦乐渐起，中段留白两拍，结尾收在女主呼吸声上">${esc(f.prompt || '')}</textarea></div>
      <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn primary" data-act="musicNew">生成音乐</button></div>
    </div>
    <h4 class="libsec">本集素材库 / MUSIC <span class="n">${ms.length}</span></h4>
    ${ms.length ? ms.map((m) => musicCard(m)).join('') : '<div class="notice">还没有配乐。填完上面点「生成音乐」，成功后自动入库。</div>'}`;
}

function viewMusicPlay() {
  const st = S.state;
  if (!st || !st.project) return emptyView();
  const all = (st.music || []);
  const f = S.musicFilter || 'all';
  const list = all.filter((m) => f === 'all' || m.kind === f);
  const done = list.filter((m) => m.url);
  const cur = done[Math.min(S.playIdx, Math.max(0, done.length - 1))];
  const tabs = [['all', '全部'], ['bgm', 'BGM'], ['theme', '主题曲'], ['sfx', '音效']]
    .map(([k, n]) => `<div class="lt ${f === k ? 'on' : ''}" data-act="musicFilter" data-kind="${k}">${n}</div>`).join('');
  return vhead('♪', '音乐播放', `共 ${all.length} 条，可播放 ${done.length} 条。成片合集可直接取这里的 BGM。`, '') +
    `<div class="libtabs">${tabs}</div>
     ${cur ? `<div class="card player">
       <h3>${esc(cur.name || '未命名配乐')} <small>${esc(cur.style || '')} · ${esc(cur.mood || '')}</small></h3>
       <audio controls autoplay src="${esc(cur.url)}" style="width:100%"></audio>
       <div class="tiny" style="margin-top:6px">${cur.duration || 30}s · ${cur.bpm || 90}BPM · ${esc(cur.status)}</div>
     </div>` : '<div class="notice">还没有可播放的音乐。先去音乐创作生成，或检查本地音乐接口是否已配置。</div>'}
     <div class="mlist">${list.map((m, i) => `
       <div class="mprow ${cur && cur.id === m.id ? 'on' : ''}" data-act="musicPick" data-url="${esc(m.url || '')}" data-idx="${i}">
         <b>${esc(m.name || '未命名')}</b><span>${esc(m.style || '')} · ${esc(m.mood || '')} · ${m.duration || 30}s</span>
         ${m.linked_ep ? `<span class="tag ok">已入成片</span>` : ''}
         <span class="tag ${m.status === 'done' ? 'ok' : ''}">${m.status === 'done' ? '可播放' : esc(m.status)}</span>
       </div>`).join('') || '<div class="hint">没有匹配的音乐</div>'}</div>`;
}

const SET_NAV = [
  ['text', 'AI文本模型', '💬'],
  ['video', '视频生成接口', '🎬'],
  ['image', '图片生成接口', '🖼'],
  ['music', '音乐生成接口', '🎵'],
  ['default', '默认配置', '⚙'],
  ['update', '系统更新', '⬆'],
  ['about', '关于系统', 'ⓘ'],
];
const SET_TEXT_PROVIDERS = [
  ['aliyun', '阿里百炼', '通义千问系列大模型，支持多种规模的文本生成', 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen-plus',
   ['qwen-plus', 'qwen-max', 'qwen-turbo', 'qwen2.5-72b-instruct', 'qwen3-max']],
  ['volc', '火山引擎', '字节跳动豆包系列大模型', 'https://ark.cn-beijing.volces.com/api/v3', 'doubao-pro-32k',
   ['doubao-pro-32k', 'doubao-pro-128k', 'doubao-lite-32k', 'doubao-seed-1-6', 'doubao-1-5-pro-32k']],
  ['deepseek', 'DeepSeek', 'DeepSeek系列大模型，擅长代码和推理', 'https://api.deepseek.com/v1', 'deepseek-chat',
   ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-pro']],
  ['ollama', '本地 Ollama', '运行在本地的大模型服务，需要安装Ollama', 'http://127.0.0.1:11434/v1', 'qwen2.5:7b',
   ['qwen2.5:7b', 'qwen3:8b', 'llama3.1:8b', 'deepseek-r1:7b']],
  ['vllm', '本地 vLLM', 'vLLM 部署的大模型服务（OpenAI 兼容接口），支持 Qwen3 系列', 'http://127.0.0.1:8000/v1', 'Qwen3-8B',
   ['Qwen3-8B', 'Qwen3-32B']],
  ['openai', 'OpenAI通用', 'OpenAI 兼容协议渠道，可自定义任意兼容服务（地址以 /v1 结尾）', 'https://api.openai.com/v1', 'gpt-4o-mini',
   ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini']],
];
const SET_VIDEO_PROVIDERS = [
  ['volc', '火山引擎', '字节跳动视频生成服务，支持高质量视频生成', 'https://ark.cn-beijing.volces.com/api/v3'],
  ['bailian', '阿里百炼', '阿里云视频生成服务', 'https://dashscope.aliyuncs.com/api/v1'],
  ['local', '本地 ComfyUI', '本地部署的ComfyUI工作流，支持自定义工作流', 'http://127.0.0.1:8188'],
];
const SET_IMAGE_PROVIDERS = [
  ['volc', '火山引擎', '字节跳动图片生成服务，支持多种风格', 'https://ark.cn-beijing.volces.com/api/v3'],
  ['bailian', '阿里百炼', '阿里云通义万相图片生成', 'https://dashscope.aliyuncs.com/api/v1'],
  ['local', '本地 ComfyUI', '本地部署ComfyUI图片生成工作流', 'http://127.0.0.1:8188'],
  ['openai', 'OpenAI通用', 'OpenAI 兼容协议渠道（/v1/images/generations 标准图片接口）', 'https://api.openai.com/v1'],
];

function sfi(key, label, opts = {}) {
  const v = S.set ? (S.set[key] ?? '') : '';
  if (opts.select) {
    return `<div class="sfi"><label>${label}</label><select data-set="${key}">
      ${opts.select.map(([val, name]) => `<option value="${val}" ${String(v) === String(val) ? 'selected' : ''}>${name}</option>`).join('')}
    </select></div>`;
  }
  return `<div class="sfi"><label>${label}</label>
    <input data-set="${key}" type="${opts.type || 'text'}" value="${esc(v)}" placeholder="${esc(opts.ph || '')}"></div>`;
}

function inlineAddr(key, base) {
  const v = S.set && S.set[key] ? S.set[key] : base;
  return `<div class="sfi"><label>API地址</label><div class="inline">
    <input data-set="${key}" value="${esc(v)}">
    <a class="btn-mini" data-act="setReset" data-key="${key}" data-val="${esc(base)}">重置</a></div></div>`;
}

/* API Key：带显示/隐藏眼睛 */
function eyeSvg(open) {
  const base = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">';
  const eyeball = '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.8"/>';
  return base + eyeball + (open ? '' : '<path d="M3 3l18 18" stroke="#7d828c"/>') + '</svg>';
}

function keyField() {
  const v = S.set && S.set.llm_api_key ? S.set.llm_api_key : '';
  const set = S.set && S.set.llm_key_set;
  return `<div class="sfi"><label>API Key</label><div class="inline">
    <input data-set="llm_api_key" type="${S.showKey ? 'text' : 'password'}" value="${esc(v)}"
      placeholder="${esc(set ? '已配置，留空不改' : 'sk-...（本地引擎可留空）')}">
    <a class="eye" data-act="toggleKey" title="${S.showKey ? '隐藏' : '显示'}">${eyeSvg(!!S.showKey)}</a></div></div>`;
}

/* 默认模型：优先用 /models 自动拉取的列表，拉不到用供应商内置候选 */
function modelField(id, cur, fallback) {
  const cached = (S.setModels || {})[id] || [];
  const opts = cached.length ? cached : (fallback || []);
  const list = (cur && opts.indexOf(cur) < 0) ? [cur].concat(opts) : opts;
  const busy = S.modelsLoading === id;
  return `<div class="sfi"><label>默认模型
    <a class="mini" data-act="fetchModels" data-id="${id}">${busy ? '获取中…' : '自动获取模型'}</a></label>
    ${list.length ? `<select data-set="llm_model">${list.map((m) => `<option value="${esc(m)}" ${String(cur) === String(m) ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>` : `<input data-set="llm_model" value="${esc(cur)}" placeholder="点「自动获取模型」拉取列表">`}
    <div class="hint">${cached.length ? `已从接口拉取 ${cached.length} 个可用模型` : '未拉取时用内置候选，点「自动获取模型」用当前 API地址 + Key 拉真实列表'}</div></div>`;
}

function sectionCard(title, desc, inner) {
  return `<div class="section-card"><div class="section-title"><h3>${title}</h3>${desc ? `<p>${desc}</p>` : ''}</div>${inner || ''}</div>`;
}

function provCard(kind, id, name, desc, active, cfg, icon) {
  return `<div class="provider-card ${active ? 'active' : ''}">
    <div class="provider-header" data-act="setProvider" data-kind="${kind}" data-id="${id}">
      <div class="provider-info">
        <div class="provider-icon">${icon || '⚙'}</div>
        <div><div class="provider-name">${name}</div><div class="provider-desc">${desc}</div></div>
      </div>
      ${kind === 'text' ? `<div class="sw ${active ? 'on' : ''}"><i></i></div>` : ''}
    </div>
    ${active ? `<div class="provider-config">${cfg}</div>` : ''}
  </div>`;
}

function saveBar(kind, label) {
  return `<div class="save-bar"><button class="btn-s p" data-act="setSave">保存配置</button>
    ${kind ? `<button class="btn-s" data-act="setTest" data-kind="${kind}">测试连接</button>` : ''}
    ${label || ''}</div>`;
}

/* 渲染前把已输入但未保存的表单值收回状态，避免重绘丢失 */
function collectSetForm() {
  if (!S.set) return;
  $$('.setpage [data-set]').forEach((f) => {
    if (f.type === 'hidden') return;
    S.set[f.dataset.set] = f.value;
  });
}

async function fetchModelsFor(id) {
  const p = SET_TEXT_PROVIDERS.find((x) => x[0] === id) || [];
  collectSetForm();
  const base = (S.set && S.set.llm_base_url) || p[3] || '';
  const key = (S.set && S.set.llm_api_key) || '';
  S.modelsLoading = id;
  renderWork();
  let r = null;
  try {
    r = await api('/api/settings/models', { method: 'POST', body: { base: base, key: key, provider: id } });
  } catch (e) {
    S.modelsLoading = '';
    toast('模型列表获取失败：' + e.message);
    return renderWork();
  }
  S.modelsLoading = '';
  if (r && r.ok && (r.models || []).length) {
    S.setModels = S.setModels || {};
    S.setModels[id] = r.models;
    if (!S.set.llm_model || r.models.indexOf(S.set.llm_model) < 0) {
      const prefer = p[4];
      S.set.llm_model = r.models.indexOf(prefer) >= 0 ? prefer : r.models[0];
    }
    toast('已获取 ' + r.models.length + ' 个模型：' + r.models.slice(0, 3).join(' / ') + (r.models.length > 3 ? ' …' : ''));
  } else {
    S.setModelsTried = S.setModelsTried || {};
    S.setModelsTried[id] = 1;
    toast('模型列表获取失败：' + ((r && r.msg) || '接口无返回'));
  }
  return renderWork();
}

/* 进入文本模型页 / 切供应商时自动拉一次（无 Key 且非本地引擎则不打网络） */
function maybeAutoModels() {
  const cur = S.set && S.set.llm_provider;
  if (!cur || S.modelsLoading) return;
  if (S.setModels && S.setModels[cur]) return;
  S.setModelsTried = S.setModelsTried || {};
  if (S.setModelsTried[cur]) return;
  const hasKey = ((S.set && S.set.llm_api_key) || '').trim();
  const local = cur === 'ollama' || cur === 'vllm';
  if (!hasKey && !local) return;
  S.setModelsTried[cur] = 1;
  fetchModelsFor(cur);
}

function viewSettings() {
  if (!S.set) {
    return `<div class="setpage"><div class="page-header"><div class="page-title">系统设置</div>
      <div class="page-desc">配置AI模型、视频生成接口和系统参数</div></div>
      <div class="notice">正在读取配置…</div></div>`;
  }
  const tab = S.setTab || 'text';
  const s = S.set;
  const nav = SET_NAV.map(([k, n, ic]) => `<div class="nav-item ${tab === k ? 'active' : ''}" data-act="setTab" data-tab="${k}">
    <span class="ni">${ic}</span><span>${n}</span></div>`).join('');
  let content = '';

  if (tab === 'text') {
    const cur = s.llm_provider || 'deepseek';
    const list = SET_TEXT_PROVIDERS.map(([id, name, desc, base, model, models], i) => {
      const active = cur === id;
      const icon = ['💬', '🤖', '🧠', '🦙', '⚡', '🔌'][i];
      const cfg = `${inlineAddr('llm_base_url', base)}
        ${keyField()}
        ${modelField(id, s.llm_model, models)}
        ${sfi('llm_temperature', '温度 (Temperature)', { ph: '0.7' })}
        ${sfi('llm_max_tokens', '最大Token数', { ph: '4096' })}
        <div class="provider-actions"><button class="btn-s" data-act="setTest" data-kind="text">测试连接</button></div>`;
      return provCard('text', id, name, desc, active, cfg, icon);
    }).join('');
    content = `<input type="hidden" data-set="llm_provider" value="${esc(cur)}">
      ${sectionCard('AI文本模型配置', '配置用于小说生成、剧本创作、分镜描述的大语言模型', `<div class="provider-list">${list}</div>`)}
      ${sectionCard('配音通道（可选）', 'PRD 八：整集一条音频、音色绑定角色；本机没有 TTS 时只显示「未接入」，绝不假装配音。',
        sfi('tts_base', 'TTS Base URL', { ph: '留空 = 未接入，生成合音会被明确拒绝' }) + sfi('tts_key', 'TTS Key', { type: 'password' }))}
      ${saveBar('text')}`;
  } else if (tab === 'video') {
    const cur = s.video_api_mode || 'local';
    const list = SET_VIDEO_PROVIDERS.map(([id, name, desc, base]) => {
      const active = cur === id;
      const key = id === 'local' ? 'comfy_base' : 'video_api_base';
      const icon = id === 'local' ? '🖥' : (id === 'volc' ? '🚀' : '☁️');
      const cfg = `${inlineAddr(key, base)}
        ${id === 'local' ? '' : sfi('video_api_key', 'API Key', { type: 'password', ph: '线上接口才需要' })}
        <div class="provider-actions"><button class="btn-s" data-act="setTest" data-kind="video">测试连接</button></div>`;
      return provCard('video', id, name, desc, active, cfg, icon);
    }).join('');
    content = `<input type="hidden" data-set="video_api_mode" value="${esc(cur)}">
      ${sectionCard('视频生成接口配置', '配置用于根据分镜描述生成视频片段的AI服务', `<div class="provider-list">${list}</div>`)}
      ${sectionCard('视频生成高级参数', '工作流文件、采样与 ffmpeg：留空即用内置 MiniMax H3 参考生视频',
        sfi('video_workflow', '工作流文件名（留空 = 内置 H3）') + sfi('video_steps', 'H3 采样步数（8 质量 / 4 快）') +
        sfi('video_scale', 'H3 分辨率缩放（1.0 满档 / 0.7 快速验证）') + sfi('ffmpeg_path', 'ffmpeg.exe 路径（可选，用于合成 MP4）'))}
      ${saveBar('video')}`;
  } else if (tab === 'image') {
    const cur = s.image_api_mode || 'local';
    const list = SET_IMAGE_PROVIDERS.map(([id, name, desc, base]) => {
      const active = cur === id;
      const key = id === 'local' ? 'comfy_base' : 'image_api_base';
      const icon = id === 'local' ? '🖥' : (id === 'volc' ? '🚀' : (id === 'bailian' ? '☁️' : '🔌'));
      const cfg = `${inlineAddr(key, base)}
        ${id === 'local' ? '' : sfi('image_api_key', 'API Key', { type: 'password', ph: '线上接口才需要' })}
        <div class="provider-actions"><button class="btn-s" data-act="setTest" data-kind="image">测试连接</button></div>`;
      return provCard('image', id, name, desc, active, cfg, icon);
    }).join('');
    content = `<input type="hidden" data-set="image_api_mode" value="${esc(cur)}">
      ${sectionCard('图片生成接口配置', '配置用于生成角色形象、场景和道具图片的AI服务', `<div class="provider-list">${list}</div>`)}
      ${sectionCard('图片生成高级参数', '前级工作流只有在 workflows/ 下存在同名 .json 时才会真正调用，否则如实报「未安装」。',
        sfi('default_image_wf', '默认前级工作流', { select: [['qwen_image2512', 'qwen_image2512'], ['qwen_image_2_1', 'qwen_image_2_1'], ['krea2_turbo', 'krea2_turbo']] }) +
        sfi('comfy_input_dir', 'ComfyUI input 目录') + sfi('comfy_output_dir', 'ComfyUI output 目录'))}
      ${saveBar('image')}`;
  } else if (tab === 'music') {
    const cfg = `${inlineAddr('music_api_base', 'http://127.0.0.1:8188')}
      <div class="provider-actions"><button class="btn-s" data-act="setTest" data-kind="music">测试连接</button></div>`;
    content = `<input type="hidden" data-set="music_api_mode" value="local">
      ${sectionCard('音乐生成接口配置', '配置用于AI音乐生成的ComfyUI服务（PRD 九：只允许本地 ComfyUI，不开放线上服务商）',
        `<div class="provider-list">${provCard('music', 'local', '本地 ComfyUI', '本地部署的ComfyUI音乐生成工作流', true, cfg, '🎹')}</div>`)}
      ${sectionCard('音乐工作流', `当前状态：${s.music_configured ? '已配置' : '未配置'}。未配置时点「生成音乐」会被后端直接拒绝，不产生占位音频。`,
        sfi('music_workflow', '工作流文件名', { ph: '例如 music_suno.json（留空 = 未配置）' }))}
      ${saveBar('music')}`;
  } else if (tab === 'default') {
    content = `${sectionCard('默认配置', '设置系统各环节的默认AI模型和服务',
      sfi('default_novel_model', '小说生成默认模型', { ph: '留空 = 用文本模型默认' }) +
      sfi('default_script_model', '剧本生成默认模型', { ph: '留空 = 用文本模型默认' }) +
      sfi('default_board_model', '分镜生成默认模型', { ph: '留空 = 用文本模型默认' }) +
      sfi('default_video_service', '视频生成默认服务', { select: [['local', '本地 ComfyUI'], ['volc', '火山引擎'], ['bailian', '阿里百炼']] }) +
      sfi('default_image_service', '图片生成默认服务', { select: [['local', '本地 ComfyUI'], ['volc', '火山引擎'], ['bailian', '阿里百炼']] }) +
      sfi('default_music_service', '音乐生成默认服务', { select: [['local', '本地 ComfyUI']] }))}
      ${sectionCard('并发控制', '控制图片和视频生成的并发数量，避免本地资源过载。建议根据电脑配置调整：低配(1-2)、中配(2-3)、高配(3-5)。',
        `<div class="sfi"><label>图片生成并发数</label><div class="inline num"><input data-set="image_concurrency" type="number" min="1" max="8" value="${esc(s.image_concurrency || '1')}"><span class="unit">个并发</span></div></div>
         <div class="sfi"><label>视频生成并发数</label><div class="inline num"><input data-set="video_concurrency" type="number" min="1" max="4" value="${esc(s.video_concurrency || '1')}"><span class="unit">个并发</span></div></div>`)}
      <div class="save-bar"><button class="btn-s p" data-act="setSave">保存配置</button>
        <button class="btn-s" data-act="setDefaults">恢复默认</button></div>`;
  } else if (tab === 'update') {
    content = `${sectionCard('系统更新', '输入校验码检查并下载最新版本更新',
      `<div class="sfi"><label>校验码（可选）</label><input id="setCode" placeholder="留空 = 仅检查本地版本"></div>
       <div class="save-bar"><button class="btn-s p" data-act="checkUpdate">检查更新</button></div>
       <div class="verline">当前版本: v${esc(s.version || '')}</div>`)}`;
  } else {
    content = `${sectionCard('关于系统', '',
      `<div class="about-title">AI视频创作系统</div>
       <div class="about-ver">版本 ${esc(s.version || '')}</div>
       <p class="about-desc">面向个人创作者的 AI 辅助视频创作工具。从创意到成片全流程自动化：小说生成、剧本创作、分镜设计、调度、提示词与视频生成。通过资产复用机制（角色形象、场景、道具）确保多段视频间的角色一致性，帮助创作者产出 AI 短剧、连续剧与长视频内容。</p>
       <div class="about-sub">技术栈</div>
       <div class="tags">${['Python 3', 'http.server', '原生 JS', 'SQLite', 'ComfyUI', 'MiniMax H3', 'ffmpeg'].map((x) => `<span class="ptag">${x}</span>`).join('')}</div>`)}`;
  }

  return `<div class="setpage">
    <div class="page-header"><div class="page-title">系统设置</div>
      <div class="page-desc">配置AI模型、视频生成接口和系统参数</div></div>
    <div class="settings-layout"><div class="settings-nav">${nav}</div><div class="settings-content">${content}</div></div>
  </div>`;
}

/* ------------------------------------------------ 控制台 */
function renderConsole() {
  const st = S.state;
  if (!st) return;
  const jobs = (st.jobs || []).slice(0, 8);
  const env = st.config || {};
  const tabs = ['jobs', 'env'];
  const tabHtml = tabs.map((t) => `<div class="tab ${S.consoleTab === t ? 'on' : ''}" data-act="consoleTab" data-tab="${t}">${t === 'jobs' ? '任务队列' : '环境配置'}</div>`).join('');
  let body = '';
  if (S.consoleTab === 'jobs') {
    body = jobs.length ? jobs.map((j) => {
      const cls = j.status === 'running' ? 'run' : j.status === 'failed' ? 'failed' : j.status === 'done' ? 'done' : '';
      const logs = (j.log || []).slice(-4).map((l) => `<div class="lg"><span>${esc(l.t)}</span>${esc(l.msg)}</div>`).join('');
      const kindName = { outline: '大纲', novel: '正文', script: '剧本', board: '分镜', blocking: '调度', prompt: '提示词', video: '生成', optimize: '深度优化' }[j.kind] || j.kind;
      return `<div class="job ${cls}">
        <div class="jh"><b>#${j.id} ${kindName}</b><span class="tag ${j.status === 'done' ? 'ok' : j.status === 'failed' ? 'bad' : 'blue'}">${esc(j.status)}</span>
          <span class="pc">${j.progress || 0}%</span><span class="tiny">${esc(j.message || '')}</span></div>
        <div class="bar"><i style="width:${j.progress || 0}%"></i></div>
        ${logs}
      </div>`;
    }).join('') : '<div class="hint">暂无任务。点任意一步的「生成 / 深度优化」，这里会显示逐条日志。</div>';
  } else {
    body = `<div class="env">
      <div class="field"><label>引擎模式</label>
        <select id="cfgMode">
          <option value="auto" ${env.engine_mode === 'auto' ? 'selected' : ''}>auto（有 Key 先用模型）</option>
          <option value="local" ${env.engine_mode === 'local' ? 'selected' : ''}>local（只用本地引擎）</option>
          <option value="llm" ${env.engine_mode === 'llm' ? 'selected' : ''}>llm（只用模型）</option>
        </select></div>
      <div class="field"><label>LLM Base URL</label><input id="cfgBase" value="${esc(env.llm_base_url)}"></div>
      <div class="field"><label>LLM 模型</label><input id="cfgModel" value="${esc(env.llm_model)}"></div>
      <div class="field"><label>API Key（${env.llm_key_set ? '已配置 ' + esc(env.llm_key_mask) : '未配置'}）</label>
        <input id="cfgKey" placeholder="sk-...（留空不修改）"></div>
      <div class="field"><label>ComfyUI 地址</label><input id="cfgComfy" value="${esc(env.comfy_base)}"></div>
      <div class="field"><label>视频工作流（留空 = 内置 H3 现拼）</label><input id="cfgWf" value="${esc(env.video_workflow)}" placeholder="留空即用内置 MiniMax H3 参考生视频"></div>
      <div class="field"><label>H3 采样步数（8=质量档；4=速度快一倍）</label><input id="cfgSteps" value="${esc(env.video_steps || '8')}"></div>
      <div class="field"><label>H3 分辨率缩放（1.0=满档；0.7≈快速验证）</label><input id="cfgScale" value="${esc(env.video_scale || '1.0')}"></div>
      <div class="field"><label>ffmpeg.exe 路径（可选，用于合成 MP4）</label><input id="cfgFf" value="${esc(env.ffmpeg_path)}"></div>
      <div class="field"><label>&nbsp;</label>
        <button class="btn primary" data-act="cfgSave">保存并重测连接</button></div>
    </div>
    <div class="hint" style="margin-top:8px">模型状态：${esc(st.server.llm.msg)}<br>ComfyUI：${esc(st.server.comfy.msg)}<br>
    数据文件：data/studio.db · 产物：data/media · 合集导出：data/exports</div>`;
  }
  $('#console').innerHTML = `
    <div class="chd"><div class="tabs">${tabHtml}</div>
      <div class="right"><span>${jobs.filter((j) => j.status === 'running').length} 个任务运行中</span>
        <button class="btn sm ghost" data-act="toggleConsole">${S.consoleBig ? '收起' : '展开'}</button></div>
    </div>
    <div class="cbody">${body}</div>`;
}

function emptyView(msg) {
  const ep = ep0();
  if (!S.state || !S.state.project) {
    return `<div class="notice err">还没有项目。点左上角「+ 新项目」建一个。</div>`;
  }
  const n = ep ? `EP${String(ep.idx).padStart(2, '0')}「${esc(ep.title || '')}」` : '这一集';
  return `${vhead('', '这一集还是空的', `${n} 还没有走到这一步。分集是独立分组：每集的正文 → 剧本 → 分镜 → 调度 → 提示词 → 视频，各走各的，互不覆盖。`,
    `<button class="btn primary" data-act="generate" data-kind="novel">先写本集正文</button>
     <button class="btn" data-act="goStep" data-step="2">回到小说正文</button>`)}
    <div class="notice">${esc(msg || '按顺序往下走：② 小说正文 → ③ 剧本 → ④ 分镜 → ⑤ 调度 → ⑥ 提示词 → ⑦ 视频。哪一步做完就停在哪一步，不会自动往下推。')}</div>`;
}

/* ------------------------------------------------ 渲染 work */
function renderWork() {
  if (!S.state) return;
  const el = $('#work');
  const top = el.scrollTop;
  if (S.view === 'wiz') { el.innerHTML = viewWizard(); el.scrollTop = 0; return; }
  const fn = [viewTarget, viewOutline, viewNovel, viewScript, viewShots, viewBlocking, viewPrompts, viewVideo, viewReel, viewLibrary][S.view]
    || { 10: viewProjects, 11: viewMusicCreate, 12: viewMusicPlay, 13: viewSettings }[S.view] || viewTarget;
  el.innerHTML = fn();
  el.scrollTop = top;
  if (S.view === 8 && !S.reel && !S.reelLoading) loadReel();
}

/* ------------------------------------------------ 傻瓜向导（每集独立 9 步） */
const WIZ_STEPS = [
  ['1', '本集正文', 'AI 写或手动改，只影响这一集'],
  ['2', '本集剧本', '角色 / 对白 / 情绪'],
  ['3', '本集分镜', '时长随台词，1–15 秒，超长自动拆'],
  ['4', '素材强校验', '缺角色/场景图就禁止出片'],
  ['5', '批量出画面', '每镜独立进度，可单镜重跑'],
  ['6', '整集拼接', '按分镜顺序合成一条'],
  ['7', '连续配音 / 口型', '整集一条音频 + 方言唇形'],
  ['8', 'SRT 字幕', '对齐配音，可手改'],
  ['9', '导出成片', 'MP4 + 回写集状态'],
];

function wizEp() {
  const eps = (S.state.episodes || []).filter((e) => !Number(e.archived));
  const ep = eps.find((e) => e.id === S.eid) || eps[0] || null;
  if (!ep) return null;
  const es = (S.state.ep_state || []).find((x) => x.id === ep.id) || {};
  return Object.assign({}, ep, es, { id: ep.id, idx: ep.idx, title: ep.title });
}
function wizState() {
  const ep = wizEp();
  if (!ep) return null;
  const shots = (S.state.shots || []).filter((s) => s.episode_id === ep.id).sort((a, b) => a.idx - b.idx);
  const missing = ep.missing || [];
  const generated = shots.filter((s) => (s.video_url || '').match(/\.(mp4|webm)$/i)).length;
  const running = shots.filter((s) => s.status === 'running').length;
  const pending = Number.isFinite(Number(ep.need_gen)) ? Number(ep.need_gen) : shots.length - generated;
  return { ep, shots, missing, generated, running, pending,
    hasNovel: !!(ep.novel_text || '').trim(), hasScript: !!(ep.script_json || '').trim(),
    hasSrt: !!(ep.srt_text || '').trim(), exportUrl: ep.export_url || '' };
}
function wizGate(st, step) {
  if (step === 1) return st.hasNovel;
  if (step === 2) return st.hasScript;
  if (step === 3) return st.shots.length > 0;
  if (step === 4) return st.missing.length === 0 && (st.ep.packet_bad || 0) === 0 && st.shots.length > 0;
  if (step === 5) return st.shots.length > 0 && st.pending === 0;
  if (step === 6) return !!st.exportUrl;
  if (step === 7) return !!st.exportUrl;
  if (step === 8) return st.hasSrt;
  return !!st.exportUrl;
}

function viewWizard() {
  const st = wizState();
  if (!st) return '<div class="empty">还没有剧集</div>';
  if (!S.wizStep) S.wizStep = Number(localStorage.getItem('nexus_wiz_step')) || 1;
  if (S.wizStep < 1 || S.wizStep > 9) S.wizStep = 1;
  const { ep, shots, missing, generated, running, pending } = st;
  const step = S.wizStep;
  const gate = wizGate(st, step);
  const nav = WIZ_STEPS.map(([n, title], i) => {
    const idx = i + 1;
    const done = idx < step ? (wizGate(st, idx) ? 'done' : '') : '';
    return `<div class="wnav ${idx === step ? 'on' : ''} ${done}" data-act="wizStep" data-step="${idx}">
      <b>${done ? '✓' : n}</b><span>${title}</span></div>`;
  }).join('');
  const eps = (S.state.episodes || []).filter((e) => !Number(e.archived));
  const epSel = `<select data-act="wizPickEp" class="wizsel">${eps.map((e) =>
    `<option value="${e.id}" ${e.id === ep.id ? 'selected' : ''}>EP${String(e.idx).padStart(2, '0')} · ${esc(e.title || '')} · ${esc(e.status_label || '')}</option>`).join('')}</select>`;
  const body = [
    wizStep1(st), wizStep2(st), wizStep3(st), wizStep4(st), wizStep5(st),
    wizStep6(st), wizStep7(st), wizStep8(st), wizStep9(st),
  ][step - 1];
  const jobs = (S.state.jobs || []).filter((j) => j.status === 'running' || j.status === 'queued');
  const jobBar = jobs.length ? `<div class="wjob">${jobs.slice(0, 3).map((j) =>
    `<div><span>${esc(j.kind)} #${j.id}</span><div class="pbar"><i style="width:${j.progress || 0}%"></i></div><em>${j.progress || 0}%</em></div>`).join('')}</div>` : '';
  return `<div class="wiz">
    <div class="wizhead">
      <div><h2>制作向导 · 第 ${ep.idx} 集</h2>
        <p>${esc(ep.title || '')} · <b>${esc(ep.status_label || '待制作')}</b> · ${shots.length} 镜 · 已出片 ${generated} 镜${pending ? ' · 待重跑 ' + pending : ''}${running ? ' · 渲染中 ' + running : ''}</p></div>
      <div class="wiztools">${epSel}<button class="btn ghost sm" data-act="wizExit">退出向导</button></div>
    </div>
    <div class="wizbody">
      <div class="wnavs">${nav}</div>
      <div class="wmain">
        ${body}
        ${jobBar}
        <div class="wfoot">
          <button class="btn ghost" data-act="wizStep" data-step="${Math.max(1, step - 1)}" ${step === 1 ? 'disabled' : ''}>← 上一步</button>
          <div class="grow"></div>
          <span class="wstate">${gate ? '本步已完成，可继续' : '本步未完成'}</span>
          <button class="btn primary" data-act="wizNext" data-step="${step}" ${gate ? '' : 'disabled'}>下一步 →</button>
        </div>
        ${gate ? '' : `<div class="wnotice">${wizHint(st, step)}</div>`}
      </div>
    </div>
  </div>`;
}

function wizHint(st, step) {
  if (step === 1) return '先写本集正文，或点「AI 生成本集正文」。';
  if (step === 2) return '把正文拆成剧本（角色 + 对白 + 情绪），确认后进分镜。';
  if (step === 3) return '本集还没有分镜，点「生成分镜」，时长会自动按台词朗读重算。';
  if (step === 4) return (st.missing.length || st.ep.packet_bad) ? '有镜头素材或数据包不完整，已阻断出片：按下面清单逐条补齐。' : '还没有分镜可校验。';
  if (step === 5) return `还有 ${st.shots.length - st.generated} 镜没出片，可以单镜重跑，不影响已完成的。`;
  if (step === 6) return '本集还没拼成一条，点「开始拼接」。';
  if (step === 7) return '先完成第 6 步拼接，再来配音与口型。';
  if (step === 8) return '先生成 SRT 字幕。';
  return '先导出本集成片。';
}

function wizStep1(st) {
  return `<div class="wsec"><h3>① 本集剧情正文</h3>
    <p class="tip">只作用于第 ${st.ep.idx} 集；AI 重写不会覆盖你手改过的文字（手改后先保存，再生成会提示确认）。</p>
    <textarea class="wtext" id="wNovel" data-wiz="novel">${esc(st.ep.novel_text || '')}</textarea>
    <div class="wbtns"><button class="btn" data-act="wizGen" data-kind="novel">AI 生成本集正文</button>
      <button class="btn primary" data-act="wizSaveNovel">保存本集正文</button>
      <button class="btn ghost" data-act="wizGenForce">强制重写</button>
      <span class="dimtext">${(st.ep.novel_text || '').length} 字${Number(st.ep.novel_manual) ? ' · 已手改保护' : ''}</span></div></div>`;
}
function wizStep2(st) {
  const s = parse(st.ep.script_json, {});
  const txt = st.ep.script_json ? JSON.stringify(s, null, 2) : '';
  const rows = (s.scenes || s.shots || []).length || 0;
  return `<div class="wsec"><h3>② 本集剧本</h3>
    <p class="tip">AI 拆分角色、对白、情绪（平静 / 小声 / 激动 / 发火）。改台词只同步本集分镜，不串集。</p>
    <div class="wbtns"><button class="btn" data-act="wizGen" data-kind="script">AI 生成本集剧本</button>
      <button class="btn primary" data-act="wizSaveScript">保存剧本 JSON</button>
      <span class="dimtext">${rows ? rows + ' 场' : '未生成'}</span></div>
    <textarea class="wtext mono" id="wScript" data-wiz="script">${esc(txt)}</textarea></div>`;
}
function wizStep3(st) {
  const rows = st.shots.map((s, i) => {
    const dlg = parse(s.dialogue_json, []).map((d) => (d.sp ? d.sp + '：' : '') + (d.line || '')).join(' / ');
    const over = Number(s.duration) > 15;
    return `<tr data-sid="${s.id}">
      <td>${s.idx}</td>
      <td>${esc(parse(s.chars, []).join('、'))}</td>
      <td class="dlg">${esc(dlg)}<div class="wsub">${esc(s.movement || '')} <button class="wlink" data-act="openParts" data-id="${s.id}">H3 提示词</button> <button class="wlink" data-act="openPacket" data-id="${s.id}">数据包</button></div></td>
      <td>${esc(((s.scene_slug || '').split('·')[1] || s.scene_slug || '').trim())}</td>
      <td><input class="wdur ${over ? 'bad' : ''}" type="number" min="1" max="15" value="${s.duration}" data-act="wizDur" data-id="${s.id}">s</td>
      <td class="wrowbtns">
        <button class="btn xs" data-act="wizMove" data-id="${s.id}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn xs" data-act="wizMove" data-id="${s.id}" data-dir="1" ${i === st.shots.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="btn xs ghost" data-act="wizDelShot" data-id="${s.id}">删</button></td></tr>`;
  }).join('');
  return `<div class="wsec"><h3>③ 本集分镜</h3>
    <p class="tip">硬规则：镜头时长 ≥ 台词朗读时长；单镜 1–15 秒；台词超 15 秒 AI 自动按语义拆镜。超过 15 秒会标红并拒绝保存。</p>
    <div class="wbtns"><button class="btn primary" data-act="wizGen" data-kind="board">${st.shots.length ? '重新生成分镜' : '生成本集分镜'}</button>
      <button class="btn" data-act="wizNormalize">按台词重算时长</button>
      <button class="btn ghost" data-act="wizAddShot">+ 加一镜</button>
      <button class="btn ghost" data-act="wizCheckAssets">检查素材</button></div>
    ${st.shots.length ? `<div class="wtable"><table><thead><tr><th>镜</th><th>角色</th><th>台词</th><th>场景</th><th>时长</th><th>排序</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">还没有分镜</div>'}</div>`;
}
function wizStep4(st) {
  const perrs = st.ep.packet_errors || [];
  const list = st.missing.length ? st.missing.map((m) =>
    `<li><b>${esc(m.name)}</b> ${m.kind === 'no_image' ? '资产存在但没选参考图' : '资产库里没有这个角色/场景'}（第 ${m.shot} 镜）</li>`).join('') : '';
  const plist = perrs.slice(0, 40).map((e) =>
    `<li>第 ${e.shot} 镜 · <b>${esc(e.field)}</b>：${esc(e.msg)}
      <button class="wlink" data-act="openPacket" data-id="${e.shot_id}">去补</button></li>`).join('');
  const bad = st.missing.length + perrs.length;
  return `<div class="wsec"><h3>④ 素材与数据包强校验</h3>
    <p class="tip">逐镜检查：镜头描述、运镜、角色图、场景图、道具图、首尾帧/参考视频、Mini3 提示词、种子、IP 权重、降噪、说话人、时长。缺任何一项直接阻断，不进 ComfyUI。</p>
    <div class="wcheck ${bad ? 'bad' : 'ok'}">${bad
      ? `<b>未通过：${st.missing.length} 项素材缺失 + ${perrs.length} 项数据包缺失，出片已锁死</b><ul>${list}${plist}</ul>`
      : `<b>通过：${st.shots.length} 镜的素材与数据包全部齐全</b>`}</div>
    <div class="wbtns"><button class="btn primary" data-act="wizInitPackets">一键预绑定并确认全镜数据包</button>
      <button class="btn" data-act="wizCheckAssets">重新校验</button>
      <button class="btn" data-act="goAssets">去资产库补齐</button></div></div>`;
}
function wizStep5(st) {
  const cards = st.shots.map((s) => {
    const has = (s.video_url || '').match(/\.(mp4|webm)$/i);
    const stale = !!(s.flags && s.flags.gen_stale);
    const stt = s.status || (has ? 'done' : 'pending');
    const cls = stt === 'running' ? 'run' : stt === 'blocked' || stt === 'failed' ? 'bad' : (has && !stale ? 'ok' : 'run');
    const label = stt === 'running' ? '渲染中' : stt === 'blocked' ? '缺素材' : stt === 'failed' ? '失败'
      : has ? (stale ? '待重跑' : '完成') : '排队';
    return `<div class="wcard ${cls}">
      <div class="wct">第 ${s.idx} 镜 · ${s.duration}s</div>
      <div class="wcv" ${has ? `data-act="playShot" data-url="${esc(s.video_url)}" title="点击播放"` : ''}>${has
        ? `<img src="${esc(s.thumb_url || s.video_url)}" alt="第${s.idx}镜" loading="eager" decoding="sync">`
        : `<div class="wnov">未出片</div>`}</div>
      <div class="wcs"><span class="dot ${cls}"></span>${label}</div>
      <div class="wcb"><button class="btn xs" data-act="wizGenOne" data-id="${s.id}">${has ? '重跑这一镜' : '生成这一镜'}</button>
      ${has ? `<button class="btn xs ghost" data-act="playShot" data-url="${esc(s.video_url)}">▶</button>` : ''}</div>
      ${s.error ? `<div class="werr">${esc(String(s.error).slice(0, 60))}</div>` : ''}
    </div>`;
  }).join('');
  return `<div class="wsec"><h3>⑤ 批量生成纯画面</h3>
    <p class="tip">只出画面，不带配音。每镜独立状态，失败单独重试，不影响已完成镜头；刷新不丢进度。</p>
    <div class="wbtns"><button class="btn primary" data-act="wizGenAll">整集批量出画面（${st.pending} 镜待跑）</button>
      <button class="btn" data-act="wizRefresh">刷新进度</button>
      <span class="dimtext">已完成 ${st.generated}/${st.shots.length} · 待重跑 ${st.pending}</span></div>
    <div class="wgrid">${cards || '<div class="empty">还没有分镜</div>'}</div></div>`;
}
function wizStep6(st) {
  return `<div class="wsec"><h3>⑥ 整集画面拼接</h3>
    <p class="tip">按分镜顺序拼成一条：统一画幅/帧率、轻微调色均衡、每镜首尾淡入淡出。RIFE 光流补帧本机未装节点，暂不做补帧。</p>
    <div class="wbtns"><button class="btn primary" data-act="wizAssemble" ${st.exportUrl ? '' : (st.generated ? '' : 'disabled')}>开始拼接（${st.generated} 镜）</button>
      ${st.exportUrl ? `<button class="btn" data-act="wizExportOpen">看本集成片</button>` : ''}</div>
    ${st.exportUrl ? `<div class="wok">已生成：<code>${esc(decodeURIComponent(st.exportUrl))}</code></div>` : '<div class="empty">还没拼接</div>'}</div>`;
}
function wizStep7(st) {
  const langs = [['zh', '普通话'], ['yue', '广州白话'], ['yue_hz', '化州白话'], ['yue_mm', '茂名白话'], ['hak', '涯话（客家话）']];
  const cur = (proj() || {}).dialogue_lang || 'zh';
  return `<div class="wsec"><h3>⑦ 整集连续配音 / 口型 / 情绪</h3>
    <p class="tip">目标：整集一条连续音频，方言 TTS + 对应 viseme 唇形库，表情跟随台词情绪，自动音量均衡。</p>
    <div class="wrow"><label>方言</label><select data-act="wizDialect" class="wizsel2">${langs.map(([v, n]) =>
      `<option value="${v}" ${cur === v ? 'selected' : ''}>${n}</option>`).join('')}</select></div>
    <div class="wcheck bad"><b>本机未装方言 TTS / viseme 唇形包，这一步还没有真正出声</b>
      <p>已预留接口与配置位：装上 TTS 引擎与各地方言 viseme 配置后即可启用；当前不会假装生成音频。普通话可先用外部 TTS 出整集一条 wav，再回来压片。</p></div>
    <div class="wbtns"><button class="btn" disabled>生成整集连续配音（未接入）</button>
      <button class="btn ghost" disabled>匹配方言口型（未接入）</button></div></div>`;
}
function wizStep8(st) {
  return `<div class="wsec"><h3>⑧ SRT 字幕</h3>
    <p class="tip">时间轴按每镜时长与台词字数分配，生成后可手改文字/时间点，不遮人脸。</p>
    <div class="wbtns"><button class="btn primary" data-act="wizSrt">生成 / 重算 SRT</button>
      <button class="btn" data-act="wizSrtDl" ${st.hasSrt ? '' : 'disabled'}>下载 SRT</button></div>
    <textarea class="wtext mono" id="wSrt" data-wiz="srt">${esc(st.ep.srt_text || '')}</textarea></div>`;
}
function wizStep9(st) {
  return `<div class="wsec"><h3>⑨ 导出本集 MP4</h3>
    <p class="tip">导出后回写集状态「成片导出完成」，返回剧集列表可跳集继续。</p>
    ${st.exportUrl ? `<div class="wok">本集成片：<a href="${esc(st.exportUrl)}" target="_blank">打开 / 下载</a></div>
      <video class="wbig" src="${esc(st.exportUrl)}" controls preload="metadata"></video>` : '<div class="empty">还没导出，先回第 6 步拼接</div>'}
    <div class="wbtns"><button class="btn primary" data-act="wizAssemble" ${st.generated ? '' : 'disabled'}>重新拼接导出</button>
      ${st.exportUrl ? `<a class="btn" href="${esc(st.exportUrl)}" download>下载 MP4</a>` : ''}
      <button class="btn ghost" data-act="wizExit">回剧集列表</button></div></div>`;
}


/* ------------------------------------------------ 交互 */
function bindEvents() {
  document.addEventListener('click', async (e) => {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    try {
      if (act === 'pickEp') {
        S.eid = Number(el.dataset.id); localStorage.setItem('nexus.eid', S.eid);
        renderAll(); return;
      }
      /* ---- 制作向导 ---- */
      if (act === 'wizOpen') { S.view = 'wiz'; S.wizStep = 1; localStorage.setItem('nexus.view', 'wiz'); localStorage.setItem('nexus_wiz_step', '1'); return renderAll(); }
      if (act === 'wizExit') { S.view = 0; localStorage.setItem('nexus.view', '0'); return renderAll(); }
      if (act === 'wizStep') { S.wizStep = Number(el.dataset.step); localStorage.setItem('nexus_wiz_step', String(S.wizStep)); return renderWork(); }
      if (act === 'wizNext') {
        const st = wizState(); const step = Number(el.dataset.step);
        if (!wizGate(st, step)) return toast(wizHint(st, step));
        S.wizStep = Math.min(9, step + 1); return renderWork();
      }
      if (act === 'wizSaveNovel') {
        const ep = wizEp(); if (!ep) return;
        const text = ($('#wNovel') || {}).value || '';
        await api('/api/episodes/' + ep.id, { method: 'POST', body: { novel: text } });
        toast('已保存本集正文（只影响第 ' + ep.idx + ' 集）'); return refresh(true);
      }
      if (act === 'wizGen') {
        const ep = wizEp(); if (!ep) return;
        S.eid = ep.id; localStorage.setItem('nexus.eid', ep.id);
        return doGenerate(el.dataset.kind, null);
      }
      if (act === 'wizGenForce') {
        const ep = wizEp(); if (!ep) return;
        if (!confirm('强制重写会覆盖你手改的第 ' + ep.idx + ' 集正文，确定？')) return;
        await api('/api/episodes/' + ep.id, { method: 'POST', body: { novel_manual: 0 } });
        S.eid = ep.id;
        return doGenerate('novel', null);
      }
      if (act === 'wizSaveScript') {
        const ep = wizEp(); let v;
        try { v = JSON.parse(($('#wScript') || {}).value || '{}'); } catch (e) { return toast('剧本 JSON 格式不对：' + e.message); }
        await api('/api/episodes/' + ep.id, { method: 'POST', body: { script: v } });
        toast('剧本已保存，本集分镜已标记需重做'); return refresh(true);
      }
      if (act === 'wizNormalize') {
        const ep = wizEp();
        const r = await api('/api/episodes/' + ep.id + '/normalize', { method: 'POST', body: {} });
        toast('按台词重算：改了 ' + (r.fixed || []).length + ' 镜' + ((r.warn || []).length ? '；' + r.warn.length + ' 镜超 15 秒需拆镜' : ''));
        return refresh(true);
      }
      if (act === 'wizAddShot') {
        const ep = wizEp();
        await api('/api/shots', { method: 'POST', body: { project_id: S.pid, episode_id: ep.id, action: '新镜头：写清楚谁、在哪、做什么' } });
        return refresh(true);
      }
      if (act === 'wizDelShot') {
        if (!confirm('删掉这一镜？')) return;
        await api('/api/shots/' + el.dataset.id, { method: 'DELETE' });
        return refresh(true);
      }
      if (act === 'wizMove') {
        const ep = wizEp();
        const shots = (S.state.shots || []).filter((s) => s.episode_id === ep.id).sort((a, b) => a.idx - b.idx);
        const i = shots.findIndex((s) => s.id === Number(el.dataset.id));
        const j = i + Number(el.dataset.dir);
        if (i < 0 || j < 0 || j >= shots.length) return;
        const ids = shots.map((s) => s.id); [ids[i], ids[j]] = [ids[j], ids[i]];
        await api('/api/shots/reorder', { method: 'POST', body: { episode_id: ep.id, ids } });
        return refresh(true);
      }
      if (act === 'wizInitPackets') {
        const ep = wizEp();
        const r = await api('/api/episodes/' + ep.id + '/packet_init', { method: 'POST', body: {} });
        toast(r.ok ? ('已确认 ' + r.bound + ' 镜数据包，全部通过') :
          ('已按资产库预绑定 ' + r.bound + ' 镜；仍有 ' + r.errors.length + ' 项要补：' + ((r.errors[0] || {}).msg || '')));
        return refresh(true);
      }
      if (act === 'wizCheckAssets') {
        const ep = wizEp();
        const r = await api('/api/episodes/' + ep.id + '/validate', { method: 'POST', body: {} });
        return toast(r.ok ? '校验通过：素材与单镜数据包齐全'
          : ('未通过：' + r.missing.length + ' 项素材缺失 + ' + (r.packet_errors || []).length + ' 项数据包缺失，出片已锁死'));
      }
      if (act === 'wizGenOne') {
        const ep = wizEp(); S.eid = ep.id;
        return doGenerate('video', Number(el.dataset.id));
      }
      if (act === 'wizGenAll') {
        const ep = wizEp(); S.eid = ep.id;
        return doGenerate('video', null);
      }
      if (act === 'wizRefresh') return refresh(true);
      if (act === 'wizAssemble') {
        const ep = wizEp();
        try {
          const r = await api('/api/episodes/assemble', { method: 'POST', body: { episode_id: ep.id } });
          toast('已提交拼接任务 #' + r.job_id);
          pollJob(r.job_id); await refresh(true);
        } catch (e) { toast(e.message); }
        return;
      }
      if (act === 'wizExportOpen') {
        const ep = wizEp(); if (ep.export_url) window.open(ep.export_url, '_blank');
        return;
      }
      if (act === 'wizSrt') {
        const ep = wizEp();
        try {
          const r = await api('/api/episodes/' + ep.id + '/srt', { method: 'POST', body: {} });
          const ta = $('#wSrt'); if (ta) ta.value = r.srt;
          toast('SRT 已生成：' + r.lines + ' 条'); await refresh(true);
        } catch (e) { toast(e.message); }
        return;
      }
      if (act === 'wizSrtDl') {
        const ep = wizEp();
        const text = ($('#wSrt') || {}).value || ep.srt_text || '';
        if (!text.trim()) return toast('还没有字幕');
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
        a.download = 'EP' + String(ep.idx).padStart(2, '0') + '.srt';
        document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
        return;
      }
      if (act === 'syncEps') {
        const r = await api('/api/episodes/sync', { method: 'POST', body: { project_id: S.pid } });
        toast('已按大纲补齐 ' + r.created + ' 集分组'); return refresh(true);
      }
      if (act === 'pickStyle') {
        const text = el.dataset.text || '';
        const p = proj();
        await api('/api/projects', { method: 'POST', body: { id: p.id, style_lock: text } });
        toast('风格锁已写入：' + text.slice(0, 26) + '…（下游全部标记需重跑）');
        return refresh(true);
      }
      if (act === 'dlNovel') {
        const q = el.dataset.all ? '/api/export/novel?project=' + S.pid
          : '/api/export/novel?project=' + S.pid + '&episode=' + (el.dataset.ep || (ep0() || {}).id);
        const a = document.createElement('a');
        a.href = q; a.download = '';
        document.body.appendChild(a); a.click(); a.remove();
        return toast('已开始下载小说（txt）');
      }
      if (act === 'perfToggle') {
        const id = el.dataset.id;
        S.perfOpen[id] = !S.perfOpen[id];
        renderWork(); return;
      }
      if (act === 'togglePromptLock') {
        const id = Number(el.dataset.id);
        const s = ((S.state.shots) || []).find((x) => x.id === id) || {};
        await api('/api/shots/' + id, { method: 'POST', body: { prompt_lock: Number(s.prompt_lock) ? 0 : 1 } });
        toast(Number(s.prompt_lock) ? '已解锁，⑥ 重跑会重新写提示词' : '已锁提示词，⑥ 重跑不会覆盖这一镜');
        return refresh(true);
      }
      if (act === 'openPacket') {
        await openPacketEditor(Number(el.dataset.id));
        return;
      }
      if (act === 'closePacket') {
        const m = document.getElementById('packetMask');
        if (m) m.classList.add('hidden');
        return;
      }
      if (act === 'savePacket') {
        await savePacket(Number(el.dataset.id), { gen: el.dataset.gen === '1' });
        return;
      }
      if (act === 'regenPacketPrompt') {
        await savePacket(Number(el.dataset.id), { regen: true });
        return;
      }
      if (act === 'openParts') {
        openPartsEditor(Number(el.dataset.id));
        return;
      }
      if (act === 'closeParts') {
        const m = document.getElementById('partsMask');
        if (m) m.classList.add('hidden');
        return;
      }
      if (act === 'saveParts') {
        await saveParts(Number(el.dataset.id), el.dataset.regen === '1');
        return;
      }
      if (act === 'contAll') {
        const value = el.dataset.value === '1' ? 1 : 0;
        const r = await api('/api/repair/continuity', { method: 'POST',
          body: { project_id: proj().id, episode_id: ep0().id, value } });
        toast(value ? `已开启镜头续接：${r.changed} 镜改为「参考上一镜末帧 + 衔接段」，这些镜需重跑④`
                    : `已关闭续接：${r.changed} 镜`);
        return refresh(true);
      }
      if (act === 'repairDialogue') {
        if (!confirm('把有台词但提示词写「无对白」的镜头重写一遍？这些镜会标④待重跑，需要重跑才有真台词。')) return;
        const r = await api('/api/repair/dialogue', { method: 'POST',
          body: { project_id: proj().id, episode_id: ep0().id } });
        toast(`已补齐 ${(r.fixed || []).length} 镜台词提示词（第 ${(r.fixed || []).join('、')} 镜）`);
        return refresh(true);
      }
      if (act === 'setPromptMode') {
        const mode = el.dataset.mode;
        await api('/api/projects', { method: 'POST', body: { id: proj().id, prompt_mode: mode } });
        toast(mode === 'h3ref' ? '已切到 H3 官方六段式：下次 ③ 会按 subject_definitions / summary / retention_analysis / detailed_description / soundscape / music 生成' : '已切回中文自由式');
        return refresh(true);
      }
      if (act === 'applyPerf') {
        const id = Number(el.dataset.id);
        const box = document.querySelector(`.perfbox[data-pid="${id}"]`);
        if (!box) return;
        const perf = {};
        box.querySelectorAll('[data-perf-field]').forEach((x) => { perf[x.dataset.perfField] = x.value; });
        const lock = box.querySelector('[data-perf-lock]');
        await api('/api/shots/' + id, { method: 'POST', body: { perf, perf_lock: lock && lock.checked ? 1 : 0 } });
        toast('表演已存入这一镜，并写进 H3 提交原文');
        return refresh(true);
      }
      if (act === 'chatToggle') {
        const id = el.dataset.id;
        S.chatOpen[id] = !S.chatOpen[id];
        renderWork(); return;
      }
      if (act === 'chatSend') {
        const id = Number(el.dataset.id);
        const box = document.querySelector(`.chatinput textarea[data-shot="${id}"]`);
        const text = (box && box.value || '').trim();
        if (!text) return toast('先写一句你想怎么改');
        if (box) box.value = '';
        await api('/api/shots/' + id + '/chat', { method: 'POST', body: { text } });
        return refresh(true);
      }
      if (act === 'setResTier') {
        const id = Number(el.dataset.id);
        await api('/api/shots/' + id, { method: 'POST', body: { res_tier: el.dataset.tier } });
        return refresh(true);
      }
      if (act === 'toggleRefPrev') {
        const id = Number(el.dataset.id);
        const cur = Number((S.state.shots.find((s) => s.id === id) || {}).ref_prev || 0);
        await api('/api/shots/' + id, { method: 'POST', body: { ref_prev: cur ? 0 : 1 } });
        return refresh(true);
      }
      if (act === 'dlVideo') {
        const u = el.dataset.url;
        if (!u) return toast('这一镜还没有真视频');
        const a = document.createElement('a');
        a.href = u; a.download = (u.split('/').pop() || 'shot.mp4');
        document.body.appendChild(a); a.click(); a.remove();
        return toast('已开始下载视频');
      }
      if (act === 'applyTierAll') {
        const tier = ($('#vTierAll') || {}).value || '';
        const shots = S.state.shots.filter((s) => s.episode_id === ep0().id);
        for (const s of shots) await api('/api/shots/' + s.id, { method: 'POST', body: { res_tier: tier } });
        toast('已把本集 ' + shots.length + ' 镜统一设为 ' + (tier || '自动档'));
        return refresh(true);
      }
      if (act === 'goStep') { S.view = Number(el.dataset.step); localStorage.setItem('nexus.view', S.view); if (S.view === 8 && !S.reel) loadReel(); renderAll(); return; }
      if (act === 'goAssets') { S.view = 9; localStorage.setItem('nexus.view', 9); renderAll(); return; }
      if (act === 'goMain') {
        const v = Number(el.dataset.view);
        if (v === 13 || v === 11) await ensureSettings();
        S.view = v; localStorage.setItem('nexus.view', v); renderAll(); return;
      }
      /* ---- 项目管理 ---- */
      if (act === 'projOpen') {
        S.pid = Number(el.dataset.id); localStorage.setItem('nexus.pid', S.pid);
        S.reel = null; S.chatShot = 0; S.chatProposal = null;
        S.view = 0; localStorage.setItem('nexus.view', 0);
        await refresh(true); toast('已切换项目'); return;
      }
      if (act === 'projEdit') {
        const p = (S.state.projects || []).find((x) => x.id === Number(el.dataset.id));
        return openEditProject(p);
      }
      if (act === 'projDel') {
        const pid = Number(el.dataset.id);
        if (!confirm('删除项目「' + (el.dataset.title || '') + '」？剧本、分镜、素材、成片、音乐都会一并清除，不可恢复。')) return;
        if (!confirm('再确认一次：这是不可撤销的删除。')) return;
        const r = await api('/api/projects/' + pid, { method: 'DELETE' });
        if (S.pid === pid) { S.pid = null; localStorage.removeItem('nexus.pid'); }
        S.state = r.state; S.rev = '';
        await refresh(true); return toast('项目已删除，连带清理 ' + (r.removed_files || 0) + ' 个产物文件');
      }
      if (act === 'epNew') {
        const r = await api('/api/episodes', { method: 'POST', body: { project_id: S.pid, after: ep0() ? ep0().id : 0 } });
        S.state = r.state; S.rev = revOf(r.state); S.eid = r.id;
        renderAll();
        return toast('已在第 ' + r.idx + ' 集位置插入空集');
      }
      if (act === 'epCopy') {
        const r = await api('/api/episodes/' + el.dataset.id + '/copy', { method: 'POST', body: {} });
        S.state = r.state; S.rev = revOf(r.state); S.eid = r.id;
        renderAll();
        return toast('已复制为第 ' + r.idx + ' 集（含本集分镜）');
      }
      if (act === 'epDel') {
        const idx = el.dataset.idx;
        if (!confirm('删除第 ' + idx + ' 集？本集的小说、剧本、分镜、成片都会清掉，不可恢复。')) return;
        if (!confirm('再确认一次：删除第 ' + idx + ' 集。')) return;
        const r = await api('/api/episodes/' + el.dataset.id + '/delete', { method: 'POST', body: {} });
        S.state = r.state; S.rev = revOf(r.state); S.eid = null;
        renderAll();
        return toast('本集已删除，清理 ' + (r.removed_files || 0) + ' 个产物文件');
      }
      if (act === 'setEps') {
        await api('/api/projects', { method: 'POST', body: { id: S.pid, episode_count: Number(el.dataset.n) } });
        return refresh(true);
      }
      /* ---- PRD 六：工作流 ---- */
      if (act === 'wfSetEpisode') {
        const sel = $('#epWf');
        if (!sel) return;
        await api('/api/episodes/' + el.dataset.id + '/workflow', { method: 'POST', body: { key: sel.value } });
        toast(sel.value ? '本集已统一为「' + sel.selectedOptions[0].textContent.split(' · ')[0] + '」' : '本集改回跟随项目默认');
        return refresh(true);
      }
      /* ---- PRD 七：单镜头 AI 对话 ---- */
      if (act === 'shotChatOpen') {
        const sid = Number(el.dataset.id);
        S.chatShot = sid; S.chatProposal = null;
        await ensureChat(sid);
        renderWork();
        const ta = $('#chatText'); if (ta) ta.focus();
        return;
      }
      if (act === 'shotChatClose') { S.chatShot = 0; S.chatProposal = null; return renderWork(); }
      if (act === 'shotChatSend') {
        const sid = Number(el.dataset.id);
        const ta = $('#chatText');
        const text = ((ta && ta.value) || '').trim();
        if (!text) return toast('先说一句要改什么');
        await ensureChat(sid);
        S.chatMsgs[sid] = (S.chatMsgs[sid] || []).concat([{ role: 'user', text }]);
        if (ta) ta.value = '';
        renderWork();
        const r = await api('/api/shots/' + sid + '/chat', { method: 'POST', body: { project_id: S.pid, text, dry: true } });
        S.chatMsgs[sid] = (S.chatMsgs[sid] || []).concat([{ role: 'assistant', text: r.reply }]);
        S.chatProposal = { sid, updates: r.proposal, changed: r.changed || Object.keys(r.proposal || {}) };
        return renderWork();
      }
      if (act === 'shotChatApply') {
        const sid = Number(el.dataset.id);
        if (!S.chatProposal || S.chatProposal.sid !== sid) return toast('没有待应用的改稿');
        const r = await api('/api/shots/' + sid + '/chat/apply', { method: 'POST',
          body: { updates: S.chatProposal.updates } });
        S.chatProposal = null;
        S.state = r.state; S.rev = revOf(r.state);
        renderAll();
        return toast('已应用到本镜：②③④ 会标记需重跑，其他镜头没动');
      }
      if (act === 'shotChatDiscard') { S.chatProposal = null; return renderWork(); }
      if (act === 'shotCopy') {
        const r = await api('/api/shots/' + el.dataset.id + '/copy', { method: 'POST', body: {} });
        S.state = r.state; S.rev = revOf(r.state); renderAll();
        return toast('已复制出一镜，插在原镜后面');
      }
      if (act === 'shotDel') {
        if (!confirm('删除第 ' + el.dataset.idx + ' 镜？只删这一镜，其他镜头不动。')) return;
        await api('/api/shots/' + el.dataset.id, { method: 'DELETE' });
        await refresh(true);
        return toast('这一镜已删除');
      }
      if (act === 'voiceBind') {
        await api('/api/assets/' + el.dataset.id + '/voice', { method: 'POST', body: { voice_id: Number(el.value) } });
        await refresh(true);
        return toast(Number(el.value) ? '音色已绑定，全集台词按说话人自动匹配' : '已解绑，改用通用备用音色');
      }
      if (act === 'voiceNew') {
        const name = prompt('音色名称（建议按角色命名）', el.dataset.name ? el.dataset.name + ' · 主音' : '新音色');
        if (!name || !name.trim()) return;
        const style = prompt('音色描述（如 青年男声/低沉/广州白话）', '') || '';
        const r = await api('/api/voices', { method: 'POST',
          body: { project_id: S.pid, asset_id: Number(el.dataset.id), name: name.trim(), engine: style.trim() } });
        S.state = r.state; S.rev = revOf(r.state); renderAll();
        return toast('音色已建立并绑定到该角色');
      }
      if (act === 'voiceSample') {
        const vid = Number(el.dataset.vid || 0);
        if (!vid) return toast('先给这个角色绑定音色');
        const v = ((S.state.voices) || []).find((x) => x.id === vid) || {};
        const ta = prompt('试听文本（默认用该角色一句台词）', '这是' + (v.name || '角色') + '的试听音色，全程固定不变。');
        if (ta === null) return;
        try {
          const r = await api('/api/voices/' + vid + '/sample', { method: 'POST', body: { text: ta } });
          if (r.url) new Audio(r.url).play();
          toast('合音已生成');
        } catch (err) { toast(err.message || '合音生成失败'); }
        return;
      }
      /* ---- PRD 四：资产操作 ---- */
      if (act === 'assetRename') {
        const name = prompt('新的资产名（改名会同步所有分镜、剧本、说话人）', el.dataset.name || '');
        if (!name || !name.trim()) return;
        const r = await api('/api/assets/' + el.dataset.id, { method: 'POST', body: { name: name.trim() } });
        await refresh(true);
        return toast('已改名，联动更新 ' + (r.renamed_shots || 0) + ' 个镜头');
      }
      if (act === 'assetReplace') {
        const url = prompt('新参考图地址（/media/xxx.png 或图片 URL）', '');
        if (!url) return;
        await api('/api/assets/' + el.dataset.id, { method: 'POST', body: { image_url: url.trim(), locked: 1 } });
        await refresh(true);
        return toast('参考图已替换：之后重跑的镜头会绑新图');
      }
      if (act === 'assetUpload') { return pickUpload(Number(el.dataset.id)); }
      if (act === 'assetDelCheck') {
        const id = Number(el.dataset.id);
        const r = await api('/api/assets/' + id + '/refs', { method: 'POST', body: {} });
        const refs = r.refs || [];
        if (refs.length) {
          const list = refs.slice(0, 6).map((x) => 'EP' + x.episode_idx + ' 第' + x.idx + '镜').join('、');
          if (!confirm('「' + r.asset + '」仍被 ' + refs.length + ' 个镜头引用（' + list + '）。删除后这些镜头会缺素材，确定删？')) return;
          await api('/api/assets/' + id + '?force=1', { method: 'DELETE' });
        } else {
          if (!confirm('删除资产「' + r.asset + '」？')) return;
          await api('/api/assets/' + id, { method: 'DELETE' });
        }
        await refresh(true); return toast('资产已删除');
      }
      if (act === 'batchPrompt') {
        const r = await api('/api/assets/batch_prompt', { method: 'POST', body: { project_id: S.pid } });
        S.state = r.state; S.rev = revOf(r.state); renderAll();
        return toast('已批量生成 ' + r.count + ' 条图片提示词');
      }
      /* ---- PRD 九：音乐 ---- */
      if (act === 'musicNew') {
        const body = {
          project_id: S.pid, kind: ($('#mKind') || {}).value || 'bgm',
          name: ($('#mName') || {}).value || '未命名配乐', style: ($('#mStyle') || {}).value || '',
          mood: ($('#mMood') || {}).value || '温馨',
          duration: Number(($('#mDur') || {}).value || 30), bpm: Number(($('#mBpm') || {}).value || 90),
          prompt: ($('#mPrompt') || {}).value || '',
        };
        const r = await api('/api/music', { method: 'POST', body });
        S.state = r.state; S.rev = revOf(r.state); renderAll();
        const g = await api('/api/music/' + r.id + '/generate', { method: 'POST', body: {} });
        toast('已提交音乐生成 #' + g.job_id + '（本地 ComfyUI）');
        return pollJob(g.job_id);
      }
      if (act === 'musicGen') {
        const r = await api('/api/music/' + el.dataset.id + '/generate', { method: 'POST', body: {} });
        toast('已提交音乐生成 #' + r.job_id);
        return pollJob(r.job_id);
      }
      if (act === 'musicRename') {
        const name = prompt('新的曲名', el.dataset.name || '');
        if (!name || !name.trim()) return;
        await api('/api/music/' + el.dataset.id, { method: 'POST', body: { name: name.trim() } });
        return refresh(true);
      }
      if (act === 'musicLink') {
        const ep = ep0();
        if (!ep) return toast('先建集');
        await api('/api/music/' + el.dataset.id + '/link', { method: 'POST', body: { episode_id: ep.id } });
        await refresh(true); return toast('已关联到 EP' + ep.idx);
      }
      if (act === 'musicDel') {
        if (!confirm('删除音乐「' + (el.dataset.name || '') + '」？音频文件也会删除。')) return;
        const r = await api('/api/music/' + el.dataset.id, { method: 'DELETE' });
        S.state = r.state; S.rev = revOf(r.state); renderAll(); return;
      }
      if (act === 'musicFilter') { S.musicFilter = el.dataset.kind; S.playIdx = 0; return renderWork(); }
      if (act === 'musicPick') { S.playIdx = Number(el.dataset.idx) || 0; return renderWork(); }
      /* ---- PRD 十：系统设置 ---- */
      if (act === 'setTab') {
        S.setTab = el.dataset.tab; renderWork();
        if (S.setTab === 'text') maybeAutoModels();
        return;
      }
      if (act === 'toggleKey') { collectSetForm(); S.showKey = !S.showKey; return renderWork(); }
      if (act === 'fetchModels') return fetchModelsFor(el.dataset.id);
      if (act === 'setProvider') {
        collectSetForm();
        const kind = el.dataset.kind, id = el.dataset.id;
        if (kind === 'text') {
          if (S.set.llm_provider === id) return;
          const p = SET_TEXT_PROVIDERS.find((x) => x[0] === id);
          if (!p) return;
          S.set.llm_provider = id; S.set.llm_base_url = p[3]; S.set.llm_model = p[4];
        } else if (kind === 'video') {
          if (S.set.video_api_mode === id) return;
          const p = SET_VIDEO_PROVIDERS.find((x) => x[0] === id);
          S.set.video_api_mode = id;
          if (p && id !== 'local') S.set.video_api_base = p[3];
        } else if (kind === 'image') {
          if (S.set.image_api_mode === id) return;
          const p = SET_IMAGE_PROVIDERS.find((x) => x[0] === id);
          S.set.image_api_mode = id;
          if (p && id !== 'local') S.set.image_api_base = p[3];
        }
        renderWork();
        if (kind === 'text') maybeAutoModels();
        return;
      }
      if (act === 'setReset') {
        const inp = document.querySelector('[data-set="' + el.dataset.key + '"]');
        if (inp) inp.value = el.dataset.val || '';
        return;
      }
      if (act === 'setDefaults') {
        S.set.llm_provider = 'deepseek';
        S.set.default_novel_model = ''; S.set.default_script_model = ''; S.set.default_board_model = '';
        S.set.default_video_service = 'local'; S.set.default_image_service = 'local'; S.set.default_music_service = 'local';
        S.set.image_concurrency = '1'; S.set.video_concurrency = '1';
        return renderWork();
      }
      if (act === 'setSave') {
        const body = {};
        $$('[data-set]').forEach((f) => { body[f.dataset.set] = f.value.trim(); });
        const r = await api('/api/settings', { method: 'POST', body });
        S.set = r.settings;
        return toast('已保存 ' + (r.saved || []).length + ' 项配置');
      }
      if (act === 'setTest') {
        const kind = el.dataset.kind;
        const f = (k) => { const x = document.querySelector('.setpage [data-set="' + k + '"]'); return x ? x.value.trim() : ''; };
        const body = { kind: kind };
        if (kind === 'text') {
          body.base = f('llm_base_url'); body.key = f('llm_api_key');
          body.provider = f('llm_provider') || (S.set && S.set.llm_provider) || '';
        } else if (kind === 'video' || kind === 'image') {
          const mode = f(kind + '_api_mode') || (S.set && S.set[kind + '_api_mode']) || 'local';
          body.mode = mode;
          body.base = mode === 'local' ? f('comfy_base') : f(kind + '_api_base');
          body.key = f(kind + '_api_key');
        }
        const r = await api('/api/settings/test', { method: 'POST', body: body });
        return toast((r.ok ? '连接正常：' : '连接失败：') + (r.msg || ''));
      }
      if (act === 'checkUpdate') {
        try {
          const r = await api('/api/update_check');
          if (r.available) return showUpdateModal(r);
          if (r.configured === false) return toast('尚未配置远端版本清单');
          if (r.offline) return toast('暂时连不上更新服务器，稍后再试');
          return toast('当前已是最新版本');
        } catch (e) { return toast('检查更新失败：' + e.message); }
      }
      if (act === 'showGuide') return showGuide(true);
      if (act === 'guideClose') {
        const never = document.querySelector('#guideNever');
        if (never && never.checked) localStorage.setItem('nexus.guide', '1');
        document.querySelector('#guide').classList.remove('on');
        return;
      }
      if (act === 'community') return showCommunity();
      if (act === 'communityClose') {
        document.querySelector('#community').classList.remove('on');
        return;
      }
      if (act === 'qrZoom') return showLightbox(el.dataset.url || '');
      if (act === 'updateClose') {
        if (UPDATE_INFO && UPDATE_INFO.latest) localStorage.setItem('nexus.updateDismissed', UPDATE_INFO.latest);
        const um = document.querySelector('#updateModal');
        if (um) um.classList.remove('on');
        return;
      }
      if (act === 'updateCommunity') return showCommunity();
      if (act === 'libKind') { S.libKind = el.dataset.kind || ''; renderWork(); return; }
      if (act === 'playShot') {
        const u = el.dataset.url || '';
        if (!u) return toast('这一镜还没有真片，先点「生成」');
        return showLightbox(u);
      }
      if (act === 'toggleAssets') { document.body.classList.toggle('assetsopen'); return; }
      if (act === 'pickAsset') {
        const id = Number(el.dataset.id);
        const a = (S.state.assets || []).find((x) => x.id === id) || {};
        const v = window.prompt('参考图地址（/media/xxx.png、本地文件名或图片 URL）', a.image_url || '');
        if (v === null) return;
        await api('/api/assets/' + id, { method: 'POST', body: { image_url: v.trim() } });
        toast(v.trim() ? '参考图已绑定，这一条资产的一致性有了硬锚点' : '参考图已清空，回到仅文本锁');
        await refresh(true); return;
      }
      if (act === 'newProject') return openNewProject();
      if (act === 'lockTarget') { await api(`/api/projects/${S.pid}/lock`, { method: 'POST', body: {} }); await refresh(true); return toast('目标已锁定，可以开始生成'); }
      if (act === 'unlockTarget') { await api(`/api/projects/${S.pid}/lock-target`, { method: 'POST', body: {} }); await refresh(true); return toast('已解除锁定：所有产出都会标记需重跑'); }
      if (act === 'generate') return doGenerate(el.dataset.kind, null);
      if (act === 'genShot') return doGenerate(el.dataset.kind, Number(el.dataset.id));
      if (act === 'optimize') return doGenerate('optimize', el.dataset.id ? Number(el.dataset.id) : null, el.dataset.stage);
      if (act === 'shotLock') {
        const id = Number(el.dataset.id);
        const cur = (S.state.shots.find((s) => s.id === id) || {}).locked;
        await api('/api/shots/' + id, { method: 'POST', body: { locked: cur ? 0 : 1 } });
        await refresh(true); return;
      }
      if (act === 'addShot') {
        await api('/api/shots', { method: 'POST', body: { project_id: S.pid, episode_id: ep0().id, action: '新镜头：写清楚谁、在哪、做什么' } });
        await refresh(true); return toast('已新增一镜，记得写分镜描述与对白');
      }
      if (act === 'delAsset') { await api('/api/assets/' + el.dataset.id, { method: 'DELETE' }); await refresh(true); return; }
      if (act === 'makePrompt') {
        const r = await api('/api/assets/' + el.dataset.id + '/make_prompt', { method: 'POST', body: {} });
        toast('已生成图片提示词：' + (r.name || '').slice(0, 12) + '（可手改后再出图）');
        return refresh(true);
      }
      if (act === 'batchImg') {
        const r = await api('/api/assets/batch_image', { method: 'POST',
          body: { project_id: proj().id, only_missing: true } });
        toast(`已排 ${(r.queued || []).length} 个资产批量出图${r.fallback_all ? "（都已有图，改为全部重出）" : "（只补缺图）"}，进度见右下任务队列`);
        return refresh(true);
      }
      if (act === 'genImg') {        const id = Number(el.dataset.id);
        const mode = el.dataset.mode;
        const a = (S.state.assets.find((x) => x.id === id) || {});
        const body = mode === 'reroll' ? { seed: Math.floor(Math.random() * 1e9) } : { mode };
        await api('/api/assets/' + id + '/image', { method: 'POST', body });
        toast('已提交出图：' + (a.name || '') + (mode === 'four' ? '（四视图）' : '') + ' · 进度见右下任务队列');
        return refresh(true);
      }
      if (act === 'genAll') {
        await api('/api/projects/' + S.pid + '/images', { method: 'POST', body: { kind: el.dataset.kind || '' } });
        toast('已提交整组出图，队列会一项一项跑');
        return refresh(true);
      }
      if (act === 'chooseImg') {
        await api('/api/images/' + el.dataset.iid + '/choose', { method: 'POST', body: {} });
        toast('已设为锁定参考图');
        return refresh(true);
      }
      if (act === 'delImg') {
        if (!confirm('删除这张图？')) return;
        await api('/api/images/' + el.dataset.iid, { method: 'DELETE' });
        return refresh(true);
      }
      if (act === 'zoom') {
        const u = el.dataset.url;
        if (!u) return toast('还没有图，先点「生成」');
        showLightbox(u); return;
      }
      if (act === 'lbClose') { $('#lightbox').classList.remove('on'); return; }
      if (act === 'dl') {
        const u = el.dataset.url;
        if (!u) return toast('还没有图');
        const a = document.createElement('a');
        a.href = u; a.download = (u.split('/').pop() || 'asset.png');
        document.body.appendChild(a); a.click(); a.remove();
        return toast('已开始下载');
      }
      if (act === 'assetLock') {
        const id = Number(el.dataset.id);
        const a = (S.state.assets.find((x) => x.id === id) || {});
        await api('/api/assets/' + id, { method: 'POST', body: { locked: a.locked ? 0 : 1 } });
        await refresh(true); return toast(a.locked ? '已解除锁定' : '已锁定：之后所有镜头用这段 canon');
      }
      if (act === 'addAsset') return openAddAsset(el.dataset.kind);
      if (act === 'copy') {
        const id = Number(el.dataset.id);
        const s = S.state.shots.find((x) => x.id === id);
        const txt = el.dataset.kind === 'h3' ? (s.h3_prompt || '') : el.dataset.kind === 'prompt_cn' ? (s.prompt_cn || '') : (s.prompt_neg || '');
        await navigator.clipboard.writeText(txt); return toast('已复制到剪贴板');
      }
      if (act === 'consoleTab') { S.consoleTab = el.dataset.tab; renderConsole(); return; }
      if (act === 'toggleConsole') { S.consoleBig = !S.consoleBig; document.body.classList.toggle('bigconsole', S.consoleBig); renderConsole(); return; }
      if (act === 'cfgSave') {
        const body = {
          engine_mode: $('#cfgMode').value, llm_base_url: $('#cfgBase').value, llm_model: $('#cfgModel').value,
          comfy_base: $('#cfgComfy').value, video_workflow: $('#cfgWf').value, ffmpeg_path: $('#cfgFf').value,
          video_steps: $('#cfgSteps').value, video_scale: $('#cfgScale').value,
        };
        const key = $('#cfgKey').value.trim(); if (key) body.llm_api_key = key;
        await api('/api/config', { method: 'POST', body });
        await refresh(true); return toast('配置已保存，正在重测连接');
      }
      if (act === 'reelScope') {
        S.reelScope = el.dataset.scope;
        S.reelPick = S.reelPick || {};
        renderWork(); return;
      }
      if (act === 'reelExport') {
        const ids = reelShotIds();
        S.reelBusy = true; renderWork();
        try {
          const r = await api('/api/reel/export', { method: 'POST',
            body: { project_id: proj().id, episode_id: ep0().id, shot_ids: ids.length ? ids : null } });
          S.reelLast = { ...(S.reelLast || {}), html: r.url };
          toast(`合集已导出（${r.file}），下面可直接打开/下载`);
        } catch (e) { toast('导出失败：' + e.message); }
        S.reelBusy = false; renderWork(); return;
      }
      if (act === 'reelFfmpeg') {
        const ids = reelShotIds();
        S.reelBusy = true; renderWork();
        try {
          const r = await api('/api/reel/ffmpeg', { method: 'POST',
            body: { project_id: proj().id, episode_id: ep0().id, shot_ids: ids.length ? ids : null } });
          S.reelLast = { ...(S.reelLast || {}), mp4: r.url };
          toast('MP4 合成完成：' + r.file);
        } catch (e) { toast('合成失败：' + e.message); }
        S.reelBusy = false; renderWork(); return;
      }
      if (act === 'reelReload') return loadReel();
      if (act === 'reelPlay') return reelPlay();
      if (act === 'reelPrev') return reelGoto(S.reelIdx - 1);
      if (act === 'reelNextBtn') return reelGoto(S.reelIdx + 1);
      if (act === 'reelGoto') return reelGoto(Number(el.dataset.i));
      if (act === 'closeModal') { $('#modal').classList.remove('on'); return; }
      if (act === 'createProject') return createProject();
      if (act === 'saveProject') return saveProject(el.dataset.id);
      if (act === 'createAsset') return createAsset();
      /* ---- PRD 十三 1：只读 → 编辑 → 保存/取消 ---- */
      if (act === 'roEdit') {
        const box = el.closest('.robox');
        if (!box) return;
        box.classList.add('editing');
        const ta = box.querySelector('.roinput');
        if (ta) ta.focus();
        const acts = box.querySelector('.roacts');
        if (acts) acts.innerHTML = `<button class="btn sm primary" data-act="roSave">保存</button>
          <button class="btn sm ghost" data-act="roCancel">取消</button>`;
        return;
      }
      if (act === 'roCancel') {
        const box = el.closest('.robox');
        if (!box) return;
        box.classList.remove('editing');
        const ta = box.querySelector('.roinput');
        if (ta) ta.value = ta.defaultValue;
        const acts = box.querySelector('.roacts');
        if (acts) acts.innerHTML = `<button class="btn sm ghost" data-act="roEdit">编辑</button>`;
        return;
      }
      if (act === 'roSave') {
        const box = el.closest('.robox');
        if (!box) return toast('找不到字段');
        const ta = box.querySelector('.roinput');
        if (!ta) return;
        ta._touched = false;
        await commit(ta);
        toast('已保存，下游已标记需重跑');
        return refresh(true);
      }
    } catch (err) {
      toast(err.message || '操作失败');
    }
  });

  // PRD 五.4：分镜表拖拽换序
  document.addEventListener('dragstart', (e) => {
    const tr = e.target.closest ? e.target.closest('#shotTbl tbody tr[data-sid]') : null;
    if (!tr) return;
    S.dragSid = Number(tr.dataset.sid);
    tr.classList.add('dragging');
    try {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', tr.dataset.sid);
    } catch (x) { /* 老浏览器不支持，不影响 */ }
  });
  document.addEventListener('dragend', () => {
    $$('#shotTbl tr.dragging, #shotTbl tr.dropmark').forEach((t) => t.classList.remove('dragging', 'dropmark'));
    S.dragSid = 0;
  });
  document.addEventListener('dragover', (e) => {
    if (!S.dragSid) return;
    const tr = e.target.closest ? e.target.closest('#shotTbl tbody tr[data-sid]') : null;
    if (!tr) return;
    e.preventDefault();
    $$('#shotTbl tr.dropmark').forEach((t) => t.classList.remove('dropmark'));
    tr.classList.add('dropmark');
  });
  document.addEventListener('drop', async (e) => {
    const tr = e.target.closest ? e.target.closest('#shotTbl tbody tr[data-sid]') : null;
    if (!S.dragSid || !tr) return;
    e.preventDefault();
    const ep = ep0();
    if (!ep) return;
    const rows = $$('#shotTbl tbody tr[data-sid]');
    const to = rows.indexOf(tr);
    const ids = (S.state.shots || []).filter((s) => s.episode_id === ep.id)
      .sort((a, b) => a.idx - b.idx).map((s) => s.id);
    const from = ids.indexOf(S.dragSid);
    if (from < 0 || to < 0) return;
    if (from === to) return;
    const moved = ids.splice(from, 1)[0];
    ids.splice(to, 0, moved);
    const r = await api('/api/shots/reorder', { method: 'POST', body: { episode_id: ep.id, ids } });
    S.state = r.state; S.rev = revOf(r.state); renderAll();
    toast('镜头顺序已调整');
  });

  // 单镜头 AI 对话：Enter 发送，Shift+Enter 换行
  document.addEventListener('keydown', (e) => {
    if (e.target.id !== 'chatText') return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const btn = document.querySelector('[data-act="shotChatSend"]');
      if (btn) btn.click();
    }
  });

  // 资产库搜索
  document.addEventListener('input', (e) => {
    if (e.target.id !== 'libSearch') return;
    S.libQuery = e.target.value;
    clearTimeout(e.target._lt);
    e.target._lt = setTimeout(() => renderWork(), 320);
  });

  // 风格搜索
  document.addEventListener('input', (e) => {
    if (e.target.id !== 'styleSearch') return;
    S.styleQuery = e.target.value;
    const grid = e.target.closest('.field');
    clearTimeout(e.target._st);
    e.target._st = setTimeout(() => {
      const p = proj();
      const wrap = e.target.closest('.card');
      const host = wrap && wrap.querySelector('.stylegrid');
      if (!host) return;
      const q = S.styleQuery.trim();
      const groups = styleCards().map((g) => ({ name: g.name, styles: g.styles.filter((s) => !q || (s.n + s.t).includes(q)) })).filter((g) => g.styles.length);
      host.innerHTML = groups.map((g) => `<div style="grid-column:1/-1" class="stylecat">${esc(g.name)}</div>` +
        g.styles.map((s) => `<div class="stylecard ${p.style_lock && p.style_lock.startsWith(s.n) ? 'on' : ''}"
          data-act="pickStyle" data-text="${esc(s.n + '：' + s.t)}"><b>${esc(s.n)}</b><span>${esc(s.t)}</span></div>`).join('')).join('')
        || '<div class="tiny" style="padding:10px">没有匹配的风格，直接在风格锁里写你自己的。</div>';
      e.target.focus();
    }, 220);
  });

  // 镜头档位 / 参考上一镜：change 即保存
  document.addEventListener('change', async (e) => {
    const fileEl = e.target.closest('input[type="file"][data-file]');
    if (fileEl) {
      const f = fileEl.files && fileEl.files[0];
      if (!f) return;
      if (f.size > 60 * 1024 * 1024) return toast('文件超过 60MB，换小一点的');
      const dataUrl = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = () => rej(new Error('读取文件失败'));
        fr.readAsDataURL(f);
      }).catch((x) => { toast(x.message); return ''; });
      if (!dataUrl) return;
      try {
        const up = await api('/api/upload', { method: 'POST', body: { data_url: dataUrl } });
        const target = document.querySelector(`#packetMask input[data-pk="${fileEl.dataset.file}"]`);
        if (target) target.value = up.url;
        toast('已上传：' + up.url + '（保存数据包后生效）');
      } catch (x) { toast('上传失败：' + x.message); }
      return;
    }
    const srtEl = e.target.closest('#wSrt');
    if (srtEl) {
      const ep = wizEp();
      if (ep) api('/api/episodes/' + ep.id + '/srt', { method: 'POST', body: { srt_text: srtEl.value } })
        .then(() => toast('字幕手改已保存'))
        .catch((x) => toast('字幕保存失败：' + x.message));
      return;
    }
    const el = e.target.closest('[data-act="setResTier"],[data-act="toggleRefPrev"],[data-act="wizDur"],[data-act="wizPickEp"],[data-act="wizDialect"]');
    if (!el) return;
    if (el.dataset.act === 'wizDur') {
      const id = el.dataset.id;
      api('/api/shots/' + id, { method: 'POST', body: { duration: Number(el.value) } })
        .then(() => { toast('第 ' + id + ' 镜时长已改为 ' + el.value + ' 秒'); refresh(true); })
        .catch((x) => { toast(x.message); refresh(true); });
      return;
    }
    if (el.dataset.act === 'wizPickEp') {
      S.eid = Number(el.value); localStorage.setItem('nexus.eid', S.eid); return refresh(true);
    }
    if (el.dataset.act === 'wizDialect') {
      api('/api/projects', { method: 'POST', body: { id: S.pid, dialogue_lang: el.value } })
        .then(() => { toast('本剧台词语言已切到：' + el.options[el.selectedIndex].text); refresh(true); })
        .catch((x) => toast(x.message));
      return;
    }
    const id = Number(el.dataset.id);
    if (el.dataset.act === 'setResTier') {
      api('/api/shots/' + id, { method: 'POST', body: { res_tier: el.value } }).then(() => refresh(true)).catch((x) => toast(x.message));
    } else {
      api('/api/shots/' + id, { method: 'POST', body: { ref_prev: el.checked ? 1 : 0 } }).then(() => {
        toast(el.checked ? '已打开：生成时会抽上一镜末帧做延续参考' : '已关闭上一镜参考');
        refresh(true);
      }).catch((x) => toast(x.message));
    }
  });

  // 镜头对话：Enter 发送，Shift+Enter 换行
  document.addEventListener('keydown', (e) => {
    if (!e.target.dataset || e.target.dataset.shot === undefined) return;
    if (e.key !== 'Enter') return;
    if (e.shiftKey) return;
    e.preventDefault();
    const id = Number(e.target.dataset.shot);
    const text = e.target.value.trim();
    if (!text) return;
    api('/api/shots/' + id + '/chat', { method: 'POST', body: { text } })
      .then(() => refresh(true)).catch((x) => toast(x.message));
  });

  // 输入：防抖自动保存
  document.addEventListener('input', (e) => {
    const el = e.target.closest('[data-field]');
    if (!el) return;
    S.dirty++;
    syncState('busy');
    clearTimeout(el._t);
    el._t = setTimeout(() => { commit(el); }, 850);
  });

  document.addEventListener('change', (e) => {
    const el = e.target.closest('[data-field]');
    if (!el) return;
    if (el.tagName === 'SELECT' || el.type === 'number' || el.type === 'checkbox') commit(el);
  });

  document.addEventListener('focusin', (e) => {
    const el = e.target.closest('[data-field]');
    if (el) S.editing = el.dataset.field + '#' + el.dataset.id;
  });
  document.addEventListener('focusout', (e) => {
    const el = e.target.closest('[data-field]');
    if (!el) return;
    commit(el);
    setTimeout(() => { S.editing = null; }, 120);
  });

  $('#projSel').addEventListener('change', async (e) => {
    S.pid = Number(e.target.value);
    localStorage.setItem('nexus.pid', S.pid);
    S.reel = null; S.reelIdx = 0;
    await refresh(true);
  });

  const langSel = $('#langSel');
  if (langSel) langSel.addEventListener('change', async (e) => {
    if (!S.pid) return;
    await api('/api/projects', { method: 'POST', body: { id: S.pid, dialogue_lang: e.target.value } });
    toast('台词语言已切换：' + e.target.selectedOptions[0].textContent + '（会写进 H3 提示词）');
    refresh(true);
  });
}

/* ------------------------------------------------ 保存 */
async function commit(el) {
  const f = el.dataset.field;
  if (!f) return;
  const id = el.dataset.id;
  let val = el.type === 'checkbox' ? (el.checked ? 1 : 0) : el.value;
  if (el.type === 'number') val = Number(val);
  if (String(el._last) === String(val) && el._touched) return;
  el._last = val; el._touched = true;
  const [scope, key] = f.split('.');
  try {
    syncState('busy', '保存中…');
    if (scope === 'project') {
      await api('/api/projects', { method: 'POST', body: { id: S.pid, [key]: val } });
    } else if (scope === 'shot') {
      if (key === 'dialogue') {
        const rows = String(val).split(/\n|;/).map((x) => x.trim()).filter(Boolean);
        const arr = rows.map((r) => {
          const p = r.split(/：|:/).map((x) => x.trim());
          return p.length > 1 ? { sp: p[0], line: p.slice(1).join('：') } : { sp: '旁白', line: r };
        });
        await api('/api/shots/' + id, { method: 'POST', body: { dialogue: arr } });
      } else {
        await api('/api/shots/' + id, { method: 'POST', body: { [key]: val } });
      }
    } else if (scope === 'asset') {
      await api('/api/assets/' + id, { method: 'POST', body: { [key]: key === 'locked' ? Number(val) : val } });
    } else if (scope === 'episode') {
      const body = {};
      if (key === 'novel') body.novel = val;
      else body[key] = val;
      await api('/api/episodes/' + id, { method: 'POST', body });
    } else if (scope === 'blocking') {
      const shot = S.state.shots.find((s) => String(s.id) === String(id));
      const b = Object.assign({}, shot.blocking || {}, { text: val });
      await api('/api/shots/' + id, { method: 'POST', body: { blocking: b } });
    } else if (scope === 'outline' || scope === 'script') {
      const ep = ep0();
      if (scope === 'outline') {
        const outline = JSON.parse(JSON.stringify(parse(ep.outline_json, {})));
        let found = null;
        (outline.acts || []).forEach((a) => (a.episodes || []).forEach((x) => { if (String(x.idx) === String(id)) found = x; }));
        if (found) {
          if (key === 'beats') found.beats = String(val).split('\n').map((x) => x.trim()).filter(Boolean);
          else found[key] = val;
        }
        await api('/api/episodes/' + ep.id, { method: 'POST', body: { outline } });
      } else {
        const script = JSON.parse(JSON.stringify(parse(ep.script_json, {})));
        const sc = (script.scenes || []).find((x) => String(x.no) === String(id));
        if (sc) {
          if (key === 'dialogue') {
            sc.dialogue = String(val).split('\n').map((x) => x.trim()).filter(Boolean).map((r) => {
              const p = r.split('｜').map((x) => x.trim());
              return { sp: p[0] || '旁白', line: p[1] || '', emotion: p[2] || '' };
            });
          } else sc[key] = val;
        }
        await api('/api/episodes/' + ep.id, { method: 'POST', body: { script } });
      }
    }
    S.dirty = Math.max(0, S.dirty - 1);
    syncState('ok');
    // 静默刷新数据（不重建视图，避免打断输入）
    const st = await api('/api/state?project=' + S.pid);
    S.state = st; S.rev = revOf(st); renderRail(); renderStatus();
  } catch (e) {
    S.dirty = 0;
    syncState('bad', e.message);
    toast('保存失败：' + e.message);
  }
}

/* ------------------------------------------------ 生成 */
async function doGenerate(kind, shotId, stage) {
  const ep = ep0();
  if (!ep) return toast('先建项目');
  if (!proj().target_locked && ['outline', 'novel', 'script', 'board'].includes(kind)) {
    return toast('目标还没锁定：先在 ① 锁定题材/画幅/集数/风格');
  }
  const body = { project_id: S.pid, episode_id: ep.id };
  if (shotId) body.shot_id = shotId;
  if (stage) body.stage = stage;
  try {
    const r = await api('/api/generate/' + kind, { method: 'POST', body });
    toast('已提交任务 #' + r.job_id + '，进度见下方控制台');
    if (kind === 'optimize') S.consoleTab = 'jobs';
    await refresh(true);
        // 轻量轮询该任务直到结束
    pollJob(r.job_id);
  } catch (e) { toast(e.message); }
}

async function pollJob(jid) {
  for (let i = 0; i < 240; i++) {
    await new Promise((r) => setTimeout(r, 1200));
    let st;
    try { st = await api('/api/state?project=' + S.pid); } catch (e) { continue; }
    S.state = st;
    if (!S.editing && S.dirty === 0) { S.rev = revOf(st); renderAll(); } else renderConsole();
    const j = (st.jobs || []).find((x) => x.id === jid);
    if (j && (j.status === 'done' || j.status === 'failed')) {
      if (j.status === 'failed') toast('任务失败：' + (j.message || ''));
      else if (S.view === 8) loadReel();
      return;
    }
  }
}

/* ------------------------------------------------ 弹窗 */
function openNewProject() {
  const styleOpts = ['真人写实', '日系动漫', '3D国风动漫', '神话仙侠', '卡通', '油画', '水彩', '像素风', '赛博朋克', '古风', '奇幻', '电影感', '其他风格'];
  const ratioOpts = [
    ['16:9', '16:9 横屏（电影/视频）'],
    ['9:16', '9:16 竖屏（短视频）'],
    ['1:1', '1:1 方形（社交媒体）'],
    ['4:3', '4:3 传统比例'],
    ['3:4', '3:4 竖版图片'],
  ];
  const types = [
    ['ai_short', '▣', 'AI短剧', '1-3集短片'],
    ['series', '▦', '连续剧', '多集连续剧'],
    ['long', '▷', 'AI长视频', '10分钟+'],
  ];
  $('#modalbox').innerHTML = `
    <div class="nphead">
      <h3>新建创作项目</h3>
      <button class="npclose" data-act="closeModal" aria-label="关闭">×</button>
    </div>
    <div class="field">
      <label><span class="req">*</span>项目名称</label>
      <div class="input-count">
        <input id="npTitle" maxlength="50" placeholder="请输入项目名称" autocomplete="off">
        <span class="count" id="npTitleCount">0 / 50</span>
      </div>
    </div>
    <div class="field">
      <label><span class="req">*</span>项目类型</label>
      <div class="ntype-grid" id="npTypeGrid">
        ${types.map(([v, icon, name, desc]) => `
          <label class="ntype-card ${v === 'series' ? 'on' : ''}" data-type="${v}">
            <input type="radio" name="npType" value="${v}" ${v === 'series' ? 'checked' : ''} hidden>
            <span class="ntype-icon">${icon}</span>
            <span class="ntype-name">${name}</span>
            <span class="ntype-desc">${desc}</span>
            <span class="ntype-dot"></span>
          </label>`).join('')}
      </div>
    </div>
    <div class="field">
      <label>视觉风格</label>
      <select id="npStyle">
        <option value="" selected>选择项目的视觉风格</option>
        ${styleOpts.map((s) => `<option value="${s}">${s}</option>`).join('')}
      </select>
      <span class="field-tip">视觉风格将影响后续图片和视频生成的画面效果</span>
    </div>
    <div class="field">
      <label>画面尺寸</label>
      <select id="npRatio">
        ${ratioOpts.map(([r, label]) => `<option value="${r}" ${r === '16:9' ? 'selected' : ''}>${label}</option>`).join('')}
      </select>
      <span class="field-tip">画面尺寸将统一整部剧的图片和视频生成比例，创建后不可更改</span>
    </div>
    <div class="field">
      <label>项目描述</label>
      <div class="input-count ta">
        <textarea id="npPremise" maxlength="200" rows="3" placeholder="简要描述你的创作想法..."></textarea>
        <span class="count" id="npPremiseCount">0 / 200</span>
      </div>
    </div>
    <div class="npfoot">
      <button class="btn ghost" data-act="closeModal">取消</button>
      <button class="btn primary" data-act="createProject">创建项目</button>
    </div>`;

  const titleEl = $('#npTitle'), premiseEl = $('#npPremise');
  const syncCount = (el, sid, max) => { $(sid).textContent = `${el.value.length} / ${max}`; };
  titleEl.addEventListener('input', () => syncCount(titleEl, '#npTitleCount', 50));
  premiseEl.addEventListener('input', () => syncCount(premiseEl, '#npPremiseCount', 200));

  const grid = $('#npTypeGrid');
  grid.addEventListener('click', (e) => {
    const card = e.target.closest('.ntype-card');
    if (!card) return;
    grid.querySelectorAll('.ntype-card').forEach((c) => c.classList.remove('on'));
    card.classList.add('on');
    const radio = card.querySelector('input');
    if (radio) radio.checked = true;
  });

  $('#modal').classList.add('on');
}

async function createProject() {
  const title = $('#npTitle').value.trim();
  if (!title) return toast('请输入项目名称');
  const typeCard = document.querySelector('#npTypeGrid .ntype-card.on');
  const typeKey = typeCard ? typeCard.dataset.type : 'series';
  const typeMap = {
    ai_short: { genre: 'AI短剧', episode_count: 3, duration_range: '75–95s' },
    series: { genre: '连续剧', episode_count: 12, duration_range: '75–95s' },
    long: { genre: 'AI长视频', episode_count: 1, duration_range: '10分钟+' },
  };
  const t = typeMap[typeKey] || typeMap.series;
  const body = {
    title,
    premise: $('#npPremise').value.trim(),
    genre: t.genre,
    ratio: $('#npRatio').value,
    episode_count: t.episode_count,
    duration_range: t.duration_range,
    style_lock: $('#npStyle').value,
    fps: 30,
    quality: '电影级',
    avoid_tpl: '标准版',
    video_wf: 'director',
  };
  const st = await api('/api/projects', { method: 'POST', body });
  S.pid = st.project.id; localStorage.setItem('nexus.pid', S.pid);
  $('#modal').classList.remove('on');
  S.reel = null; S.view = 0; localStorage.setItem('nexus.view', 0);
  await refresh(true);
  toast('项目已创建，去 ① 锁定目标');
}

function openEditProject(p) {
  if (!p) return toast('项目不存在');
  const wfOpts = ((S.state.wf) || []).map((w) => `<option value="${w.key}" ${p.video_wf === w.key ? 'selected' : ''}>${esc(w.name)}</option>`).join('');
  $('#modalbox').innerHTML = `
    <h3>修改项目 · ${esc(p.title || '')}</h3>
    <div class="field"><label>项目名称</label><input id="epTitle" value="${esc(p.title)}"></div>
    <div class="field"><label>项目描述</label><textarea id="epPremise" style="min-height:74px">${esc(p.premise)}</textarea></div>
    <div class="frow">
      <div class="field"><label>题材</label><input id="epGenre" value="${esc(p.genre)}"></div>
      <div class="field"><label>画面尺寸</label><select id="epRatio">
        ${['9:16', '16:9', '1:1', '4:3', '3:4', '21:9'].map((r) => `<option ${p.ratio === r ? 'selected' : ''}>${r}</option>`).join('')}</select></div>
    </div>
    <div class="frow">
      <div class="field"><label>规划总集数</label><input id="epEps" type="number" min="1" max="300" value="${p.episode_count}"></div>
      <div class="field"><label>单集时长区间</label><input id="epRange" value="${esc(p.duration_range || '75–95s')}"></div>
    </div>
    <div class="frow">
      <div class="field"><label>帧率</label><select id="epFps">
        ${[24, 25, 30, 60].map((f) => `<option value="${f}" ${Number(p.fps) === f ? 'selected' : ''}>${f} fps</option>`).join('')}</select></div>
      <div class="field"><label>画质等级</label><select id="epQuality">
        ${['预览', '标准', '高清', '电影级'].map((q) => `<option ${p.quality === q ? 'selected' : ''}>${q}</option>`).join('')}</select></div>
    </div>
    <div class="field"><label>视觉风格锁</label><input id="epStyle" value="${esc(p.style_lock)}"></div>
    <div class="field"><label>避雷词 / 禁区</label><input id="epForbid" value="${esc(p.forbidden)}"></div>
    <div class="frow">
      <div class="field"><label>避雷词模板</label><select id="epAvoid">
        ${['标准版', '平台合规版', '道德安全版', '自定义'].map((q) => `<option ${p.avoid_tpl === q ? 'selected' : ''}>${q}</option>`).join('')}</select></div>
      <div class="field"><label>默认视频工作流</label><select id="epWfSel">${wfOpts}</select></div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
      <button class="btn ghost" data-act="closeModal">取消</button>
      <button class="btn primary" data-act="saveProject" data-id="${p.id}">保存</button>
    </div>`;
  $('#modal').classList.add('on');
}

async function saveProject(id) {
  const body = {
    id: Number(id),
    title: $('#epTitle').value, premise: $('#epPremise').value, genre: $('#epGenre').value,
    ratio: $('#epRatio').value, episode_count: Number($('#epEps').value || 12),
    duration_range: $('#epRange').value, fps: Number($('#epFps').value || 30),
    quality: $('#epQuality').value, style_lock: $('#epStyle').value, forbidden: $('#epForbid').value,
    avoid_tpl: $('#epAvoid').value, video_wf: $('#epWfSel').value,
  };
  await api('/api/projects', { method: 'POST', body });
  $('#modal').classList.remove('on');
  await refresh(true);
  toast('项目已更新');
}

function pickUpload(aid) {
  const inp = document.createElement('input');
  inp.type = 'file'; inp.accept = 'image/*';
  inp.onchange = async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    const fr = new FileReader();
    fr.onload = async () => {
      const up = await api('/api/upload', { method: 'POST', body: { data_url: fr.result } });
      await api('/api/assets/' + aid, { method: 'POST', body: { image_url: up.url, locked: 1 } });
      await refresh(true);
      toast('已上传并设为锁定参考图');
    };
    fr.readAsDataURL(f);
  };
  inp.click();
}

function openAddAsset(kind) {
  $('#modalbox').innerHTML = `
    <h3>新增${kind === 'char' ? '角色' : kind === 'scene' ? '场景' : '道具'}</h3>
    <div class="field"><label>名称</label><input id="naName"></div>
    <div class="field"><label>锁定描述 canon（会原文写进提示词）</label>
      <textarea id="naCanon" style="min-height:80px" placeholder="例如：念念｜5 岁半｜圆脸大眼，齐刘海，米白小熊卫衣，红色小书包，左眉尾一颗小痣"></textarea></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
      <button class="btn ghost" data-act="closeModal">取消</button>
      <button class="btn primary" data-act="createAsset" data-kind="${kind}">创建并锁定</button>
    </div>`;
  $('#modal').classList.add('on');
}

async function createAsset() {
  const kind = document.querySelector('[data-act="createAsset"]').dataset.kind;
  await api('/api/assets', { method: 'POST', body: { project_id: S.pid, kind, name: $('#naName').value || '新资产', canon: $('#naCanon').value, locked: 1 } });
  $('#modal').classList.remove('on');
  await refresh(true); toast('已创建并锁定');
}

/* ------------------------------------------------ 新手引导 */
function showGuide(force) {
  let g = $('#guide');
  if (!g) { g = document.createElement('div'); g.id = 'guide'; document.body.appendChild(g); }
  const sec = (n, t, b) => `<div class="gcard-g"><div class="gno">${n}</div><div><b>${t}</b><p>${b}</p></div></div>`;
  g.innerHTML = `
    <div class="gmask" data-act="guideClose"></div>
    <div class="gbox">
      <div class="ghead"><b>这套系统怎么用（一分钟看完）</b>
        <span class="tiny">做完哪一步就停在哪一步，不会自动往下走；只能手动退回</span></div>
      <div class="ggrid">
        ${sec('00-02', '目标锁定 → 大纲 → 正文', '先在 00 锁题材/画幅/集数/风格锁，再 AI 写大纲、写小说。每集独立分组，EP01–EP12 互不覆盖。')}
        ${sec('03-04', '剧本 → 分镜', 'AI 出场次/动作/对白，再拆 4–10 秒可生成的镜头。改① 会让 ②③④ 全部标「需重跑」。')}
        ${sec('05-06', '调度 → H3 提示词', '机位/走位/光位先定，再生成 H3 提交原文。提示词里写明「图N = 谁」，参考图才锁得住人。')}
        ${sec('07', '生成视频（H3）', '选档位 480/720/1080；勾「参考上一镜」抽末帧延续；点缩略图直接播放；「AI 改镜」用一句话改台词/景别/光。')}
        ${sec('语言', '台词语言（顶栏下拉）', '普通话 / 粤语(白话) / 客家话 / 英语 / 闽南话 / 四川话。选完会写进 H3 提示词的「台词语言」段，口型按该语言，不改成旁白。')}
        ${sec('表演', '表演层（每镜「表演」按钮）', '情绪基调、强度 1–5、表情细节、音量、语速、肢体、眼神、口型。强度 5 = 爆发；愤怒就选「瞪眼/眉心收紧 + 大声/喊出来 + 握拳」，全都焊进 H3 原文。')}
        ${sec('资产库', '资产三分组 + 锁定', '顶栏「资产库」：角色形象 / 场景资产 / 道具资产。四视图或单图出图，选中那张 = 锁定参考图，H3 按「图1 = 阿棠」喂进去。')}
        ${sec('08', '成片合集', '真片自动串时间线，条带点哪格跳哪镜，可导出内嵌视频的 HTML 或 ffmpeg 合 MP4。')}
      </div>
      <div class="gbar">
        <button class="btn primary" data-act="guideClose">知道了，开始</button>
        <label class="tiny"><input type="checkbox" id="guideNever"> 以后不再自动弹出</label>
      </div>
    </div>`;
  g.classList.add('on');
  if (force) g.dataset.force = '1';
}

/* 版本更新提示：远端清单判定为旧版本时弹出，引导联系 QQ / 交流群领取新版。 */
let UPDATE_INFO = null;
function showUpdateModal(info) {
  UPDATE_INFO = info || {};
  let m = $('#updateModal');
  if (!m) { m = document.createElement('div'); m.id = 'updateModal'; document.body.appendChild(m); }
  const qq = UPDATE_INFO.qq || '229611621';
  m.innerHTML = `
    <div class="gmask" data-act="updateClose"></div>
    <div class="gbox upbox">
      <div class="ghead uphead"><b>🔔 发现新版本</b>
        <span class="tiny">当前 v${esc(UPDATE_INFO.current || '')} → 最新 v${esc(UPDATE_INFO.latest || '')}</span></div>
      <div class="upbody">
        <p class="upnote">${esc(UPDATE_INFO.note || '你的系统版本已过期，请联系作者获取最新版本使用。')}</p>
        <div class="upcontact">
          <div class="upqq">联系 QQ：<b>${esc(qq)}</b></div>
          <div class="uptip">加 QQ 联系作者领取最新版本，也可进交流群获取。</div>
        </div>
      </div>
      <div class="gbar">
        <button class="btn ghost" data-act="updateCommunity">查看交流群二维码</button>
        <button class="btn primary" data-act="updateClose">我知道了</button>
      </div>
    </div>`;
  m.classList.add('on');
}

/* 社区交流：作者固定展示信息（只读）。
   界面不提供任何编辑 / 删除 / 替换二维码的入口；如需更换群名、文案或二维码，
   由部署者改下方常量，并替换 static/qq-group-qrcode.jpg。 */
const COMMUNITY = {
  title: '🎉 欢迎加入作者的 QQ 交流群',
  sub: '加入官方交流群，与更多创作者一起沟通学习',
  intro: '大家好！感谢使用 AI 视频创作系统。为了方便大家沟通交流、第一时间获取版本更新，作者建立了 QQ 交流群，期待你的加入！',
  perks: [
    ['💬', '反馈使用问题，作者协助解答'],
    ['🚀', '第一时间获取新版本发布与功能预告'],
    ['🎬', '交流视频创作技巧与提示词经验'],
    ['💡', '提出你的需求建议，一起打磨产品'],
  ],
  qr: '/static/qq-group-qrcode.jpg?v=78',
  way: '进群方式：用手机 QQ 扫描上方二维码即可入群 👇',
};

function showCommunity() {
  let c = $('#community');
  if (!c) { c = document.createElement('div'); c.id = 'community'; document.body.appendChild(c); }
  c.innerHTML = `
    <div class="gmask" data-act="communityClose"></div>
    <div class="gbox commbox">
      <div class="ghead commhead"><b>${esc(COMMUNITY.title)}</b>
        <span class="tiny">${esc(COMMUNITY.sub)}</span></div>
      <div class="commbody">
        <p class="commintro">${esc(COMMUNITY.intro)}</p>
        <div class="commperk-title">进群你可以：</div>
        <ul class="commperks">
          ${COMMUNITY.perks.map(([em, t]) => `<li><span class="commem">${em}</span><span>${esc(t)}</span></li>`).join('')}
        </ul>
        <div class="commqr">
          <img class="qrzoom" data-act="qrZoom" data-url="${COMMUNITY.qr}" src="${COMMUNITY.qr}" alt="QQ 交流群二维码" title="点击放大">
          <div class="commway">${esc(COMMUNITY.way)}<span class="qrhint">点击二维码可放大查看</span></div>
        </div>
      </div>
      <div class="gbar">
        <button class="btn primary" data-act="communityClose">关闭</button>
      </div>
    </div>`;
  c.classList.add('on');
}


let toastTimer = null;
function showLightbox(url) {
  let lb = $('#lightbox');
  if (!lb) { lb = document.createElement('div'); lb.id = 'lightbox'; document.body.appendChild(lb); }
  const isVid = /\.(mp4|webm)$/i.test(url || '');
  lb.innerHTML = `
    <div class="lbmask" data-act="lbClose"></div>
    <div class="lbbox">
      ${isVid ? `<video class="lbvid" src="${esc(url)}" controls autoplay playsinline></video>`
              : `<img class="lbimg" src="${esc(url)}" alt="">`}
      <div class="lbbar">
        <button class="btn sm" data-act="dl" data-url="${esc(url)}">下载${isVid ? '视频' : '原图'}</button>
        <button class="btn sm ghost" data-act="lbClose">关闭</button>
      </div>
    </div>`;
  lb.classList.add('on');
  const v = lb.querySelector('video');
  if (v) {
    v.muted = false;
    const p = v.play();
    if (p && p.catch) p.catch(() => { v.muted = true; v.play().catch(() => {}); });
    setTimeout(() => {
      if (v.paused) {
        const ov = document.createElement('button');
        ov.className = 'lbplay'; ov.type = 'button';
        ov.textContent = '▶ 播放';
        ov.onclick = () => { v.muted = false; v.play().catch(() => {}); ov.remove(); };
        lb.querySelector('.lbbox').appendChild(ov);
      }
    }, 500);
  }
}

function toast(msg) {
  const el = $('#toast');
  el.textContent = msg; el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), 3200);
}
