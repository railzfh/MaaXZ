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
CLOSE_TEMPLATES = ["叉1.png"]
CLOSE_ROI = (560, 260, 130, 100)
CLOSE_POINT = (602, 321)
TEMPLATE_THRESHOLD = 0.75

# --- 返回：左上角箭头 ---
BACK_TEMPLATES = ["返回箭头1.png", "返回箭头2.png"]
BACK_ROI = (0, 0, 110, 110)
BACK_POINT = (36, 36)

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

        # ③ 其他弹窗（右上角 X）→ ④ 返回箭头 → ⑤ 兜底点左上
        hit, score, box = close_button(ctx, frame)
        if hit and box is not None:
            x, y = _center(box)
            print(f"{LOG_PREFIX}   命中关闭按钮 score={score:.3f} → 点 ({x},{y})", flush=True)
            _click(ctx, (x, y))
            time.sleep(ACTION_WAIT)
            continue

        hit, score, box = back_button(ctx, frame)
        if hit and box is not None:
            x, y = _center(box)
            print(f"{LOG_PREFIX}   命中返回箭头 score={score:.3f} → 点 ({x},{y})", flush=True)
            _click(ctx, (x, y))
            time.sleep(ACTION_WAIT)
            continue

        print(f"{LOG_PREFIX}   未认出关闭/返回按钮 → 兜底点左上 {BACK_POINT}", flush=True)
        _click(ctx, BACK_POINT)
        time.sleep(ACTION_WAIT)

    return False, f"探测 {MAX_PROBES} 轮仍未回到主界面"


@AgentServer.custom_action("maa_agent_guard_ensure_main")
class EnsureMainUIAction(CustomAction):
    """任务主体前的守卫：不在主界面就点返回/叉，拉不回来即失败。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        ok, detail = ensure_main_ui(context)
        print(f"{LOG_PREFIX} {'通过：' if ok else '失败：'}{detail}", flush=True)
        return ok
