"""通用前置守卫：确保任务主体开始时的界面是「游戏主界面」。

★★ 本模块对外只提供两个**可复用部件**，新脚本直接借用，不要重复实现 ★★
======================================================================
部件① 回到主界面（pipeline 节点 `回到主界面`）
    作用：把界面从任何地方拉回主界面（仙界 或 人间 都算），拉不回来就硬失败。
    实现：Custom 动作 `maa_agent_guard_ensure_main`。
    接线（新任务只需在 interface.json 的 pipeline_override 里加这两行）：
        "<任务入口节点>": {"next": ["回到主界面"]}
        "通用_守卫出口": {"next": ["<你的任务主体>"]}
    失败：走 on_error → `通用_守卫失败`（终点，不再乱点）。

部件② 切到指定界（pipeline 节点 `切到指定界`）
    作用：确保当前在**指定**的主界面（仙界 / 人间）。已在目标界则什么都不做（幂等）。
    实现：Custom 动作 `maa_agent_switch_zone`，界名由 custom_action_param.zone 传入。
    接线（只在需要指定界时用，插在「回到主界面」之后）：
        "通用_守卫出口": {"next": ["切到指定界"]}
        "切到指定界":   {"action": {"type": "Custom",
                                    "param": {"custom_action": "maa_agent_switch_zone",
                                              "custom_action_param": {"zone": "人间"}}},
                         "next": ["<你的任务主体>"]}

三个必须遵守的实现约束（都是实测踩出来的）
--------------------------------------------
1. **不能用 `controller.cached_image`**：它是缓存，只由框架的节点识别流程刷新。
   守卫循环里连续多轮判定时缓存不更新，会一直对着同一张旧图判定和点击。
   → 每轮用 `post_screencap()` 主动取新帧。
2. **一轮只截一帧、所有判定共用**：若每个探测各自截图，同一轮内不同判定会基于不同时刻
   的画面，出现「顶部命中、底部不命中」这种自相矛盾的组合（表现为在界面间来回横跳）。
   → 每轮取一帧 `frame`，把它传给所有探测函数。
3. **主界面判据必须配排除词**：`境界` 这类主界面文案都是通用词，活动说明弹窗正文里也会
   出现（实测「…境界符合的玩家均可参与圣兽封印」），不排除就会把弹窗误判成主界面。

硬失败语义
----------
超过 MAX_PROBES 轮仍未确认主界面 → 返回 False → 节点动作失败 → 任务链走 `on_error` 停下。
绝不在未知界面上继续点（乱点可能误触充值/购买）。

多主界面（表驱动，加界只加一行）
--------------------------------
`ZONES` 表列出「界名 → (识别 ROI, 该界独有特征词)」。目前 仙界 / 人间。
**加第三种主界面只需往表里加一行**，判定与恢复逻辑都不用改。

移植到别的游戏
--------------
只改下面「项目配置区」：ROI、文案、模板图名、ZONES 表。`检测原语` 与 `决策循环` 两节通用。
"""

from __future__ import annotations

import time

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JClick, JOCR, JTemplateMatch

# ==================== 项目配置区（换游戏只改这里） ====================

# --- 主界面判据：① 顶部玩家名 ② 主界面独有文案 ---
# ① 左上角玩家名。太古仙尊里玩家名带区服前缀（如 `s521.欧阳梓玥`），
#    所以只认区服号，换账号也不影响。换游戏改成该游戏主界面独有的一段文字。
PLAYER_EXPECTED = ["s521"]
PLAYER_ROI = (100, 10, 300, 60)  # (x, y, w, h)，帧坐标（短边 720）

# ② 主界面独有文案（OCR）。
#    ⚠️ 这一条原来用**模板匹配**底部功能条（宗门/游历），实测**不可靠**：
#    底部导航条的位置会随日志面板高度变化（同一账号同一分辨率下，某次实测宗门在
#    y≈1004，另一次在 y≈1130），模板一失效就判「不在主界面」。后果很严重 ——
#    守卫会去点「返回箭头」，而那个坐标其实是**左上角头像**，点开储物袋，
#    下一轮又点返回，**无限来回**（实测连续 20 轮耗尽后硬失败）。
#    改成认主界面底部的文字：OCR 对位置不敏感，跨分辨率/跨面板高度都稳。
#
#    ⚠️⚠️ 这三个词都是**通用词**，会被屏幕上的活动说明文字命中！实测踩坑：
#    某个弹窗里写着「…每日18:30持续至20:30境界符合的玩家均可参与圣兽」，
#    里面含「境界」，于是守卫误判「已在主界面」/或用它当特征导致状态判断混乱。
#    所以必须配一份**黑名单**：出现这些词说明有弹窗/活动说明，绝不算主界面。
MAIN_TEXTS = ["总修为", "当前修炼效率", "境界"]
MAIN_TEXT_ROI = (100, 820, 520, 130)
MAIN_TEXT_THRESHOLD = 0.5

# 主界面**排除词**：命中任一 ⇒ 有盖住主界面的弹窗/活动说明，不算主界面。
# ⚠️ 只放**弹窗专属**的词，绝不能放主界面日志里也会出现的词（日志里常见：
#    「获得奖励」「境界提升」），否则主界面会被自己底部的日志误排除。
MAIN_EXCLUDE_TEXTS = ["均可参与", "持续至", "预约", "邮箱发放"]
MAIN_EXCLUDE_ROI = (0, 500, 720, 620)  # 覆盖弹窗正文区（避开底部日志：日志在 y>1050）
MAIN_EXCLUDE_THRESHOLD = 0.5
# 上面这些弹窗（活动说明类）关闭用的 X 坐标。实测「玄武/圣兽封印」弹窗的 X 在 (620,178)，
# 背包弹窗的在 (611,253)。
POPUP_CLOSE_POINTS = [(620, 178), (611, 253), (636, 290)]

# ⚠️ 这个游戏有**多套主界面**（目前已知两套：仙界 / 人间（凡界））。
#    它们**顶部完全相同**（玩家名、总修为、境界、当前修炼效率），只有**底部导航**不同，
#    所以判据只能看底部导航的文字。
#
#    ★ 表驱动设计：以后再加一种主界面，**只往这张表里加一行**，判定逻辑与恢复逻辑
#      （点返回 / 点叉 / 关弹窗）都不用改 —— 恢复逻辑只管「不在主界面就往外退」，
#      退到哪一界都算成功。
#
#    每个条目：界名 → (识别用 ROI, 该界独有的特征词列表)
#    ROI 是底部导航条所在的横带；特征词必须**该界独有**（不能和别的界重复）。
ZONE_TEXTS_ROI = (0, 960, 720, 90)  # 默认 ROI：底部导航条
ZONES: dict[str, tuple[tuple, list[str]]] = {
    "仙界": (ZONE_TEXTS_ROI, ["宗门", "比斗", "太虚", "道使", "游历"]),
    "人间": (ZONE_TEXTS_ROI, ["福地", "门派", "洞府", "历练"]),
}
ZONE_TEXT_THRESHOLD = 0.6
ZONE_DEFAULT = "仙界"  # 两界同时命中时（理论上不会）取它，保持原行为

# 界切换按钮：左上角图标区**第二行最左**那个（实测文字框 (8,204,48,28)）。
# ⚠️ 它显示的是**要切过去的那个界**：实测在人间时它显示「仙界」。
#    所以「切到某界」的实现 = 读这个按钮的文字 → 若等于目标界就点它 → 若不是就说明已在目标界。
ZONE_SWITCH_ROI = (0, 190, 70, 60)
ZONE_SWITCH_POINT = (32, 218)
ZONE_SWITCH_TEXTS = ["仙界", "人间"]

# 模板判据保留作辅助（命中其一也算主界面）
MENU_TEMPLATES = ["主界面入口_宗门.png", "主界面入口_游历.png"]
MENU_ROI = (0, 950, 720, 280)
MENU_THRESHOLD = 0.9
MENU_MIN = 1  # 命中几个算数

# --- 弹窗关闭：右上角 X ---
# 实测教训：每个界面的 X 样式/位置都不同，单靠模板会失配（在「落潮驿站」弹窗上
# 用从其它弹窗裁的模板只有 0.415）。所以策略是「模板先试，固定坐标兜底」。
# ⚠️⚠️ ROI 必须**足够大以覆盖各种弹窗的 X**：实测「玄武/圣兽封印」弹窗的 X 在
#    (603,160,34,37)（中心 620,178），而背包「一键出售」弹窗的在 (611,253)。
#    原来 ROI=(560,250,150,130) 从 y=250 起，**根本框不到 y=160 的叉**，
#    于是模板永远不命中，只能靠兜底坐标乱点（点错 20 次才发现）。
CLOSE_TEMPLATES = ["叉1.png"]
CLOSE_ROI = (540, 120, 180, 220)
CLOSE_POINT = (620, 178)  # 兜底点：实测「玄武」弹窗 X 中心（模板命中 (603,160,34,37)）
TEMPLATE_THRESHOLD = 0.75

# --- 覆盖式弹窗：必须先关掉，否则它下面的按钮点不动 ---
# 实测教训：背包的「一键出售」弹窗盖住了储物袋左上角的返回箭头 —— 模板每轮都命中
# score=0.996，点击也「成功」，但落点属于弹窗，返回箭头纹丝不动，12 轮全部耗尽。
# 所以**先识别弹窗标题，再点弹窗自己的 X**，优先级高于返回箭头。
# 通用做法：往 OVERLAY_TITLES 里加「弹窗标题文案 → 该弹窗 X 的坐标」即可。
OVERLAY_TITLE_ROI = (200, 200, 320, 90)  # 标题文字所在区域（帧坐标）
OVERLAY_TITLES = [
    ("一键出售", (611, 253)),
]
OVERLAY_TITLE_THRESHOLD = 0.3

# --- 返回：左上角箭头 ---
BACK_TEMPLATES = ["返回箭头1.png", "返回箭头2.png"]
BACK_ROI = (0, 0, 130, 130)
BACK_POINT = (40, 42)  # 左上返回箭头的兜底点击位置

# --- 通用弹窗「确定」兜底 ---
# 很多弹窗只有底部一个「确定」；实测 OCR 很稳（0.9999）。当 X / 返回模板都认不到时点它。
CONFIRM_EXPECTED = ["确定"]
CONFIRM_ROI = (220, 900, 280, 130)

# 模板全失配时的兜底动作轮换顺序。四者的落点互不相同，所以轮换能覆盖更多界面：
#   "close"   → 右上角 X 固定坐标（弹窗右上角几乎都是关闭）
#   "back"    → 左上角返回固定坐标（子界面左上角几乎都是返回）
#   "confirm" → 底部「确定」OCR（带确认按钮的弹窗）
#   "x_naive" → 常见的右上角关闭坐标（用于 X 位置不同的弹窗，如「落潮驿站」）
FALLBACK_ACTIONS = ("close", "back", "confirm", "x_naive")
NAIVE_X_POINT = (530, 600)

# --- 「确定退出游戏吗？」弹窗 ---
# 实测（720x1280 帧）：文案「确定退出游戏吗？」在 (286,542,138,21)；
#   「再玩一会」在 (247,741,72,23) → 中心 (283,752)
#   「退出游戏」在 (400,733,70,21) → 中心 (435,743)
# STAY_ROI 必须**只覆盖「再玩一会」**，绝不能碰到「退出游戏」——点错就真退出了。
EXIT_DIALOG_EXPECTED = ["退出游戏吗"]
EXIT_DIALOG_ROI = (180, 520, 360, 120)
STAY_BUTTON_EXPECTED = ["再玩一会"]
STAY_BUTTON_ROI = (190, 700, 190, 110)
STAY_BUTTON_FALLBACK = (283, 752)  # 认不到文字时的兜底坐标（「再玩一会」中心）

# --- 循环参数 ---
# 为什么降噪只用 4 个灰阶分桶：实测同一格在不同帧会因为抗锯齿/动画漂移而得到不同指纹，
# 分桶太细（>>3 = 8 档）会产生抖动，反而让「卡住检测」误判成「有进展」。
STUCK_BUCKETS = 4
MAX_PROBES = 20
ACTION_WAIT = 1.5
OCR_THRESHOLD = 0.3
LOG_PREFIX = "[guard]"

# ==================== 检测原语（均接收外部帧，不自己截图） ====================


def _fresh_frame(ctx: Context) -> np.ndarray:
    """主动截一张新帧（短边 720 基准）。见模块 docstring 约束 1。"""
    return ctx.tasker.controller.post_screencap().wait().get()


def match_template(
    ctx: Context, frame: np.ndarray, templates: list[str], roi: tuple, threshold: float = TEMPLATE_THRESHOLD
):
    """模板匹配。返回 (hit, score, box)。"""
    detail = ctx.run_recognition_direct(
        "TemplateMatch",
        JTemplateMatch(template=list(templates), roi=roi, threshold=[threshold]),
        frame,
    )
    if detail is None or detail.best_result is None:
        return False, None, None
    return bool(detail.hit), getattr(detail.best_result, "score", None), getattr(detail.best_result, "box", None)


def match_ocr(ctx: Context, frame: np.ndarray, expected: list[str], roi: tuple, threshold: float = OCR_THRESHOLD):
    """OCR。返回 (hit, text, box)。"""
    detail = ctx.run_recognition_direct(
        "OCR",
        JOCR(expected=expected, roi=roi, threshold=threshold),
        frame,
    )
    if detail is None or not detail.hit or detail.best_result is None:
        return False, None, None
    return True, getattr(detail.best_result, "text", None), getattr(detail.best_result, "box", None)


def _click(ctx: Context, point: tuple[int, int]) -> bool:
    """点一个绝对坐标（帧坐标）。"""
    detail = ctx.run_action_direct("Click", JClick(target=point))
    return bool(detail is not None and detail.success)


def _center(box) -> tuple[int, int]:
    return box[0] + box[2] // 2, box[1] + box[3] // 2


# ==================== 判定（都接收同一帧） ====================


def current_zone(ctx: Context, frame: np.ndarray) -> str:
    """识别当前主界面是哪个界（表驱动，见配置区 ZONES）。返回界名，都不像则返回 ''。

    ⚠️ 判据只看底部导航文字 —— 各主界面顶部完全相同，只有底部导航不同。
       新加一种主界面只需往 ZONES 表里加一行。
    """
    hits: dict[str, list[str]] = {}
    for zone, (roi, words) in ZONES.items():
        got = [w for w in words
               if match_ocr(ctx, frame, [w], roi, ZONE_TEXT_THRESHOLD)[0]]
        if got:
            hits[zone] = got
    if not hits:
        return ""
    if len(hits) == 1:
        return next(iter(hits))
    # 多个界同时命中（特征词有重叠）：取命中词最多的那个；打平则取 ZONE_DEFAULT
    best = max(hits.items(), key=lambda kv: len(kv[1]))
    if list(hits.values()).count(best[1]) > 1:
        return ZONE_DEFAULT if ZONE_DEFAULT in hits else best[0]
    return best[0]


def is_main_ui(ctx: Context, frame: np.ndarray) -> tuple[bool, str]:
    """主界面判定：顶部玩家名 + 「底部界导航 / 主界面文案 / 底部模板」，且**不能命中排除词**。

    ⚠️ 排除词很关键：主界面文案（境界等）都是通用词，活动弹窗的说明文字里也会出现
       （实测「…境界符合的玩家均可参与圣兽」含「境界」），不排除就会误判。
    """
    # 先看排除词：命中任一条 ⇒ 有弹窗盖着，直接不算主界面
    excluded = [t for t in MAIN_EXCLUDE_TEXTS
                if match_ocr(ctx, frame, [t], MAIN_EXCLUDE_ROI, MAIN_EXCLUDE_THRESHOLD)[0]]
    if excluded:
        return False, f"排除词命中={excluded}（有弹窗/活动说明，不算主界面）"

    name_ok, name_text, _ = match_ocr(ctx, frame, PLAYER_EXPECTED, PLAYER_ROI)

    text_hits = []
    for t in MAIN_TEXTS:
        ok, got, _ = match_ocr(ctx, frame, [t], MAIN_TEXT_ROI, MAIN_TEXT_THRESHOLD)
        if ok:
            text_hits.append(got)

    zone = current_zone(ctx, frame)

    tpl_hits = []
    for tpl in MENU_TEMPLATES:
        ok, score, _ = match_template(ctx, frame, [tpl], MENU_ROI, MENU_THRESHOLD)
        if ok:
            tpl_hits.append(f"{tpl.replace('主界面入口_', '').replace('.png', '')}={score:.3f}")

    body_ok = bool(text_hits) or bool(zone) or len(tpl_hits) >= MENU_MIN
    ok = name_ok and body_ok
    return ok, f"顶部={'✓' if name_ok else '✗'}({name_text}) 界={zone or '?'} 文案={text_hits} 模板={tpl_hits}"


def exit_dialog_present(ctx: Context, frame: np.ndarray) -> bool:
    return match_ocr(ctx, frame, EXIT_DIALOG_EXPECTED, EXIT_DIALOG_ROI)[0]


def close_button(ctx: Context, frame: np.ndarray):
    return match_template(ctx, frame, CLOSE_TEMPLATES, CLOSE_ROI)


def back_button(ctx: Context, frame: np.ndarray):
    return match_template(ctx, frame, BACK_TEMPLATES, BACK_ROI)


def overlay_dialog(ctx: Context, frame: np.ndarray):
    """识别「覆盖式弹窗」标题，返回 (标题, 该弹窗 X 坐标) 或 (None, None)。"""
    for title, close_point in OVERLAY_TITLES:
        ok, text, _ = match_ocr(ctx, frame, [title], OVERLAY_TITLE_ROI, OVERLAY_TITLE_THRESHOLD)
        if ok:
            return text or title, close_point
    return None, None


def _frame_key(frame: np.ndarray) -> str:
    """把一帧压成很粗的签名，只用来判断「界面这一轮有没有变化」。"""
    small = frame[::32, ::32]
    step = max(1, 256 // STUCK_BUCKETS)
    return "".join(str(int(v) // step) for v in small.mean(axis=2).flatten())


# ==================== 决策循环 ====================


def ensure_main_ui(ctx: Context) -> tuple[bool, str]:
    """把界面拉回主界面。返回 (是否成功, 说明)。"""
    last_key: str | None = None
    stuck = 0
    for probe in range(1, MAX_PROBES + 1):
        frame = _fresh_frame(ctx)  # 一轮一帧，所有判定共用

        key = _frame_key(frame)
        if key == last_key:
            stuck += 1
        else:
            stuck = 0
        last_key = key

        # ① 退出确认弹窗必须最先处理。
        #    注意：它是覆盖层，主界面元素（顶部玩家名、底部功能条）仍可见，
        #    所以不能先判「已在主界面」就放行 —— 那会带着阻塞弹窗进入任务主体。
        if exit_dialog_present(ctx, frame):
            print(f"{LOG_PREFIX} 第 {probe} 轮：检测到退出确认弹窗，先保住游戏不退出", flush=True)
            stay_ok, stay_text, stay_box = match_ocr(ctx, frame, STAY_BUTTON_EXPECTED, STAY_BUTTON_ROI)
            if stay_ok and stay_box is not None:
                point = _center(stay_box)
            else:
                # 认不到文字就点「再玩一会」的固定中心 —— 绝不能落到「退出游戏」上
                point = STAY_BUTTON_FALLBACK
                print(f"{LOG_PREFIX}   未认出「再玩一会」，用兜底坐标 {point}", flush=True)
            print(f"{LOG_PREFIX}   → 点「再玩一会」{point}", flush=True)
            _click(ctx, point)
            time.sleep(ACTION_WAIT)
            continue

        # ② 活动说明类弹窗：正文含弹窗专属词（均可参与/持续至/预约/…）⇒ 先关掉它。
        #    为什么要专门一步：这类弹窗**不在 OVERLAY_TITLES 表里**（表是按弹窗标题登记的），
        #    而且它盖在子界面上时会挡住真正的返回按钮。实测踩坑：守卫一直去点「返回箭头」
        #    模板在 (36,38) 命中的东西（其实是背景标题「历练」的图标），点了 20 次毫无反应。
        overlay_hits = [t for t in MAIN_EXCLUDE_TEXTS
                        if match_ocr(ctx, frame, [t], MAIN_EXCLUDE_ROI, MAIN_EXCLUDE_THRESHOLD)[0]]
        if overlay_hits:
            # 先用模板精确定位这个弹窗的 X；认不到再用兜底坐标
            hit, score, box = close_button(ctx, frame)
            if hit and box is not None:
                point = _center(box)
                print(f"{LOG_PREFIX} 第 {probe} 轮：检测到活动/说明弹窗（命中 {overlay_hits}）"
                      f"→ 模板命中 X score={score:.3f} → 点 {point}", flush=True)
            else:
                point = POPUP_CLOSE_POINTS[min(stuck, len(POPUP_CLOSE_POINTS) - 1)]
                print(f"{LOG_PREFIX} 第 {probe} 轮：检测到活动/说明弹窗（命中 {overlay_hits}）"
                      f"→ 模板未命中，用兜底 X {point}", flush=True)
            _click(ctx, point)
            time.sleep(ACTION_WAIT)
            continue

        # ③ 没有阻塞弹窗，再判定是否已在主界面
        ok, detail = is_main_ui(ctx, frame)
        print(f"{LOG_PREFIX} 第 {probe} 轮：{'已在主界面 ✓' if ok else '不在主界面'}"
              f"{'（界面未变化 x%d）' % stuck if stuck >= 2 else ''} | {detail}", flush=True)
        if ok:
            return True, f"第 {probe} 轮确认主界面"

        # ③ 覆盖式弹窗：先点弹窗自己的 X。
        #    这一段必须在返回箭头之前 —— 弹窗盖住返回箭头时，点返回是无效动作（见配置区注释）。
        if stuck < 2:
            title, x_point = overlay_dialog(ctx, frame)
            if title:
                print(f"{LOG_PREFIX}   识别到弹窗「{title}」→ 点它的 X {x_point}", flush=True)
                _click(ctx, x_point)
                time.sleep(ACTION_WAIT)
                continue

        # ④ 其他弹窗：模板定位优先；模板失配就按固定坐标轮换兜底。
        #
        #    为什么要「轮换」而不是固定顺序选一个：X / 返回箭头 / 确定 都是位置固定但
        #    样式各异的元素，任何单一策略都会在某些界面上失效。轮换能保证每个候选都被试到 ——
        #    实测「落潮驿站」弹窗上模板 X 只有 0.415、模板返回只有 0.240，两个模板全废，
        #    但底部的「确定」OCR 有 0.9999，靠轮换才救得回来。
        #    界面连续两轮没变化 ⇒ 上一次的动作无效，不再重复它，直接换下一个。
        if stuck < 2:
            hit, score, box = close_button(ctx, frame)
            if hit and box is not None:
                point = _center(box)
                print(f"{LOG_PREFIX}   模板命中关闭按钮 score={score:.3f} → 点 {point}", flush=True)
                _click(ctx, point)
                time.sleep(ACTION_WAIT)
                continue

            hit, score, box = back_button(ctx, frame)
            if hit and box is not None:
                point = _center(box)
                print(f"{LOG_PREFIX}   模板命中返回箭头 score={score:.3f} → 点 {point}", flush=True)
                _click(ctx, point)
                time.sleep(ACTION_WAIT)
                continue

        # ⑤ 模板都不认 / 或已经卡住：轮流用固定坐标与 OCR 兜底。
        seq = FALLBACK_ACTIONS[probe % len(FALLBACK_ACTIONS):] + FALLBACK_ACTIONS[:probe % len(FALLBACK_ACTIONS)]
        action = seq[min(stuck, len(seq) - 1)]
        if action == "confirm":
            ok2, text, cbox = match_ocr(ctx, frame, CONFIRM_EXPECTED, CONFIRM_ROI)
            if ok2 and cbox is not None:
                point = _center(cbox)
                print(f"{LOG_PREFIX}   兜底：OCR 命中「{text}」→ 点 {point}", flush=True)
            else:
                point = CLOSE_POINT
                print(f"{LOG_PREFIX}   兜底：未认出「确定」→ 点右上角 X {point}", flush=True)
        elif action == "back":
            point = BACK_POINT
            print(f"{LOG_PREFIX}   兜底：点左上返回 {point}", flush=True)
        elif action == "x_naive":
            point = NAIVE_X_POINT
            print(f"{LOG_PREFIX}   兜底：点常见右上角关闭位 {point}", flush=True)
        else:
            point = CLOSE_POINT
            print(f"{LOG_PREFIX}   兜底：点右上角 X {point}", flush=True)
        _click(ctx, point)
        time.sleep(ACTION_WAIT)

    return False, f"探测 {MAX_PROBES} 轮仍未回到主界面"


@AgentServer.custom_action("maa_agent_guard_ensure_main")
class EnsureMainUIAction(CustomAction):
    """任务主体前的守卫：不在主界面就点返回/叉，拉不回来即失败。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        ok, detail = ensure_main_ui(context)
        print(f"{LOG_PREFIX} {'通过：' if ok else '失败：'}{detail}", flush=True)
        return ok


@AgentServer.custom_action("maa_agent_switch_zone")
class SwitchZoneAction(CustomAction):
    """切到指定的主界面（界名从 pipeline 的 custom_action_param 传进来，默认「仙界」）。

    实现：左上角图标区第二行最左那个按钮显示的是**要切过去的界**（实测在人间时显示
    「仙界」）。所以：
      · 读按钮文字 == 目标界  ⇒ 点它（切过去）
      · 读按钮文字 != 目标界  ⇒ 说明已在目标界，什么都不做（幂等）
      · 读不到按钮文字        ⇒ 用固定坐标兜底点一下，再复判

    用法（pipeline 里给参数）：
        "action": {"type":"Custom","param":{"custom_action":"maa_agent_switch_zone",
                                            "custom_action_param":{"zone":"人间"}}}
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        target = "仙界"
        param = getattr(argv, "custom_action_param", None)
        if isinstance(param, dict) and param.get("zone"):
            target = str(param["zone"])

        frame = _fresh_frame(ctx)
        zone = current_zone(ctx, frame)
        print(f"{LOG_PREFIX} 切换界：当前={zone or '?'} 目标={target}", flush=True)
        if zone == target:
            print(f"{LOG_PREFIX}   已在「{target}」，无需切换 ✓", flush=True)
            return True

        ok, text, box = match_ocr(ctx, frame, ZONE_SWITCH_TEXTS, ZONE_SWITCH_ROI)
        if ok and text:
            if text == target:
                point = _center(box) if box is not None else ZONE_SWITCH_POINT
                print(f"{LOG_PREFIX}   切换按钮显示「{text}」= 目标 → 点 {point}", flush=True)
            else:
                print(f"{LOG_PREFIX}   切换按钮显示「{text}」≠ 目标，但仍点一下试试", flush=True)
                point = _center(box) if box is not None else ZONE_SWITCH_POINT
        else:
            point = ZONE_SWITCH_POINT
            print(f"{LOG_PREFIX}   未读出切换按钮文字 → 用兜底坐标 {point}", flush=True)

        _click(ctx, point)
        time.sleep(ACTION_WAIT)

        frame = _fresh_frame(ctx)
        zone2 = current_zone(ctx, frame)
        ok2 = zone2 == target
        print(f"{LOG_PREFIX}   切换后={zone2 or '?'} → {'成功 ✓' if ok2 else '未成功 ✗'}", flush=True)
        return ok2
