"""AgentServer 入口：通用 UI 会拉起本进程，并把 socket_id 作为命令行参数传进来。

启动方式由 interface.json 的 agent 字段决定（CWD 为 interface.json 所在目录 assets/）：
    python ../agent/main.py <socket_id>

注册方式：`import` 即完成注册 —— my_action / my_reco / guard 里的
`@AgentServer.custom_action(...)` / `custom_recognition(...)` 装饰器在导入时执行。
"""

import sys

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

import guard  # noqa: F401  maa_agent_guard_ensure_main（通用前置守卫）
import my_action  # noqa: F401
import my_reco  # noqa: F401


def main():
    # AgentServer 进程里唯一该调的全局设置是日志目录（Toolkit.init_option 在
    # AgentServer 模式下已废弃，只会转调 set_log_dir）。
    Tasker.set_log_dir("./debug")

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by AgentIdentifier.")
        sys.exit(1)

    socket_id = sys.argv[-1]

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
