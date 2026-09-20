"""通用流程小工具（Custom 动作）：把「依赖识别结果做分支」的活从 pipeline 挪到这里。

为什么要这个模块
----------------
MaaFramework 里**「识别不命中」不能可靠地当业务分支用** —— 实测：
- 一个识别必定失败的测试节点，确实会走它的 `on_error`；
- 但业务节点（例如「在 ROI 里找 2000，找不到就跳过」）识别不命中时，
  **任务链会直接终止**（任务报 succeeded，但后面所有步骤都没跑）。
两者行为不一致，我们没定位到差异根因。

所以凡是「有则做、无则跳过」这类**由识别结果决定要不要动手**的步骤，
统一用本模块的 `maa_agent_click_if_found`：识别 + 点击都在一次动作里完成，
**无论有没有命中都返回成功**，pipeline 那条线永远不会因为"没找到"而断。

用法（pipeline 里）：
    {
        "action": {
            "type": "Custom",
            "param": {
                "custom_action": "maa_agent_click_if_found",
                "custom_action_param": {
                    "expected": ["2000"],     // 要找的文字
                    "roi": [404, 848, 148, 52], // 在哪找（帧坐标）
                    "threshold": 0.6,
                    "target": [497, 874]      // 可选：命中后点哪（缺省点命中框中心）
                }
            }
        },
        "next": ["下一步"]                   // 无论命中与否都会走到这里
    }
"""

from __future__ import annotations

import time

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JClick, JOCR

LOG = "[flow]"
OCR_THRESHOLD = 0.6
CLICK_WAIT = 1.2


def _frame(ctx: Context) -> np.ndarray:
    """主动取新帧（cached_image 不会自动刷新）。"""
    return ctx.tasker.controller.post_screencap().wait().get()


def _param(argv, default=None) -> dict:
    """取 custom_action_param，**兼容它是 JSON 字符串的情况**。

    ⚠️ 实测踩坑：pipeline 里写的 custom_action_param 传进来可能是 **str**（JSON 文本），
    而不是 dict。直接 `param.get(...)` 会抛 `'str' object has no attribute 'get'`，
    而 MaaFramework 会把 Custom 动作里的异常**吞掉并把该节点当成功**，
    于是任务"成功"却什么都没做 —— 又一个静默失败。所以这里统一解析。
    """
    raw = getattr(argv, "custom_action_param", None)
    if raw is None:
        return dict(default or {})
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return dict(default or {})
        try:
            import json

            v = json.loads(raw)
            return v if isinstance(v, dict) else dict(default or {})
        except Exception:  # noqa: BLE001
            print(f"{LOG} !! custom_action_param 不是合法 JSON: {raw[:120]!r}", flush=True)
            return dict(default or {})
    return dict(default or {})


@AgentServer.custom_action("maa_agent_run_if_not_found")
class RunIfNotFoundAction(CustomAction):
    """若**没有**出现 skip_if_found 里的文字，就顺序执行 steps 里的点击；出现了就整段跳过。

    典型用途：同一个界面有多个状态，只在"可以做这件事"的状态下动手。例如神游弹窗：
      · 未开始（有次数输入框 + [开始神游] 按钮）→ 要「点最大 → 点开始神游」
      · **进行中**（显示「神游常羊山 2/70」进度条 + [神魂归窍]，没有开始按钮）
        → 这两个按钮都不存在，硬点会失败并**断掉任务链**，所以必须跳过

    param:
      skip_if_found: ["神魂归窍","神游常羊山"]   # 出现任一 → 跳过 steps
      skip_roi:      [0,960,720,140]            # 在哪找（缺省整屏）
      steps: [ {"click":[540,976],"wait":0.8},  # 按顺序点
               {"click":[359,1039],"wait":2.5} ]
    无论跳过还是执行完，都返回 True（不断链）。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        skip_words = list(param.get("skip_if_found") or [])
        skip_roi = tuple(param.get("skip_roi") or (0, 0, 720, 1280))
        steps = list(param.get("steps") or [])

        if skip_words:
            frame = _frame(context)
            d = context.run_recognition_direct(
                "OCR", JOCR(expected=skip_words, roi=skip_roi, threshold=OCR_THRESHOLD), frame)
            hit = bool(d is not None and d.hit and getattr(d, "best_result", None) is not None)
            if hit:
                got = getattr(d.best_result, "text", "")
                print(f"{LOG} run_if_not_found: 命中「{got}」→ 整段跳过（{len(steps)} 步）",
                      flush=True)
                return True

        for i, st in enumerate(steps, 1):
            point = st.get("click")
            if point and len(point) >= 2:
                p = (int(point[0]), int(point[1]))
                print(f"{LOG} run_if_not_found: 第 {i}/{len(steps)} 步 → 点 {p}", flush=True)
                context.run_action_direct("Click", JClick(target=p))
            time.sleep(float(st.get("wait", 1.0)))
        print(f"{LOG} run_if_not_found: {len(steps)} 步已执行完", flush=True)
        return True


@AgentServer.custom_action("maa_agent_click_if_found")
class ClickIfFoundAction(CustomAction):
    """在指定 ROI 里找文字：找到就点它，找不到就什么都不做；**两种情况都返回 True**。

    这样 pipeline 里不再需要「识别失败 → on_error 分支」这种不可靠的写法。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        expected = list(param.get("expected") or [])
        roi = tuple(param.get("roi") or (0, 0, 0, 0))
        threshold = float(param.get("threshold", OCR_THRESHOLD))
        target = param.get("target")

        if not expected:
            print(f"{LOG} click_if_found: 没给 expected，直接跳过（返回成功以免断链）", flush=True)
            return True

        frame = _frame(context)
        detail = context.run_recognition_direct(
            "OCR", JOCR(expected=expected, roi=roi, threshold=threshold), frame)
        n = len(list(getattr(detail, "all_results", None) or [])) if detail is not None else -1
        print(f"{LOG} click_if_found: 找 {expected} in {roi} → hit={getattr(detail,'hit',None)} "
              f"候选={n}", flush=True)

        best = getattr(detail, "best_result", None) if detail is not None else None
        # ⚠️ 注意：不能只看 detail.hit —— 实测 all_results 会返回低于阈值的候选，
        #    而 hit 只反映"最佳结果是否达阈值"。这里直接核对 best_result 的文字与分数。
        if detail is not None and detail.hit and best is not None:
            text = getattr(best, "text", "")
            score = float(getattr(best, "score", 0.0))
            if score >= threshold and any(e in text or text in e for e in expected):
                if target is not None and len(target) >= 2:
                    point = (int(target[0]), int(target[1]))
                    how = "按给定 target"
                else:
                    box = getattr(best, "box", None)
                    if not box:
                        print(f"{LOG} click_if_found: 命中「{text}」但没有框，跳过点击", flush=True)
                        return True
                    point = (int(box[0] + box[2] // 2), int(box[1] + box[3] // 2))
                    how = "按命中框中心"
                print(f"{LOG} click_if_found: 命中「{text}」(score={score:.3f}) → 点 {point}（{how}）",
                      flush=True)
                context.run_action_direct("Click", JClick(target=point))
                time.sleep(CLICK_WAIT)
                return True

        got = getattr(best, "text", None) if best is not None else None
        print(f"{LOG} click_if_found: 在 {roi} 里没找到 {expected}（最佳={got!r}）→ 跳过，不点击",
              flush=True)
        return True
