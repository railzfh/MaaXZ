"""通用前置守卫：确保任务主体开始时的界面是「游戏主界面」。

用法
----
pipeline 里加一个入口节点（见 `assets/resource/pipeline/通用_确保主界面.json`）：

```jsonc
{
    "通用_确保主界面": {
        "doc": "万能前置守卫：把界面拉回主界面，拉不回来就硬失败",
        "recognition": { "type": "DirectHit" },
        "action": { "type": "Custom", "param": { "custom_action": "maa_agent_guard_ensure_main" } }
    }
}
```

以后每个任务的 next 首步都指向它：`"next": ["通用_确保主界面", "任务第一步"]`。

两个必须遵守的实现约束（都是实测踩出来的）
--------------------------------------------
1. **不能用 `controller.cached_image`**：它是缓存，只由框架的节点识别流程刷新。
   守卫循环里连续多轮判定时缓存不更新，会一直对着同一张旧图判定和点击。
   → 每轮用 `post_screencap()` 主动取新帧。
2. **一轮只截一帧、所有判定共用**：若每个探测各自截图，同一轮内不同判定会基于不同时刻
   的画面，出现「顶部命中、底部不命中」这种自相矛盾的组合（表现为在界面间来回横跳）。
   → 每轮取一帧 `frame`，把它传给所有探测函数。

硬失败语义
----------
超过 MAX_PROBES 轮仍未确认主界面 → 返回 False → 节点动作失败 → 任务链走 `on_error` 停下。
绝不在未知界面上继续点（乱点可能误触充值/购买）。

移植到别的游戏
--------------
只改下面「项目配置区」：ROI、文案、模板图名。`检测原语` 与 `决策循环` 两节通用。
"""

from __future__ import annotations

import time

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JClick, JOCR, JTemplateMatch

# ==================== 项目配置区（换游戏只改这里） ====================

# --- 主界面判据：① 顶部玩家名 ② 底部功能条 ---
# ① 左上角玩家名。太古仙尊里玩家名带区服前缀（如 `s521.欧阳梓玥`），
#    所以只认区服号，换账号也不影响。换游戏改成该游戏主界面独有的一段文字。
PLAYER_EXPECTED = ["s521"]
PLAYER_ROI = (100, 10, 300, 60)  # (x, y, w, h)，帧坐标（短边 720）

# ② 底部功能条：**用模板匹配而不是 OCR**。实测同一张主界面截图：
#      模板 宗门/游历 在主界面命中 0.999998~0.999999，在子界面（储物袋）只有 0.326；
#    而 OCR 认这几个中文词很不稳（实测只认到 1/5，还容易把「比斗」看成「比武」）。
#    这一条是区分「主界面 vs 子界面」的关键（子界面同区域是物品/正文）。
MENU_TEMPLATES = ["主界面入口_宗门.png", "主界面入口_游历.png"]
MENU_ROI = (0, 950, 720, 140)
MENU_THRESHOLD = 0.9
MENU_MIN = 1  # 命中几个算数（两个模板各 1，命中任一即可）

# --- 弹窗关闭：右上角 X ---
# 实测教训：每个界面的 X 样式/位置都不同，单靠模板会失配（在「落潮驿站」弹窗上
# 用从其它弹窗裁的模板只有 0.415）。所以策略是「模板先试，固定坐标兜底」——
# X 是固定位置元素，兜底坐标比模板更可靠（见 docs pipeline-v2 §4）。
CLOSE_TEMPLATES = ["叉1.png"]
CLOSE_ROI = (560, 250, 150, 130)
CLOSE_POINT = (636, 290)  # 右上角 X 的兜底点击位置（帧坐标）
TEMPLATE_THRESHOLD = 0.75

# --- 返回：左上角箭头 ---
BACK_TEMPLATES = ["返回箭头1.png", "返回箭头2.png"]
BACK_ROI = (0, 0, 130, 130)
BACK_POINT = (40, 42)  # 左上返回箭头的兜底点击位置

# --- 通用弹窗「确定」兜底 ---
# 很多弹窗只有底部一个「确定」；实测 OCR 很稳（0.9999）。当 X / 返回模板都认不到时点它。
CONFIRM_EXPECTED = ["确定"]
CONFIRM_ROI = (220, 900, 280, 130)

# 模板全失配时的兜底动作轮换顺序（按轮次取模）。三者的依据不同，所以轮换能覆盖更多界面：
#   "close"   → 右上角 X 固定坐标（弹窗右上角几乎都是关闭）
#   "back"    → 左上角返回固定坐标（子界面左上角几乎都是返回）
#   "confirm" → 底部「确定」OCR（带确认按钮的弹窗）
FALLBACK_ACTIONS = ("close", "back", "confirm")

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
MAX_PROBES = 12
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


def is_main_ui(ctx: Context, frame: np.ndarray) -> tuple[bool, str]:
    """主界面判定：顶部玩家名 + 底部功能条模板命中，两个条件同时成立。"""
    name_ok, name_text, _ = match_ocr(ctx, frame, PLAYER_EXPECTED, PLAYER_ROI)
    hits = []
    for tpl in MENU_TEMPLATES:
        ok, score, _ = match_template(ctx, frame, [tpl], MENU_ROI, MENU_THRESHOLD)
        if ok:
            hits.append(f"{tpl.replace('主界面入口_', '').replace('.png', '')}={score:.3f}")
    menu_ok = len(hits) >= MENU_MIN
    ok = name_ok and menu_ok
    return ok, f"顶部={'✓' if name_ok else '✗'}({name_text}) 底部模板={hits}"


def exit_dialog_present(ctx: Context, frame: np.ndarray) -> bool:
    return match_ocr(ctx, frame, EXIT_DIALOG_EXPECTED, EXIT_DIALOG_ROI)[0]


def close_button(ctx: Context, frame: np.ndarray):
    return match_template(ctx, frame, CLOSE_TEMPLATES, CLOSE_ROI)


def back_button(ctx: Context, frame: np.ndarray):
    return match_template(ctx, frame, BACK_TEMPLATES, BACK_ROI)


# ==================== 决策循环 ====================


def ensure_main_ui(ctx: Context) -> tuple[bool, str]:
    """把界面拉回主界面。返回 (是否成功, 说明)。"""
    for probe in range(1, MAX_PROBES + 1):
        frame = _fresh_frame(ctx)  # 一轮一帧，所有判定共用

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

        # ② 没有阻塞弹窗，再判定是否已在主界面
        ok, detail = is_main_ui(ctx, frame)
        print(f"{LOG_PREFIX} 第 {probe} 轮：{'已在主界面 ✓' if ok else '不在主界面'} | {detail}", flush=True)
        if ok:
            return True, f"第 {probe} 轮确认主界面"

        # ③ 其他弹窗：模板定位优先；模板失配就按固定坐标轮换兜底。
        #
        #    为什么要「轮换」而不是固定顺序选一个：X / 返回箭头 / 确定 都是位置固定但
        #    样式各异的元素，任何单一策略都会在某些界面上失效。轮换能保证每个候选都被试到 ——
        #    实测「落潮驿站」弹窗上模板 X 只有 0.415、模板返回只有 0.240，两个模板全废，
        #    但底部的「确定」OCR 有 0.9999，靠轮换才救得回来。
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

        # 模板都不认：轮流用「右上 X 固定坐标 / 左上返回固定坐标 / 底部确定(OCR)」
        action = FALLBACK_ACTIONS[probe % len(FALLBACK_ACTIONS)]
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
