"""背包（储物袋）的基础操作：打开储物袋、打开一键出售弹窗。

为什么把这两步**从整理流程里拆出来做成独立 pipeline 节点**
----------------------------------------------------------
- 可单独调试：每一步有自己的识别判据，跑单节点就能看到「认到了什么、点了哪」，
  不用每次都把整个整理流程跑一遍。
- 可复用：以后别的背包相关任务（清装备、卖材料、看容量）都要先走这两步。
- 出问题好定位：是「没打开背包」还是「没打开弹窗」，日志直接告诉你。

节点设计（V2 格式）
------------------
每个节点都是「**识别 = 验证当前界面** + **动作 = 点进下一界面**」：

| 节点              | 识别                          | 动作            |
|-------------------|-------------------------------|-----------------|
| 打开储物袋        | 主界面特征（宗门/游历 + s521）| 点左上角头像    |
| 打开储物袋_校验   | 储物袋标题（OCR）             | 无              |
| 打开一键出售      | 储物袋的「一键出售」按钮（OCR）| 点该按钮        |
| 打开一键出售_校验 | 弹窗标题「一键出售」（OCR）   | 无              |

这样「打开储物袋」节点天然**幂等**：已经在储物袋里时识别不通过，就不会重复点头像
（重复点头像会关上背包）。校验节点失败会走 on_error 停下，不会带着错的界面继续跑。
"""

from __future__ import annotations

import threading
import time

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JClick, JOCR, JTemplateMatch

# ==================== 配置区（720x1280 帧坐标） ====================

# --- 主界面判据：**复用 guard 的那一份** ---
# ⚠️ 不要在这里维护第二份副本。之前两处各写一套，guard 修好了（改成 OCR 文案判定）、
# pack 还留着旧的模板判定，结果 pack 判「不在主界面」直接拒绝点头像。
# 同一个概念只能有一个权威实现。
from guard import is_main_ui as _is_main_ui  # noqa: E402

# --- 储物袋 ---
# 实测：标题「储物袋」文字框 (73,14,117,45)。
# ⚠️ ROI 必须**完整包含**目标文字：一开始用 (100,5,220,80) 时左边把字切掉了，
# 引擎只能返回一个被裁的框 (0,0,82,81)/「口」，于是判不通过 —— 明明认得出却报失败。
# 放宽到含返回箭头也没关系：expected 只匹配「储物袋」，箭头认成别的字不影响。
BAG_TITLE_EXPECTED = ["储物袋"]
BAG_TITLE_ROI = (60, 0, 180, 80)
AVATAR_POINT = (56, 56)  # 主界面左上角头像（点它开储物袋）

# --- 一键出售弹窗 ---
# 底部两个按钮（实测文字框，帧坐标）：
#   「一键出售」(110,1214,107,34) → 中心 (163,1231)
#   「整理」    (523,1213, 69,36) → 中心 (557,1231)
# ⚠️ ROI 必须完整包住文字：第一版用 (100,1195,160,65) 时右边被切掉，OCR 只返回
# 「建出售」(150,1225,63,19)，命中点偏左 20px（按钮很宽所以没出事，但属于隐患）。
SELL_BUTTON_EXPECTED = ["键出售"]  # 只认「键出售」——避免「整理」被误认
SELL_BUTTON_ROI = (60, 1190, 200, 70)
SELL_BUTTON_FALLBACK = (163, 1231)  # 认不到文字时的兜底点击位（按钮中心）
SORT_BUTTON_EXPECTED = ["整理"]
SORT_BUTTON_ROI = (460, 1190, 200, 70)
SORT_BUTTON_POINT = (557, 1231)
SORT_WAIT = 1.8  # 点整理后等列表重排
DIALOG_TITLE_EXPECTED = ["一键出售"]
DIALOG_TITLE_ROI = (240, 210, 260, 70)

# --- 时序 ---
BAG_WAIT = 2.5  # 点头像后等储物袋打开
DIALOG_WAIT = 2.0  # 点按钮后等弹窗打开
OCR_THRESHOLD = 0.3
LOG = "[pack]"
FRAME_TIMEOUT = 20.0

# --- 关闭用的固定坐标 ---
CLOSE_POINT = (611, 253)  # 出售弹窗右上角 X
BACK_POINT = (40, 42)  # 储物袋左上角返回箭头


# ==================== 基础原语 ====================

def _frame(ctx: Context) -> np.ndarray:
    """主动取新帧（cached_image 不自动刷新）。带超时，防 screencap 卡死。"""
    box: dict = {}

    def _grab():
        try:
            box["img"] = ctx.tasker.controller.post_screencap().wait().get()
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=_grab, daemon=True)
    t.start()
    t.join(FRAME_TIMEOUT)
    if t.is_alive():
        raise TimeoutError(f"screencap 超过 {FRAME_TIMEOUT}s 未返回")
    if "err" in box:
        raise box["err"]
    return box["img"]


def _ocr(ctx: Context, frame: np.ndarray, expected: list[str], roi: tuple, threshold: float = OCR_THRESHOLD):
    detail = ctx.run_recognition_direct("OCR", JOCR(expected=expected, roi=roi, threshold=threshold), frame)
    if detail is None or not detail.hit or detail.best_result is None:
        return False, None, None
    return True, getattr(detail.best_result, "text", None), tuple(detail.best_result.box)


def _match(ctx: Context, frame: np.ndarray, template: str, roi: tuple, threshold: float = 0.9):
    detail = ctx.run_recognition_direct(
        "TemplateMatch", JTemplateMatch(template=[template], roi=roi, threshold=[threshold]), frame)
    if detail is None or not detail.hit or detail.best_result is None:
        return False, None, None
    return True, getattr(detail.best_result, "score", None), tuple(detail.best_result.box)


def _click(ctx: Context, point: tuple[int, int]) -> bool:
    detail = ctx.run_action_direct("Click", JClick(target=point))
    return bool(detail is not None and detail.success)


def _center(box) -> tuple[int, int]:
    return int(box[0] + box[2] // 2), int(box[1] + box[3] // 2)


# ==================== 判据 ====================

def in_main_ui(ctx: Context, frame: np.ndarray) -> tuple[bool, str]:
    """主界面判定 —— 直接委托给 guard 的权威实现（见配置区注释）。"""
    return _is_main_ui(ctx, frame)


def in_bag(ctx: Context, frame: np.ndarray) -> tuple[bool, str]:
    ok, text, box = _ocr(ctx, frame, BAG_TITLE_EXPECTED, BAG_TITLE_ROI)
    return ok, f"储物袋标题={'✓' if ok else '✗'}({text}) box={box}"


def in_sell_dialog(ctx: Context, frame: np.ndarray) -> tuple[bool, str]:
    ok, text, box = _ocr(ctx, frame, DIALOG_TITLE_EXPECTED, DIALOG_TITLE_ROI)
    return ok, f"弹窗标题={'✓' if ok else '✗'}({text}) box={box}"


# ==================== 节点动作 ====================

@AgentServer.custom_action("maa_agent_open_bag")
class OpenBagAction(CustomAction):
    """打开储物袋：只在「确认在主界面且不在储物袋」时才点头像。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        frame = _frame(context)
        ok_bag, bag_detail = in_bag(context, frame)
        print(f"{LOG} 打开储物袋：当前 {bag_detail}", flush=True)
        if ok_bag:
            print(f"{LOG}   已在储物袋，无需操作 ✓", flush=True)
            return True

        ok_main, main_detail = in_main_ui(context, frame)
        print(f"{LOG}   主界面判定：{'✓' if ok_main else '✗'} | {main_detail}", flush=True)
        if not ok_main:
            print(f"{LOG} !! 既不在储物袋也不在主界面，拒绝点头像（乱点可能误触）", flush=True)
            return False

        print(f"{LOG}   → 点左上角头像 {AVATAR_POINT}", flush=True)
        if not _click(context, AVATAR_POINT):
            print(f"{LOG} !! 点击失败", flush=True)
            return False
        time.sleep(BAG_WAIT)

        frame = _frame(context)
        ok_bag, bag_detail = in_bag(context, frame)
        print(f"{LOG}   重新判定：{bag_detail}", flush=True)
        return ok_bag


@AgentServer.custom_action("maa_agent_open_sell")
class OpenSellAction(CustomAction):
    """打开「一键出售」弹窗：只在「确认在储物袋且弹窗未开」时才点按钮。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        frame = _frame(context)
        ok_dlg, dlg_detail = in_sell_dialog(context, frame)
        print(f"{LOG} 打开一键出售：当前 {dlg_detail}", flush=True)
        if ok_dlg:
            print(f"{LOG}   弹窗已开着，无需操作 ✓", flush=True)
            return True

        ok_bag, bag_detail = in_bag(context, frame)
        print(f"{LOG}   储物袋判定：{'✓' if ok_bag else '✗'} | {bag_detail}", flush=True)
        if not ok_bag:
            print(f"{LOG} !! 不在储物袋里，拒绝点「一键出售」（按钮不存在，乱点会点到物品格）", flush=True)
            return False

        ok_btn, btn_text, btn_box = _ocr(context, frame, SELL_BUTTON_EXPECTED, SELL_BUTTON_ROI)
        if ok_btn and btn_box is not None:
            point = _center(btn_box)
            print(f"{LOG}   OCR 命中「{btn_text}」box={btn_box} → 点 {point}", flush=True)
        else:
            point = SELL_BUTTON_FALLBACK
            print(f"{LOG}   未认出按钮文字 → 用兜底坐标 {point}", flush=True)

        if not _click(context, point):
            print(f"{LOG} !! 点击失败", flush=True)
            return False
        time.sleep(DIALOG_WAIT)

        frame = _frame(context)
        ok_dlg, dlg_detail = in_sell_dialog(context, frame)
        print(f"{LOG}   重新判定：{dlg_detail}", flush=True)
        return ok_dlg


@AgentServer.custom_action("maa_agent_sort_bag")
class SortBagAction(CustomAction):
    """点「整理」按钮，让同种物品重新聚到一起。

    ⚠️ 为什么每轮卖之前都要点：卖掉几格之后，剩下的同类物品**不再彼此相邻**（中间空出来的
    位置会被后面的物品顶上，种类就交错了）。而「每种保留前 2 格」这个规则依赖
    「同种物品在顺序里连续出现」—— 不整理的话，同一个种类会被拆成好几段，
    每段各自留下 2 格，最后一种物品留一大堆，完全卖不对。
    所以标准的循环是：**点整理 → 点一键出售 → 认选区 → 勾选 → 出售**。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        frame = _frame(context)
        ok_dlg, _d = in_sell_dialog(context, frame)
        if ok_dlg:
            print(f"{LOG} 点整理：出售弹窗开着，先关掉它", flush=True)
            _click(context, CLOSE_POINT)
            time.sleep(1.2)
            frame = _frame(context)

        ok_bag, bag_detail = in_bag(context, frame)
        if not ok_bag:
            print(f"{LOG} !! 点整理失败：不在储物袋里（{bag_detail}）", flush=True)
            return False

        ok, text, box = _ocr(context, frame, SORT_BUTTON_EXPECTED, SORT_BUTTON_ROI)
        if ok and box is not None:
            point = _center(box)
            print(f"{LOG} 点整理：OCR 命中「{text}」box={box} → 点 {point}", flush=True)
        else:
            point = SORT_BUTTON_POINT
            print(f"{LOG} 点整理：未认出文字 → 用兜底坐标 {point}", flush=True)
        if not _click(context, point):
            print(f"{LOG} !! 点整理失败", flush=True)
            return False
        time.sleep(SORT_WAIT)
        print(f"{LOG} 整理完成", flush=True)
        return True


@AgentServer.custom_action("maa_agent_close_bag")
class CloseBagAction(CustomAction):
    """关掉储物袋（回主界面）：先关弹窗、再点返回箭头。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        for i in range(4):
            frame = _frame(context)
            ok_bag, bag_detail = in_bag(context, frame)
            print(f"{LOG} 关背包 第 {i + 1} 轮：{bag_detail}", flush=True)
            if not ok_bag:
                ok_main, main_detail = in_main_ui(context, frame)
                print(f"{LOG}   {main_detail}", flush=True)
                return ok_main
            if in_sell_dialog(context, frame)[0]:
                print(f"{LOG}   → 先关弹窗 X {CLOSE_POINT}", flush=True)
                _click(context, CLOSE_POINT)
            else:
                print(f"{LOG}   → 点返回箭头 {BACK_POINT}", flush=True)
                _click(context, BACK_POINT)
            time.sleep(1.5)
        return False
