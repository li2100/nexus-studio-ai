# H3 视频提示词核心规范（按 MiniMax 官方 + GitHub 开源实现核对）

> 用途：给本平台 ③ 视频提示词 / ⑥ H3 提交原文提供权威依据，避免「凭感觉写提示词」。
> 本文件里的规范来自 MiniMax 官方写作指南，不是二手经验。

## 一、官方来源（原文可查）

| 材料 | 地址 |
| --- | --- |
| H3 全参考模式（Ref2VA）提示词规范 | https://huggingface.co/MiniMaxAI/MiniMax-H3/raw/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md |
| H3 基础模式（T2VA/I2VA/FL2VA/L2VA） | https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md |
| ComfyUI 官方 H3 教程 | https://docs.comfy.org/tutorials/partner-nodes/minimax/minimax-h3 |
| MiniMax H3 部署与工作流 | https://design.minimaxi.com/h3 |
| GitHub：H3 提示词编译器（Plan v2，读完 README） | https://github.com/ethanfel/ComfyUI-MiniMax-H3-Guide |
| GitHub：多段 H3 导演台 | https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director |
| GitHub：参考素材时间轴导演 | https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector |
| GitHub：镜头串联（运动+声音延续） | https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context |
| GitHub：首尾帧/图生/文生工作流+分辨率面板 | https://github.com/pixelaigc/ComfyUI-MiniMax-H3-Workflow |
| H3 Dials（运动/情绪滑杆，第三方整理） | https://freakyjolly.com/h3-dials-comfyui-motion-mood-sliders/ |

## 二、官方全参考模式：六段式（顺序不能乱）

| 段 | 内容 | 写法要点 |
| --- | --- | --- |
| `subject_definitions` | 被引用的内容与标签 | 四类标签：`<Subject N>` 可见主体、`<Picture N>` 图像（首帧/关键帧/构图锚点）、`<Video N>` 视频（编辑源/延续起点/节奏结构）、`<Audio N>` 音频（照搬/音色/节拍参考） |
| `summary` | 任务类型 + 目标视频 + 主要参考关系 | 开头方括号任务类型，如 `[reference generation]`、`[video editing + audio reuse]`，多类型用 ` + ` 连接 |
| `retention_analysis` | 每个标签保留到什么程度 | 画面用 `fully_preserved / partially_preserved / attribute_transfer / weak_reference`；音频用 `fully_copy / partially_copy / reference / weak_reference`，一行一个标签 |
| `detailed_description` | 按播放顺序写画面/动作/镜头/声音/台词 | `[Shot 1]` 开场无时间戳；后续 `[Shot N] At MM:SS.mmm` 标切点；说话人 `(S1)(S2)` 按发声顺序编号；台词写 `<d>[Language] …</d>`；跨切 `<scenetrans>`、截断 `<cutoff>`；生成任务正文 350–500 英文词（对白密集时以说完整台词为优先） |
| `overall_soundscape` | 环境声与物理声（全片概述） | 只写环境/动作声，不写台词、不写配乐 |
| `non_diegetic_music` | 只有观众听得到的配乐 | 写乐器、速度、强弱变化；没有就写 `N/A` |

硬性细节：
- 台词、歌词、画面里可见的文字保留原语言，其余正文用英文（官方指南要求）。
- 说话人编号只按目标视频里“真正发声”的顺序分配，同一人跨镜头复用同一编号；旁白、画外音也算独立声源。
- 参考图若只用来定义角色/场景/风格，就不要单列 `<Picture N>`，而写进对应 `<Subject N>`；只有当它是首帧/关键帧/构图锚点时才单列。
- 台词被画面结束截断用 `<cutoff>`，跨切继续说话用 `<scenetrans>`，不要靠猜。

## 三、H3 能力上限（决定提示词怎么写）

- 4–15 秒；原生 `17k+5` 帧栅格、24fps（362 帧 ≈ 15.083 秒）。
- 参考素材：图 ≤9、视频 ≤3、音频 ≤3，混合参考合计 ≤12；每个参考视频/音频 2–15 秒，累计各 ≤15 秒。
- 参考图数量与顺序会映射到提示词里的 `<Picture N>` / `<Subject N>`，喂图顺序错了，脸就错了。

## 四、本平台怎么落地的

| 平台字段 | 对应官方规范 |
| --- | --- |
| 资产库 canon（角色/场景/道具） | `subject_definitions` 的 `<Subject N>` + `<Picture N>` |
| 每镜 `chars / props / scene_slug` | 生成 `<Subject N>` 与 `[Shot 1]` 内的出场描述 |
| ② 场面调度（机位/镜头/光/走位/构图/节拍） | `detailed_description` 里的镜头运动、构图、布光、动作 |
| 表演层 8 项（情绪/强度/表情/音量/语速/肢体/眼神/口型） | `detailed_description` 内的表演与台词方式 |
| 对白轨（角色 + 台词） | `<d>[语言] …</d>` + `(S1)(S2)` 编号 |
| 台词语言下拉 | `<d>` 内的 `[Chinese]/[Cantonese]/[Hakka]/[English]…` 与发音口型指令 |
| 参考上一镜 | 上一镜末帧作为 `<Picture N>` / 延续参考 |
| 负面提示 | 官方没有负面段，平台在末尾以「画面里不要出现：」追加，属于工程加固 |

两种格式（③ 页可一键切换）：
- `中文自由式`（默认）：中文六段（主体/场景/道具/动作/镜头/构图/光线/调度/风格/对白）+ 【表演与台词（锁死）】。已被现网 18 镜验证可用。
- `H3 官方六段式（参考模式）`：严格按上面六段 + `<Subject N>` / `<Picture N>` / `<d>[语言]` / `(S1)` 写，适合重跑对准确率要求高的镜头。

## 五、从 GitHub 学到但**尚未实现**的（列清楚，不装作做了）

1. **运动幅度/情绪滑杆**（H3 Dials）：官方 H3 没有暴露强度参数，只能用提示词描述；第三方用 LoRA/节点实现。我们目前用「表演层」的文字强度近似。
2. **运动上下文串联**（NikoDemon80/ComfyUI-H3-Motion-Context）：靠参考视频把运动与声音跨镜头延续。我们只有「上一镜末帧」参考，没有音频延续。
3. **多段时间轴导演**（AIMixer / Songssx）：可在一段视频里排多个 reference 时间片。我们的粒度是一镜一提交，不做段内多参考。
4. **原生分辨率总控**（pixelaigc）：首尾帧 + Turbo LoRA + 分辨率面板；我们已有档位映射，但 1080p 仍是算法放大未实测。

## 六、踩过的坑（已修）

- **对白丢失**：`shots.dialogue_json` 是 JSON 字符串，生成提示词时直接读 `shot["dialogue"]` 得到空值，12 镜提示词被写成「无对白，仅环境声」——素材里有台词，H3 却按无声跑。已修：生成前统一 `parse_json`，并把台词逐字写进 【表演与台词（锁死）】段；③ 页有「补齐台词提示词」按钮，补齐后这 12 镜标 ④ 待重跑。
- **参考图顺序**：提示词里的 `图1/图2` 必须和实际提交的参考图顺序一致，否则锁定的是错的资产。现在 `h3_prompt_text()` 与 `build_h3_workflow()` 共用同一份 labels。
