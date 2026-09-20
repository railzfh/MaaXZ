"""端到端夹具：真跑含 agent 自定义动作的 pipeline 任务。

maasf3_run 是 headless runner，不会拉起 agent 进程（Custom 动作会报 Action is null），
所以这里按 MaaFramework 的方式自己组装 agent 连接。

用法：python _e2e_bag.py <entry节点名>
"""
import json
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent
from maa.agent_client import AgentClient
from maa.controller import AdbController
from maa.resource import Resource
from maa.tasker import Tasker

ADB = r"D:\Program Files\Netease\MuMu\nx_main\adb.exe"
DEV = "127.0.0.1:16416"
ENTRY = sys.argv[1] if len(sys.argv) > 1 else "整理背包"
# 可选第二参数：直接从**某个 pipeline 节点**起跑（不经过 interface 任务链）。
# 用来单独验证一步（例如「某界面的返回到底能不能退」），排查问题时非常省时间。
ENTRY_NODE = sys.argv[2] if len(sys.argv) > 2 else None


def _override_from_interface(entry: str) -> tuple[dict, str]:
    """从 interface.json 取该任务的 pipeline_override（MaaPiCli/通用 UI 也是这么做的）。

    既接受**任务名**也接受 **entry 节点名**；同名 entry 有多个任务时，先按任务名精确匹配。
    interface.json 是 JSONC（行尾有 // 注释），必须用 jsonc 解析，不能手剥注释。
    """
    import jsonc  # 来自 json-with-comments（tools/requirements.txt）

    data = jsonc.loads((PROJ / "assets" / "interface.json").read_text(encoding="utf-8"))
    tasks = data.get("task", [])
    for t in tasks:
        if t.get("name") == entry:
            return (t.get("pipeline_override") or {}), t.get("entry")
    for t in tasks:
        if t.get("entry") == entry:
            return (t.get("pipeline_override") or {}), t.get("entry")
    return {}, None


def main() -> int:
    entry_name = ENTRY
    resource = Resource()
    resource.post_bundle(PROJ / "assets" / "resource").wait()
    override, entry = _override_from_interface(ENTRY)
    if ENTRY_NODE:
        # 直接以某个 pipeline 节点起跑，忽略 interface 的 entry/override
        entry_name = ENTRY_NODE
        override = {}
        print(f"★ 直接从节点起跑: {entry_name}", flush=True)
    elif entry:
        entry_name = entry
    print(f"按钮/节点 {ENTRY!r} → entry={entry_name!r} 存在:", bool(resource.get_node_data(entry_name)), flush=True)
    if override:
        print(f"pipeline_override: {json.dumps(override, ensure_ascii=False)}", flush=True)

    client = AgentClient.create_tcp(0)
    port = client.identifier
    client.bind(resource)
    # agent 输出**实时写文件**：隔着管道读要等子进程退出，卡死时就什么都看不到。
    agent_log = open(PROJ / "_agent_live.log", "w", encoding="utf-8")
    agent = subprocess.Popen(
        [sys.executable, "-u", str(PROJ / "agent" / "main.py"), str(port)],
        cwd=str(PROJ / "assets"),
        stdout=agent_log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    connected = client.connect()
    print(f"agent 连接={connected}；已注册动作={client.custom_action_list if connected else []}", flush=True)
    if not connected:
        agent.kill()
        return 2

    controller = AdbController(adb_path=ADB, address=DEV)
    controller.post_connection().wait()
    tasker = Tasker()
    tasker.bind(resource, controller)

    print("开始跑任务 ...", flush=True)
    t0 = time.time()
    detail = tasker.post_task(entry_name, override).wait().get()
    print(f"任务结果: succeeded={detail.status.succeeded} 用时 {time.time() - t0:.1f}s", flush=True)
    # 把访问过的节点 ID 解析成名字。
    # ⚠️ 走过弯路：Resource.get_node_data 只接受**名字**且不返回 id；
    #    真正能用的是 **Tasker.get_node_detail(node_id) → NodeDetail{node_id, name}**。
    id2name: dict[int, str] = {}
    for nid in (detail.node_id_list or []):
        try:
            nd = tasker.get_node_detail(int(nid))
            if nd is not None:
                id2name[int(nid)] = nd.name
        except Exception:  # noqa: BLE001
            pass
    seq = [id2name.get(int(nid), f"?{nid}") for nid in (detail.node_id_list or [])]
    print(f"访问节点 {len(seq)} 个:", flush=True)
    for i, n in enumerate(seq, 1):
        print(f"   {i:>2}. {n}", flush=True)

    # ⚠️ 只看 succeeded 不可靠：MaaFramework 里走 on_error 到失败终点、或走到没有 next 的
    #    节点，都算 succeeded。所以额外判断「有没有走到名字里含'完成'的节点」。
    finished = [n for n in seq if n.endswith("完成")]
    if finished:
        print(f"✅ 已到达成功终点: {finished[-1]}", flush=True)
    else:
        print(f"⚠️ 没有到达任何「完成」终点 —— succeeded={detail.status.succeeded} 但流程可能提前结束"
              f"（最后一个是 {seq[-1] if seq else '?'}）", flush=True)

    client.disconnect()
    try:
        agent.wait(timeout=15)
    except subprocess.TimeoutExpired:
        agent.kill()
    agent_log.close()
    print("--- agent 输出见 _agent_live.log ---", flush=True)
    return 0 if detail.status.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
