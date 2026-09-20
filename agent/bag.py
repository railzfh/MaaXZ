"""整理背包（一键出售）：每种物品只留 2 格，其余勾选出售。

算法（选区式，本项目的核心思路）
================================
不扫全表，而是**反复只看头部那几行**：

**阶段 1（翻动前，选区 `ROI_BEFORE`）**
1. 在选区里认格子。顺序是**行优先、行内从左到右**：第 1 行左→右数 1~4，
   第 2 行从头接第 5 个……（`ordered_tiles` 负责这个拍平顺序）
2. 按这个顺序数每种物品出现次数：第 1、2 个保留，**第 3 个起全部勾上**，点一次出售。
3. 卖完**关掉弹窗、再点一次一键出售**。每种只剩 2 格后会向上坍缩，头部被「已处理完」
   的物品占满 —— 新一轮里这些格子自然 ≤2 格直接跳过，**没处理的物品被顶进选区**。
4. 重复到**选区内每种都 ≤2 格**（说明头部已整理干净）。

**阶段 2（翻动后，选区 `ROI_AFTER`）**
5. **大翻动**一口气翻到列表最后：固定手势（用户框选的线 `[356,385,4,436]`，从底端
   y=821 向上拉 436，0.2 秒）× 固定 3 次。用户实机标定「这个长度划 3 次一定能到底」，
   所以不做「探测到底」的循环，行为可预测。
6. 在翻动后的选区里从**后往前**（最后一行往上、行内从右到左）同样做一遍。

两个选区不同是实测结论
----------------------
翻动前后内容区整体会下移，头部能完整露出的范围也跟着变，所以是两个略微偏移的框：
翻动前 `[108,349,504,434]`、翻动后 `[111,412,493,428]`（用户实测框选）。
共用一个框会导致翻动后少认一行。

为什么这样比「翻页扫全表」好
----------------------------
- 不需要跨屏去重（每次只看固定几行）。
- 天然的**幂等收敛**：卖完坍缩让选区内容自动前进，不依赖精确控制滚动位置。
- 每轮都有明确退出条件（选区内无 >2 格），不会因惯性滑动的不确定性跑飞。

安全约定
--------
「出售」**不可逆**。本 action 会真的点出售（这是本方案的要求），所以：
- `maa_agent_bag_preview`（dry）走完全部识别与报数，但**一格都不点、出售也不点** ——
  先用它确认选区识别与计数无误，再跑正式版。
- 只有「选区内某物品 >2 格」成立时才勾选并出售；没有可卖内容时立即停手。

规则（已确认）
--------------
- 每种物品**总共只留 2 格**（不论占几格、跨几屏），多出来的全部勾选待售。
- 只处理「材料」页签，不碰「装备」。

为什么用「模板命中框」而不是「数量数字框」定位格子
--------------------------------------------------
最初的设计是 OCR 出数量数字（999/325…），再把数字框按固定偏移推回格心。
实测这个偏移**算不准**：不同行/不同缩放下偏移会漂，实测算出来 (113,402) 而模板
自身命中的框中心是 (168,397) —— 差了整整 55px（约半个格子宽）。定位一错，点击就
落到隔壁格子，分类也拿到邻居的图标。

改成：**模板匹配的命中框就是格子**。
- 位置：直接用命中框中心（不再做任何偏移推算）。
- 分类：模板名天然就是分类结果（`all_results` 的 label 是空的，所以必须一个模板一次
  调用，模板名由我们自己持有）。
- 同一格子被多个模板命中时取分数最高者。

数量数字仍然 OCR，但**只用来判断"这一屏有没有内容 / 是否到底"**，不参与定位。

阈值
----
实测分数**双峰分布**：完整可见的格子 0.95~0.999，被视口边缘裁掉一半的 0.26~0.65，
中间几乎没有数据，所以 0.85 正好切在谷底。低分必须判「不认识」而不是「勉强算它」，
否则会把计数灌高、把该留的 2 格算错。宁可不勾，绝不误卖。

滑动（踩了很久的坑）
--------------------
- 「上滑/下滑」指**列表内容**往哪走，不是手指往哪走：
  手指**从上往下**划（框 y 450→600）⇒ 内容上移、看到**后面**的格子。
- 手势必须落在可拖拽区内：实测可拖区 = GRID_AREA（框 x 110~612 / y 353~846）。
- 一次滑动推进**一行**（实测行距 ~107），靠行间重叠去重。

换物品 / 换游戏
--------------
模板丢进 assets/resource/image/bag_items/ 即可；坐标见「配置区」。
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JClick, JOCR, JTemplateMatch

# ==================== 配置区（720x1280 帧坐标） ====================

ITEMS_TEMPLATE_DIR = "bag_items"
# 分类阈值：见模块 docstring「阈值」
ITEM_TEMPLATE_THRESHOLD = 0.85

# 出售弹窗
DIALOG_TITLE_ROI = (240, 210, 260, 70)
DIALOG_TITLE_EXPECTED = ["一键出售"]
TAB_MATERIAL_POINT = (174, 312)  # 「材料」页签
CLOSE_POINT = (611, 253)  # 弹窗右上角 X

# 网格：可滑动区（用户实测 [110,353,502,493]）
GRID_AREA = (110, 353, 502, 493)  # (x, y, w, h) → x 110~612, y 353~846

# 模板匹配范围：整个弹窗内容区（图标都在这里）
# 注意：真正的识别选区用 ROI_BEFORE / ROI_AFTER（翻动前后不同），
# 这个只用于「兜底诊断」时看得更宽一点。
TEMPLATE_ROI = (100, 255, 470, 600)
# 同一模板的多个命中框若中心距离小于此值，视为同一格（去重同格重复命中）
SAME_TILE_DIST = 45
# 同一个格子在相邻两屏会被重复扫到，用图标内容指纹去重
FP_SIZE = 64
FP_BLOCK = 16

# 勾选
TAP_WAIT = 0.5

# 拖动手势的插值步数。
# ⚠️ 这个常量被删过一次 —— 删旧滚动常量时误删，结果 _touch_drag 每次都抛
# `name 'DRAG_STEPS' is not defined`，大翻动 3 次全是空操作，整个阶段 2 都跑在地面上。
DRAG_STEPS = 8
# _touch_drag 在 hold_ms=None 时用的默认值（当前所有调用都显式传参，但保留默认值更安全）
HOLD_MS = 200
END_HOLD_MS = 350
SWIPE_X = 358

# 卡死检测用的「上一轮状态签名」（见 cycle_once）
_LAST_SIG: list[str] = [""]

# 弹窗内的「材料」页签（打开弹窗的前置步骤在 pack.py / pipeline/储物袋.json）
TAB_ROI_HINT = (100, 270, 300, 70)

# ---- 两个识别选区（用户实测框选，翻动前后不同）----
# ⚠️ 翻动前 / 翻动后是**两个不同的区域**，有略微偏移，不能共用一个：
#   翻动前 [108,349,504,434]（列表在顶时的前 4 行）
#   翻动后 [111,412,493,428]（翻到底之后，内容区整体下移）
#
# 用户给的两个框高度都是 ~430（约 4 行量级），但下边界**正好切在数字框底部**
# （实测数字行 y = 425/532/639/746，数字框底 = 768，选区下沿 783）。
# 结果：被底栏压住的下一行（格子只剩上半截）整个被排除在外，那些物品不会被卖。
# 用户要求「部分被遮住没关系，能识别出来就卖」⇒ 下方各放宽约一行，
# 让被裁的格子也能进选区。整框判定仍要求框完整落在选区内，只是允许框本身比满格矮，
# 所以放宽不会把「只露一条边」的垃圾命中放进来。
ROI_BEFORE = (108, 349, 504, 520)
ROI_AFTER = (111, 412, 493, 500)

# 顺序：**行优先、行内从左到右** —— 第 1 行左→右数 1~4，第 2 行从头接第 5 个…
# 所以先把命中框按 y 分组成行，行内按 x 排序；反向时行序取反（从最后一行往上）。
ROW_TOL = 45  # 同一行的 y 容差：命中框中心 y 相差小于它就算同一行

# ---- 窗口式卖法（本项目主算法，见模块 docstring）----
KEEP_IN_WINDOW = 2  # 窗口内每种保留的格数；第 3 格起全部勾掉

# 出售按钮（弹窗底部）。
# 实测文字框：(323,921,74,36) 与 (323,930,71,27) → 中心 ≈ (360,935)。
# ⚠️ 两个坑：
#   ① 兜底坐标原写 (307,995)（y 偏 56px）点在「出售数量」那行文字上，点击无声失败。
#   ② ROI 上边界若低于 y≈900，会把「出售数量：N」那行也框进来 —— 它含子串「出售」，
#      且 y 更靠上（实测 877），于是 **expected=["出售"] 先命中它**，点击落到计数文字上。
#      ROI 必须从按钮上方一点开始，把计数行排除掉。
SELL_CLICK_POINT = (360, 935)
SELL_CLICK_ROI = (200, 900, 320, 100)
SELL_CLICK_EXPECTED = ["出售"]
SELL_TEXT_MIN_W = 70  # 「出售」两字正常宽 ~74px；「出售数量」被裁后会更宽，用它兜底
SELL_WAIT = 1.5  # 点出售后等确认框冒出

# 点出售后还会弹一个确认框，需要点「确定」（用户给的选区 [419,740,145,69]）
# 实测 OCR 定位到 (463,755,58,36) → 中心 (492,773)，与上述选区中心 (491,774) 一致 ✓
CONFIRM_POINT = (491, 774)
CONFIRM_ROI = (400, 700, 200, 140)
CONFIRM_EXPECTED = ["确定"]
CONFIRM_WAIT = 2.0  # 点确定后等结算

# 「出售数量：N」计数区（用来校验真的成交了：卖完应归 0）
SELL_COUNT_ROI = (230, 850, 340, 70)

# 「整理」按钮（在**储物袋**底部，不在弹窗里 —— 弹窗打开时它被盖住）。
# ⚠️ 每轮卖之前必须点它：卖掉几格后同类物品不再相邻（空位被后面的物品顶上，种类交错），
# 而「每种保留前 2 格」依赖「同种在顺序里连续出现」。不整理的话同一种会被拆成多段、
# 每段各留 2 格，最后一种物品留一大堆，完全卖不对。
SORT_BUTTON_ROI = (460, 1190, 200, 70)
SORT_BUTTON_EXPECTED = ["整理"]
SORT_BUTTON_POINT = (557, 1231)  # 实测文字框 (523,1213,69,36) → 中心
SORT_WAIT = 1.8
BAG_TITLE_ROI = (60, 0, 180, 80)
BAG_TITLE_EXPECTED = ["储物袋"]
SELL_BUTTON_ROI = (60, 1190, 200, 70)
SELL_BUTTON_EXPECTED = ["键出售"]
SELL_BUTTON_POINT = (163, 1231)
DIALOG_WAIT = 2.0

# ---- 大翻动：固定手势 × 固定次数（用户实测标定）----
# 手势用用户框选的那条线 [356,385,4,436]：x=356~360，y=385~821。
# 所以取**最底端 y=821** 起手，向上拉 436 到 y=385，时长 0.2 秒。
# 用户实机观察：这个长度划 3 次一定能到底 ⇒ 不做「探测到底」的循环，直接划 3 次。
FLING_X = 358  # 框的横向中心 (356+4/2)
FLING_Y_TOP = 385  # 框的最上端
FLING_Y_BOTTOM = 821  # 框的最底端
FLING_DIST = 436  # 框的高度 = 每次拉的位移
FLING_MS = 200  # 0.2 秒
FLING_TIMES = 3  # 固定划 3 次
FLING_SETTLE = 1.8  # 每次甩完等惯性停稳

MAX_ROUNDS = 40  # 正/反向各自的最大轮数（防死循环）

LOG = "[bag]"
FRAME_TIMEOUT = 20.0  # 单次截图超时（秒），防 screencap 卡死
# 存诊断图（assets/debug/plugin/screens/）。正式跑关掉 —— 它在游戏自己的目录里写文件，
# 可能被反作弊盯上；要人工核对画面时用环境变量 MAAXZ_DUMP=1 打开。
DUMP_SCREENS = os.environ.get("MAAXZ_DUMP", "") == "1"

# ==================== 基础原语 ====================

def _frame(ctx: Context) -> np.ndarray:
    """主动取新帧（cached_image 不自动刷新，见 OPERATIONS 已知坑 #20）。

    ⚠️ screencap 偶发卡死（实测模拟器 + 反复重连后出现，卡住时进程 CPU 空转、
    日志停在上一句，任务永不结束）。所以放在线程里跑并设超时，卡住就抛错，
    让任务走 on_error 停下，而不是无声挂死。
    """
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

def _ocr(ctx: Context, frame: np.ndarray, expected: list[str], roi: tuple, threshold: float = 0.3):
    detail = ctx.run_recognition_direct("OCR", JOCR(expected=expected, roi=roi, threshold=threshold), frame)
    if detail is None or not detail.hit or detail.best_result is None:
        return False, None, None
    return True, getattr(detail.best_result, "text", None), tuple(detail.best_result.box)

def _click(ctx: Context, point: tuple[int, int]) -> bool:
    detail = ctx.run_action_direct("Click", JClick(target=point))
    return bool(detail is not None and detail.success)

def _list_templates() -> list[str]:
    root = Path(__file__).resolve().parent.parent / "assets" / "resource" / "image" / ITEMS_TEMPLATE_DIR
    return sorted(p.name for p in root.glob("*.png")) if root.is_dir() else []

def _touch_drag(ctx: Context, y_from: int, y_to: int, duration_ms: int, hold_ms: int | None = None,
                x: int | None = None) -> bool:
    """分步触摸拖动：按下 → 停顿 → 逐步移动 → **终点停住** → 抬起。坐标是**框坐标**。

    为什么逐行推进要在终点停住
    --------------------------
    手指抬起时的瞬时速度会被系统当成甩动（fling），列表带着惯性继续滑 —— 滑多远
    取决于速度，不可预测。在终点**停住**（手指不动停留一段）再抬起，速度归零，
    列表就停在手指停下的位置。

    例外：`hold_ms=0` 就是**故意要甩**（大翻动用），不给尾端停顿。

    方向说明见模块 docstring「滑动」一节。
    """
    hold = HOLD_MS if hold_ms is None else hold_ms
    end_hold = END_HOLD_MS if hold_ms is None else 0
    px = SWIPE_X if x is None else x
    try:
        ctrl = ctx.tasker.controller
        ctrl.post_touch_down(px, y_from, 0, 1).wait()
        if hold > 0:
            time.sleep(hold / 1000.0)
        steps = DRAG_STEPS
        for i in range(1, steps + 1):
            y = y_from + (y_to - y_from) * i // steps
            ctrl.post_touch_move(px, y, 0, 1).wait()
            time.sleep(max(0.0, duration_ms / 1000.0 / steps))
        if end_hold > 0:
            time.sleep(end_hold / 1000.0)  # ★ 终点停住：消掉甩动惯性
        ctrl.post_touch_up(0).wait()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG} !! touch drag 异常: {exc}", flush=True)
        return False

# ==================== 指纹 / 诊断 ====================

def _fingerprint(frame: np.ndarray, cx: int, cy: int) -> str:
    """格子图标内容的量化指纹（8 灰阶一档）。

    ⚠️ 必须量化：精确像素值会因抗锯齿/动画在帧间漂移，同一格得到不同指纹，
    去重直接失效（实测把格子数累加到 178）。
    """
    h, w = frame.shape[:2]
    half = FP_SIZE // 2
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(w, cx + half), min(h, cy + half)
    if x1 - x0 < FP_BLOCK or y1 - y0 < FP_BLOCK:
        return ""
    patch = frame[y0:y1, x0:x1]
    gh, gw = max(1, patch.shape[0] // FP_BLOCK), max(1, patch.shape[1] // FP_BLOCK)
    vals = []
    for gy in range(FP_BLOCK):
        for gx in range(FP_BLOCK):
            block = patch[gy * gh:(gy + 1) * gh, gx * gw:(gx + 1) * gw]
            if block.size == 0:
                return ""
            vals.append(str(int(block.mean()) >> 3))
    return ",".join(vals)

def _write_png(path: Path, rgb: np.ndarray) -> None:
    """纯 zlib 写 PNG（不依赖 PIL/cv2 —— 环境里都没装）。"""
    import struct
    import zlib

    h, w = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )

def _dump_frame(frame: np.ndarray, tag: str) -> None:
    if not DUMP_SCREENS:
        return
    try:
        out_dir = Path(__file__).resolve().parent.parent / "assets" / "debug" / "plugin" / "screens"
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_png(out_dir / f"{tag}.png", np.ascontiguousarray(frame))
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG} !! dump 失败: {exc}", flush=True)

# ==================== 扫描 ====================

class Tile:
    """一个格子 = 一次模板命中。位置直接用命中框中心，不做任何偏移推算。"""

    __slots__ = ("box", "center", "item", "idx", "score", "fp")

    def __init__(self, box, item, idx, score, fp):
        self.box = box
        self.center = (int(box[0] + box[2] / 2), int(box[1] + box[3] / 2))
        self.item, self.idx, self.score, self.fp = item, idx, score, fp

def find_tiles(ctx: Context, frame: np.ndarray, names: list[str], roi: tuple) -> list[Tile]:
    """在**指定选区**内逐模板匹配 → 每格一个 Tile（同格重复命中取最高分）。

    roi 由调用方给（翻动前用 ROI_BEFORE、翻动后用 ROI_AFTER）。
    """
    x0, y0, rw, rh = roi
    x1, y1 = x0 + rw, y0 + rh
    best: dict[tuple[int, int], Tile] = {}
    for idx, name in enumerate(names):
        detail = ctx.run_recognition_direct(
            "TemplateMatch",
            JTemplateMatch(template=[f"{ITEMS_TEMPLATE_DIR}/{name}"],
                           roi=roi, threshold=[ITEM_TEMPLATE_THRESHOLD]),
            frame,
        )
        if detail is None:
            continue
        # ⚠️⚠️ 关键坑：MaaFramework 的 all_results **不过滤阈值**，它会连 0.26 分的
        # 误命中一起返回（实测同一个模板在 threshold=0.85 下返回 16 条，其中 5 条只有
        # 0.51~0.52、还有别的模板 0.26~0.48）。阈值只体现在 detail.hit 上。
        # 所以这里必须自己按 score 再筛一遍，否则误命中会被当成正常格子，
        # 把物品计数灌高（实测 #0 绿叶被从 11 灌到 17）。
        for r in (detail.all_results or []):
            box = getattr(r, "box", None)
            if not box:
                continue
            score = float(getattr(r, "score", 0.0))
            if score < ITEM_TEMPLATE_THRESHOLD:
                continue
            x, y, w, h = (int(v) for v in box)
            cx, cy = x + w // 2, y + h // 2
            # ⚠️ 判据是「**整框**落在选区内」，不是「中心点在选区内」。
            # 实测踩坑：选区内某一行的格子上半截在选区内、下半截被弹窗底栏压住，
            # 中心点仍在选区内 ⇒ 混进来；而这类被遮住的格子模板分数只有 0.50~0.52
            # （同一物品完整时 0.99），会把某些物品的计数灌高（实测 #0 从 9 被灌到 17），
            # 进而把「该留 2 格」算错、勾错。
            if not (x0 <= x and x + w <= x1 and y0 <= y and y + h <= y1):
                continue
            key = (round(cx / SAME_TILE_DIST), round(cy / SAME_TILE_DIST))
            old = best.get(key)
            if old is None or score > old.score:
                best[key] = Tile((x, y, w, h), name, idx, score, "")
    tiles = list(best.values())
    for t in tiles:
        t.fp = _fingerprint(frame, *t.center)
    tiles.sort(key=lambda t: (t.center[1], t.center[0]))  # 从上到下、从左到右
    return tiles

def scan_step(ctx: Context, roi: tuple, out_frame: dict | None = None) -> list[Tile]:
    """按给定选区取一帧并识别。"""
    frame = _frame(ctx)
    if out_frame is not None:
        out_frame["f"] = frame
    return find_tiles(ctx, frame, _list_templates(), roi)

def group_rows(tiles: list[Tile]) -> list[list[Tile]]:
    """按 y 分组成行，行内按 x 排序 → 「第 1 行左→右数 1~4，第 2 行从头接第 5 个…」。"""
    rows: list[list[Tile]] = []
    for t in sorted(tiles, key=lambda t: t.center[1]):
        if rows and abs(t.center[1] - rows[-1][0].center[1]) <= ROW_TOL:
            rows[-1].append(t)
        else:
            rows.append([t])
    for r in rows:
        r.sort(key=lambda t: t.center[0])
    return rows

def ordered_tiles(tiles: list[Tile], reverse: bool = False) -> list[Tile]:
    """按「行优先、行内从左到右」拍平；reverse=True 时从最后一行、行内从右到左。"""
    rows = group_rows(tiles)
    if reverse:
        rows = rows[::-1]
        for r in rows:
            r.reverse()
    return [t for r in rows for t in r]

def plan_window(window: list[Tile]) -> tuple[list[Tile], dict[int, int]]:
    """按顺序数每种物品的出现次数 → (要勾的格, {物品序号: 出现次数})。

    规则：顺序里第 1、2 个保留，第 3 个起勾掉（KEEP_IN_WINDOW=2）。
    """
    seen: dict[int, int] = {}
    to_tap: list[Tile] = []
    for t in window:
        seen[t.idx] = seen.get(t.idx, 0) + 1
        if seen[t.idx] > KEEP_IN_WINDOW:
            to_tap.append(t)
    return to_tap, seen

def _fling(ctx: Context, to_bottom: bool) -> None:
    """按**规定手势**甩一次：436px 位移、0.2 秒、无尾端停顿（要惯性）。

    手势线用用户框选的 [356,385,4,436]：
      · 往列表**后面**（下）翻：从框最底端 y=821 起手，向上拉 436 到 y=385
      · 往列表**前面**（上）回：从 y=385 起手，向下拉 436 到 y=821
    """
    if to_bottom:
        _touch_drag(ctx, FLING_Y_TOP + FLING_DIST, FLING_Y_TOP, FLING_MS, hold_ms=0, x=FLING_X)
    else:
        _touch_drag(ctx, FLING_Y_TOP, FLING_Y_TOP + FLING_DIST, FLING_MS, hold_ms=0, x=FLING_X)

def fling_to_end(ctx: Context, to_bottom: bool, why: str = "") -> None:
    """按规定次数（3 次）甩到列表一端。不做「探测是否到底」的循环 —— 行为可预测。

    用户实机标定：这个长度划 3 次一定能到底。
    """
    tag = "翻到最后" if to_bottom else "回到最前"
    for i in range(FLING_TIMES):
        _fling(ctx, to_bottom)
        time.sleep(FLING_SETTLE)
        print(f"{LOG}   {tag} {i + 1}/{FLING_TIMES}{('（' + why + '）') if why else ''}", flush=True)

def _ocr_exact(ctx: Context, frame: np.ndarray, roi: tuple) -> str:
    """把 ROI 里的文字原样读出来（不设 expected），用于读「出售数量：N」这种数值。"""
    detail = ctx.run_recognition_direct("OCR", JOCR(expected=[], roi=roi, threshold=0.2), frame)
    if detail is None:
        return ""
    parts = []
    for r in (detail.all_results or []):
        t = (getattr(r, "text", "") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)

def read_sell_count(ctx: Context, frame: np.ndarray) -> int | None:
    """读弹窗里「出售数量：N」的 N。读不到返回 None。"""
    text = _ocr_exact(ctx, frame, SELL_COUNT_ROI)
    m = re.search(r"(\d+)", text.replace("，", "").replace(",", ""))
    if not m:
        # 有些帧会把「0」单独识别，或整行读成「出售数量:」没带数字
        return 0 if "出售数量" in text else None
    return int(m.group(1))

def confirm_popup(ctx: Context, frame: np.ndarray) -> bool:
    """点出售后弹的确认框 → 点「确定」。"""
    ok, text, box = _ocr(ctx, frame, CONFIRM_EXPECTED, CONFIRM_ROI)
    if ok and box is not None:
        point = (int(box[0] + box[2] // 2), int(box[1] + box[3] // 2))
        print(f"{LOG}   确认框：OCR 命中「{text}」box={box} → 点 {point}", flush=True)
    else:
        point = CONFIRM_POINT
        print(f"{LOG}   确认框：未认出文字 → 用兜底坐标 {point}", flush=True)
    if not _click(ctx, point):
        print(f"{LOG}   !! 点确认失败", flush=True)
        return False
    time.sleep(CONFIRM_WAIT)
    return True

def click_sell(ctx: Context) -> bool:
    """点「出售」→ 点确认框的「确定」→ 校验「出售数量」归 0。

    ⚠️ 实测：点出售后**弹窗不会关闭**，而是再冒一个确认框，必须点「确定」才真正成交。
    所以这里两步都要做，而且要用「出售数量归 0」来**校验真的成交了** ——
    之前就是因为出售按钮的 ROI 偏下、点到被弹窗盖住的储物袋按钮，静默失败了好几轮。
    """
    frame = _frame(ctx)
    before = read_sell_count(ctx, frame)
    ok, text, box = _ocr(ctx, frame, SELL_CLICK_EXPECTED, SELL_CLICK_ROI)
    if ok and box is not None:
        point = (int(box[0] + box[2] // 2), int(box[1] + box[3] // 2))
        print(f"{LOG}   OCR 命中「{text}」box={box} → 点出售 {point}（点前数量={before}）", flush=True)
    else:
        point = SELL_CLICK_POINT
        print(f"{LOG}   未认出出售按钮 → 用兜底坐标 {point}（点前数量={before}）", flush=True)
    if not _click(ctx, point):
        print(f"{LOG} !! 点出售失败", flush=True)
        return False
    time.sleep(SELL_WAIT)

    # 确认框
    if not confirm_popup(ctx, _frame(ctx)):
        return False

    # 校验：出售数量应归 0（选中被消费掉了）
    frame = _frame(ctx)
    after = read_sell_count(ctx, frame)
    print(f"{LOG}   成交校验：出售数量 {before} → {after}", flush=True)
    if after is None:
        print(f"{LOG}   !! 读不到「出售数量」，无法确认成交", flush=True)
        return False
    if after != 0:
        print(f"{LOG}   !! 出售后数量仍为 {after}，说明没成交（按钮点错/确认没点上）", flush=True)
        return False
    return True

# ==================== 主流程 ====================

def pick_target(ordered: list[Tile]) -> tuple[int | None, dict[int, int]]:
    """按顺序数每种出现次数 → (第一个超过 2 格的种类, 计数表)。

    为什么是「第一个」而不是「全部都勾」：一次只卖一个种类，卖完列表坍缩，剩下的
    种类会被顶进选区。这样每轮的动作最小、最容易核对，也天然对应
    「第一种物品 → 第二种物品 → …」的顺序。
    """
    counted: dict[int, int] = {}
    for t in ordered:
        counted[t.idx] = counted.get(t.idx, 0) + 1
    for t in ordered:
        if counted[t.idx] > KEEP_IN_WINDOW:
            return t.idx, counted
    return None, counted

def cycle_once(ctx: Context, roi: tuple, reverse: bool, dry: bool) -> tuple[bool, dict]:
    """一轮：按选区认格子 → 找第一个超过 2 格的种类 → 勾掉它第 3 格起 → 点出售。

    roi: 本轮的识别选区（翻动前 ROI_BEFORE / 翻动后 ROI_AFTER）
    reverse: True = 从最后一行、行内从右到左数（翻到大翻动之后用）
    返回 (本轮是否卖掉了一票, 统计)
    """
    holder: dict = {}
    frame0 = None
    tiles = scan_step(ctx, roi, out_frame=holder)
    frame0 = holder.get("f")
    if frame0 is not None:
        _dump_frame(frame0, "cycle-after" if reverse else "cycle-before")

    # 残留保护：上一轮如果没卖成功，弹窗里可能还留着选中（出售数量 > 0）。
    # 这时再往上叠加勾选会把不该卖的也卖掉，所以直接失败退出，交给人看。
    if not dry and frame0 is not None:
        left = read_sell_count(ctx, frame0)
        if left:
            print(f"{LOG} !! 弹窗里还残留选中（出售数量={left}），拒绝继续叠加勾选", flush=True)
            return False, {"window": 0, "tap": 0, "kind": None, "abort": "leftover"}

    ordered = ordered_tiles(tiles, reverse=reverse)
    if not ordered:
        print(f"{LOG} !! 选区内一格都没认出来（命中框 {len(tiles)} 个）", flush=True)
        return False, {"window": 0, "tap": 0, "kind": None, "abort": "选区无识别结果"}

    target, counted = pick_target(ordered)
    layout = ", ".join(f"#{i}×{n}" for i, n in sorted(counted.items()))
    print(f"{LOG}   选区 {len(ordered)} 格；构成 {layout}", flush=True)

    # 卡死检测：把「选区内容指纹 + 每个种类的格数」当成本轮的状态签名。
    # 若连续两轮签名相同，说明上一轮的勾选/出售**没有改变列表**（比如物品不可卖、
    # 或翻动没生效导致停在原地），再转下去只会把同一批格子反复点、反复"卖"。
    # 实测就是靠这个发现阶段 2 在地面上空转了 10 轮。
    sig = "|".join(sorted(t.fp for t in ordered if t.fp)) + "#" + \
          ",".join(f"{i}:{n}" for i, n in sorted(counted.items()))
    if sig == _LAST_SIG[0]:
        print(f"{LOG} !! 选区内容与上一轮完全相同（列表没变）→ 停止，避免死循环", flush=True)
        return False, {"window": len(ordered), "tap": 0, "kind": None, "abort": "列表无变化（可能翻动未生效/物品不可卖）"}
    _LAST_SIG[0] = sig

    if target is None:
        print(f"{LOG}   每个种类都 ≤ {KEEP_IN_WINDOW} 格 → 选区内已整理干净", flush=True)
        return False, {"window": len(ordered), "tap": 0, "kind": None}

    same = [t for t in ordered if t.idx == target]
    keep, drop = same[:KEEP_IN_WINDOW], same[KEEP_IN_WINDOW:]
    print(f"{LOG}   本轮处理 #{target}：共 {len(same)} 格 → 保留 {len(keep)} 格"
          f"{[t.center for t in keep]}，勾选 {len(drop)} 格{[t.center for t in drop]}", flush=True)

    if dry:
        print(f"{LOG}   [dry] 只报不点", flush=True)
        return True, {"window": len(ordered), "tap": len(drop), "kind": target}

    picked = 0
    for t in drop:
        if _click(ctx, t.center):
            picked += 1
            time.sleep(TAP_WAIT)
        else:
            print(f"{LOG}   !! 点格子失败 {t.center}", flush=True)
    print(f"{LOG}   已勾选 {picked}/{len(drop)} 格", flush=True)
    if picked == 0:
        return False, {"window": len(ordered), "tap": 0, "kind": target, "abort": "勾选一个都没点上"}

    if not click_sell(ctx):
        return False, {"window": len(ordered), "tap": picked, "kind": target, "abort": "出售/确认失败"}
    return True, {"window": len(ordered), "tap": picked, "kind": target}

def run_organize_bag(ctx: Context, dry: bool = False, stop_before_fling: bool = False) -> tuple[bool, str]:
    """选区式整理（自动循环）。

    每一轮 = **处理一个种类并卖掉**：认选区 → 找第一个超过 2 格的种类 → 勾掉它第 3 格起
    → 点出售。卖完列表坍缩，后面的种类被顶进选区，于是下一轮自然处理「下一种物品」。

    阶段 1（翻动前，选区 ROI_BEFORE）：反复上面这一轮，直到选区内**每个种类都 ≤2 格**。
    阶段 2（翻动后，选区 ROI_AFTER）：大翻动到底，从最后一行往上、行内从右到左再来一遍。

    stop_before_fling=True 时，阶段 1 收敛后就停下（不进入大翻动），用于分步验证。
    """
    names = _list_templates()
    if not names:
        return False, f"{ITEMS_TEMPLATE_DIR}/ 下没有模板图"
    print(f"{LOG} 模板 {len(names)} 种；每种留 {KEEP_IN_WINDOW} 格；"
          f"翻动前选区 {ROI_BEFORE}，翻动后选区 {ROI_AFTER}", flush=True)

    # 前置的「打开储物袋 / 打开一键出售」已拆成独立 pipeline 节点
    # （见 agent/pack.py + pipeline/储物袋.json），本 action 只负责弹窗打开之后的循环。
    frame = _frame(ctx)
    ok, text, _box = _ocr(ctx, frame, DIALOG_TITLE_EXPECTED, DIALOG_TITLE_ROI)
    if not ok:
        return False, "一键出售弹窗没打开（请先跑「打开储物袋 + 一键出售」节点）"
    print(f"{LOG} 出售弹窗已就绪（标题「{text}」）", flush=True)

    total_taps = 0
    total_cycles = 0
    sold_log: list[str] = []

    # 起点：pipeline 的「打开储物袋 + 打开整理 + 打开一键出售」节点已保证弹窗开着且列表已排好。
    # 只点一次「材料」页签，之后每轮都不用再点（弹窗不关，页签保持）。
    _click(ctx, TAB_MATERIAL_POINT)
    time.sleep(1.0)
    _LAST_SIG[0] = ""  # 清掉跨任务残留的卡死签名

    # 起点校验：弹窗里不该有残留选中。有的话直接停 —— 因为按「数量总和」反推选中格数可能在
    # 满格(999)时多算，猜错会多卖。明确交给人处理，比猜安全。
    frame = _frame(ctx)
    left = read_sell_count(ctx, frame)
    if left:
        return False, (f"弹窗里已存在选中（出售数量={left}），拒绝在未知选择上继续。"
                       f"请手动清空选择后再跑")

    # ---- 阶段 1：翻动前，反复处理选区里的「下一个还有多余的物品」----
    # ⚠️ 实测：点「出售」+「确定」之后**弹窗不关**，列表就地坍缩（同类还会自动聚在一起）。
    # 所以每轮不需要关弹窗/重新整理/重开弹窗 —— 直接再认一次选区即可。
    for rnd in range(MAX_ROUNDS):
        print(f"{LOG} ===== 阶段1 第 {rnd + 1} 轮 =====", flush=True)
        sold, stat = cycle_once(ctx, ROI_BEFORE, reverse=False, dry=dry)
        total_cycles += 1
        total_taps += stat["tap"]
        if stat.get("kind") is not None and stat["tap"]:
            sold_log.append(f"#{stat['kind']}×{stat['tap']}")
        if stat.get("abort"):
            # ⚠️ 必须和「没有可卖」区分开：出售失败也返回 sold=False，
            # 若不拦住，一次点击失败就会被误判成「选区内已整理干净」而提前收工。
            return False, f"第 {rnd + 1} 轮异常中止（{stat['abort']}），未完成"
        if not sold:
            print(f"{LOG} ★ 翻动前选区里每个种类都 ≤ {KEEP_IN_WINDOW} 格 —— 阶段1 收敛", flush=True)
            break
        if dry:
            # dry 不真卖 ⇒ 内容不会因坍缩而前进，再转只会反复"发现"同一格。报一轮就收工。
            print(f"{LOG} [dry] 本轮有可卖内容；未真卖则选区不会前进，dry 到此收工", flush=True)
            report = (f"[dry] 翻动前第 1 轮发现 {stat['tap']} 格需勾选并出售；"
                      f"选区 {stat['window']} 格，每种留 {KEEP_IN_WINDOW} 格")
            print(f"{LOG} {report}", flush=True)
            return True, report
        # 弹窗不关，列表已就地坍缩 —— 直接进入下一轮，不用关弹窗/整理/重开
    else:
        return False, f"阶段1 达到上限 {MAX_ROUNDS} 轮仍未收敛"

    if stop_before_fling:
        report = (f"阶段1 完成：{total_cycles} 轮，共出售 {total_taps} 格；"
                  f"次序 [{', '.join(sold_log)}]（每种留 {KEEP_IN_WINDOW} 格）；"
                  f"已按要求停在大翻动之前")
        print(f"{LOG} {report}", flush=True)
        return True, report

    # ---- 阶段 2：大翻动到底，从后往前 ----
    print(f"{LOG} ===== 阶段2 大翻动到底，从后往前 =====", flush=True)
    _LAST_SIG[0] = ""  # 翻动会改变列表位置，先清签名
    fling_to_end(ctx, to_bottom=True, why="翻到最后")
    for rnd in range(MAX_ROUNDS):
        print(f"{LOG}   翻动后 第 {rnd + 1} 轮", flush=True)
        sold, stat = cycle_once(ctx, ROI_AFTER, reverse=True, dry=dry)
        total_cycles += 1
        total_taps += stat["tap"]
        if stat.get("kind") is not None and stat["tap"]:
            sold_log.append(f"#{stat['kind']}×{stat['tap']}(尾)")
        if not sold:
            print(f"{LOG}   尾部每种都 ≤ {KEEP_IN_WINDOW} 格 → 结束", flush=True)
            break
        if dry:
            print(f"{LOG}   [dry] 尾部有可卖内容；dry 收工", flush=True)
            break
        # 弹窗不关，就地坍缩。为了保证仍停在列表最后，再甩到最底
        _LAST_SIG[0] = ""  # 重新翻动前清签名
        fling_to_end(ctx, to_bottom=True, why="重新翻到最后")
    else:
        print(f"{LOG} !! 阶段2 达到上限 {MAX_ROUNDS} 轮", flush=True)

    report = (f"{'[dry] ' if dry else ''}共 {total_cycles} 轮，共出售 {total_taps} 格"
              f"（每种留 {KEEP_IN_WINDOW} 格）；次序 [{', '.join(sold_log)}]")
    print(f"{LOG} {report}", flush=True)
    return True, report

@AgentServer.custom_action("maa_agent_organize_bag")
class OrganizeBagAction(CustomAction):
    """整理背包（选区式·自动循环）：每轮处理一个种类并出售，反复到选区内每种都 ≤2 格，
    再大翻动到底从后往前做一遍。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        ok, detail = run_organize_bag(context)
        print(f"{LOG} {'完成：' if ok else '失败：'}{detail}", flush=True)
        return ok


@AgentServer.custom_action("maa_agent_bag_preview")
class BagPreviewAction(CustomAction):
    """dry-run：只识别与报数，绝不点格子、绝不点出售。用来验证选区和识别是否正常。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        ok, detail = run_organize_bag(context, dry=True)
        print(f"{LOG} {'预览完成：' if ok else '预览失败：'}{detail}", flush=True)
        return ok
