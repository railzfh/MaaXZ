# MaaXZ AI 操作手册

本文件是把仓库里的模板通用文档（`how_to_develop.md` / `agent.md` / `custom_configure.md` / `faq.md`
+ `deps/tools/*.schema.json` 六个协议 schema + `tools/*.py`）**归纳成一份可直接照做的操作手册**。

**读取约定：只在做 MaaXZ 相关工作时读本文件。** 它刻意不叫 `AGENTS.md`——那类文件会被 DSH 自动注入每一轮对话，
与本项目无关的会话不该被它占用上下文。日常开发优先看下面的「一分钟上手」。

---

## 0. 一分钟上手

| 项 | 值 |
| --- | --- |
| 项目 | **MaaXZ** —《太古仙尊》自动化助手 |
| 目标包名 | `com.xyjx.hardtime.aligames` |
| 控制器 | 仅 `Adb`（`安卓端`），已移除模板的 Win32 桌面端 |
| 资源名 | `官服` → `./resource` |
| 坐标基准 | **1280×720**（pipeline 里所有 `roi` / `target` / 坐标都按它写） |
| MaaFramework | CI 钉 `MAAFW_VERSION: v5.13.0`；本地 pip 绑定应为 **MaaFw 5.13.0** |
| 通用 UI | `MFAA_VERSION: v2.16.1`（MFAAvalonia，CI 打包时拼装） |
| 设备 | MuMu 模拟器；adb 路径见下「工具链」 |

坐标系换算铁律：MuMu 的 `exec-out screencap` 返回**原生 2560×1440**，
量坐标前必须归一到短边 720；写成 pipeline 时也必须是 720 基准值。

---

## 1. 热区铁律（违反必炸）

1. **`doc` 字段必填**：每个 pipeline 节点都要写 `doc` 说明它是干什么的，`maa-tools check` / lint 会检查。
2. **节点名跨文件全局共享**：改名/删名必须先查引用（`maasf3_pipeline action=refs`），否则留下悬挂 `next`。
3. **`DirectHit` 不许放进 `next`**：无条件命中会导致 next 链在此死循环。
4. **`interface.json` 的 `task[].entry` 必须是真实存在的节点**；spell 错一个字母就是「UI 里点了没反应」。
5. **JSON 是 JSONC**：`interface.json` / pipeline 允许 `//` 注释；但**不能**有尾随逗号以外的语法错。
6. **28MB 的 OCR 模型不入库**：`assets/resource/model/ocr/` 被 `.gitignore` 忽略，别 `git add -f`。
7. **改 pipeline 后必须重跑校验**：`maasf3_check`（语义引擎，约 50ms）。改 `interface.json` 同样要跑。
8. **`maasf3_run` 会真的操作设备**（点击/滑动）。试跑一律用 `dryRun`（动作全 DoNothing）。
9. **pipeline 一律写 V2**（`recognition` / `action` = `{ type, param }`，参数进 `param`）。V1 平铺能跑但不要写 —— 见 [pipeline-v2.md](./pipeline-v2.md)。
10. **显式点击坐标的两种场合**：识别只做校验但点击位置固定时、以及 `DirectHit` 配 `Click` 时（DirectHit 识别框是整屏，`target: true` = 点屏幕正中）。

---

## 2. 目录职责（谁放什么）

```text
MaaXZ/
├── assets/
│   ├── interface.json          ← 唯一入口：控制器/资源/任务/agent 启动方式
│   └── resource/               ← 打包后整个成为发布包的 resource/
│       ├── pipeline/*.json     ← ★ 脚本逻辑（递归读取所有 json）
│       ├── image/*.png         ← 模板图（720p 基准裁剪）
│       └── model/ocr/          ← det.onnx / keys.txt / rec.onnx（gitignore）
├── agent/                      ← ★ Python 自定义识别/动作（AgentServer）
├── deps/tools/*.schema.json    ← 协议 schema，由 sync_schema_files.yml 从 MaaFramework main 同步
├── tools/                      ← CI 打包与校验脚本
├── docs/zh_cn/develop/         ← 开发文档（本手册在此）
└── .github/workflows/          ← check / install / mirrorchyan / sync_schema
```

---

## 3. 三份契约

### 3.1 Pipeline 节点

> **写法一律用 V2**：`recognition` / `action` 各是 `{ type, param }` 对象，类型专属参数全放 `param`。
> 完整规范见 [pipeline-v2.md](./pipeline-v2.md)（本仓库内置）。V1 平铺能跑但**不要写**，工具会报 `form: "v1"` 警告。

```jsonc
{
    "节点名": {
        "doc": "干什么用的（必填，lint 会查）",
        "recognition": {
            "type": "OCR",
            "param": { "expected": ["开始"], "roi": [500, 600, 200, 60], "threshold": 0.3 }
        },
        "action": {
            "type": "Click",
            "param": { "target": true }        // true = 点刚识别到的位置
        },
        "next": ["下一个节点"],                 // 候选列表：从上到下第一个命中的赢
        "on_error": ["错误处理节点"],           // 识别超时/动作失败的去处
        "post_delay": 500                       // 动作后等待 ms
    }
}
```

**三条最容易踩的**（详见 pipeline-v2.md §4）：

1. `target: true` 点的是**识别框中心**。所以「识别用来校验、点击位置固定」时要显式写 `target: [x, y]`。
2. **`DirectHit` 配 `target: true` 等于点屏幕正中**（DirectHit 的识别框是整屏）。纯动作节点必须显式写坐标。
3. 类型没有参数时 `param` 可省；`recognition` / `action` 整个省略时分别按 `DirectHit` / `DoNothing`。

**识别类型（10 种）** —— 下表的「关键参数」都写在 `recognition.param` 里：

| type | 用途 | 关键参数（放 param 内） |
| --- | --- | --- |
| `DirectHit` | 无条件命中，做「纯动作」节点 | 仅 `roi` |
| `TemplateMatch` | 找按钮/图标（最常用） | `template`(必需)、`threshold`(默认 0.7)、`green_mask` |
| `OCR` | 找文字（本项目主力） | `expected`(正则)、`threshold`(默认 0.3)、`only_rec` |
| `FeatureMatch` | 模板会缩放/旋转 | `template`、`count`(默认 4)、`detector`(SIFT) |
| `ColorMatch` | 找纯色块/血条 | `lower` / `upper`(必需)、`count` |
| `NeuralNetworkClassify` | 分类「是/不是」 | `model`(必需)、`labels`、`expected` |
| `NeuralNetworkDetect` | 找目标框 | `model`(必需)、`labels` |
| `And` / `Or` | 组合子识别 | `all_of` / `any_of`（写节点名或内联对象） |
| `Custom` | 交给 Python | `custom_recognition`(必需)、`custom_recognition_param` |

**动作类型（常用）** —— 下表的「关键参数」都写在 `action.param` 里：

| type | 用途 | 关键参数（放 param 内） |
| --- | --- | --- |
| `DoNothing` | 只识别不动手 | — |
| `Click` | 点击 | `target`：`true`=识别框中心 / 节点名 / `[x,y]` / `[x,y,w,h]` |
| `LongPress` | 长按 | `duration`(默认 1000) |
| `Swipe` | 滑动 | `begin` / `end` / `duration`(默认 200) |
| `StartApp` / `StopApp` | 启停应用 | `package`(必需) |
| `Custom` | 交给 Python | `custom_action`(必需)、`custom_action_param` |
| `StopTask` | 结束当前任务 | — |
| 其他 | `MultiSwipe`、`TouchDown/Move/Up`、`ClickKey`、`InputText`、`Shell`、`Command`、`Screencap`、`Scroll` | 详见 `maasf3_pipeline action=schema` |

**行为字段（易忽略但常用）**

`pre_delay`(200) / `post_delay`(200) / `rate_limit`(1000) / `timeout`(20000) /
`repeat`(1) / `repeat_delay` / `max_hit` / `enabled` / `inverse`（反转识别）/
`pre_wait_freezes` / `post_wait_freezes`（等画面静止，处理动画/弹窗神器）/
`next` 里可用 `[JumpBack]X`（先处理 X 再回来）/ `[Anchor]`（别名引用）。

> 已废弃：`interrupt`、`is_sub`（5.1 起改用 `next`）。

### 3.2 interface.json（通用 UI 的入口）

```jsonc
{
    "interface_version": 2,
    "name": "MaaXZ",
    "github": "https://github.com/railzfh/MaaXZ",   // 填了才有自动更新
    "version": "1.0.0",
    "controller": [{ "name": "安卓端", "type": "Adb", "display_short_side": 720 }],
    "resource":   [{ "name": "官服", "path": ["./resource"] }],
    "agent": {                                       // 不填 = UI 不会启动你的 Python
        "child_exec": "python",
        "child_args": ["../agent/main.py"]           // ★ 相对 interface.json 所在目录（assets/）
    },
    "task": [
        { "name": "UI 上显示的任务名", "entry": "pipeline 里真实的节点名" }
    ],
    "option": { /* select / checkbox / input，可给任务加参数 */ }
}
```

### 3.3 Agent（Python 自定义逻辑）

```python
# agent/my_action.py
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

@AgentServer.custom_action("my_action_name")          # ← 与 pipeline 的 custom_action 对应
class MyAction(CustomAction):
    def run(self, context, argv) -> bool:
        param = json.loads(argv.custom_action_param)   # 参数是 JSON 字符串，要自己 parse
        return True
```

```python
# agent/main.py —— import 即完成注册（装饰器在导入时执行）
import my_action
AgentServer.start_up(socket_id)   # socket_id 由框架通过命令行传入
AgentServer.join()
```

- **不要自己启动 AgentServer**：通用 UI / 插件调试时会自动拉起并传入 `socket_id`。
- **不需要**在 agent 里创建 Resource/Tasker/Controller：宿主已创建，通过 `context` 访问。
- 打包时若用户机器没有 Python，需附带便携式解释器并改 `interface.json` 里的 `child_exec`。

#### 现成可用的通用守卫：`maa_agent_guard_ensure_main`

**用途**：任务主体开始前保证界面在「游戏主界面」。不满足就交替点返回/叉，拉不回来**硬失败**
（走 `on_error` 停下，绝不在未知界面上乱点）。

**用法**：每个任务的 `next` 首步引用它。

```jsonc
"你的任务入口": {
    "doc": "…",
    "recognition": { "type": "DirectHit" },
    "action": { "type": "DoNothing" },
    "next": ["通用_确保主界面", "任务第一步"]     // ← 守卫在前
}
```

节点已内置在 `assets/resource/pipeline/通用_确保主界面.json`（含成功/失败两个落脚点）。

**它怎么判「主界面」**（两者同时成立）：

| 判据 | 配置项 | 实测 |
| --- | --- | --- |
| 顶部玩家名含区服前缀 | `PLAYER_EXPECTED=["s521"]` + `PLAYER_ROI` | OCR 0.999 |
| 底部功能条模板命中 | `MENU_TEMPLATES` + `MENU_ROI` + `MENU_THRESHOLD=0.9` | 主界面 0.97，子界面 **0.33** |

**处理优先级**（`agent/guard.py` 的 `ensure_main_ui`）：

1. **退出确认弹窗最先处理** —— 它是覆盖层，主界面元素在其后仍可见，所以不能先判「已在主界面」就放行。
   只点「再玩一会」（认不到文字就用固定兜底坐标），**绝不碰「退出游戏」**。
2. 判定主界面 → 通过
3. 弹窗右上角 X → 4. 左上返回箭头 → 5. 兜底点左上

**换游戏怎么改**：只改 `agent/guard.py` 顶部「项目配置区」的 ROI / 文案 / 模板名；
下面的检测原语与决策循环是通用的（用 `run_recognition_direct` 内联参数，不需要在 pipeline 里建探测节点）。

**注意**：`maasf3_run` 是 headless runner，**不会拉起 agent 进程**，Custom 动作必然报
`Action is null [param.name=...]`。要真跑 agent 得用通用 UI（MFAAvalonia），或自己组装
`AgentClient.create_tcp → bind(resource) → connect()` + 起 `python agent/main.py <端口>`。

---

## 4. 干活 SOP

### 4.1 加一个关卡/任务

1. 写 pipeline 节点（每个都要 `doc`），入口节点起个好名字。
2. 用 `interface.json` 的 `task` 注册：`{ "name": "界面上显示的", "entry": "入口节点名" }`。
3. 校验：`maasf3_check`（必须 `ok: true`）。
4. 试跑（零点击）：`maasf3_stage`（默认 `dryRun`，动作全 DoNothing）。
5. 满意后才 `maasf3_run`（真实点击，务必知道后果）。

一键版：`maasf3_stage` = 生成 pipeline → 注册 interface → lint → 试跑，一次做完。

### 4.2 加一个自定义识别/动作

1. 新建/追加 `agent/xxx.py`，用 `@AgentServer.custom_action("名字")` 或 `custom_recognition("名字")` 注册。
2. 在 `agent/main.py` 里 `import xxx`（否则不注册）。
3. pipeline 节点写 `"action": "Custom"` + `"custom_action": "名字"`。
4. 参数通过 `custom_action_param`（任意 JSON）传入；Python 侧收到的是 **JSON 字符串**，要 `json.loads`。
5. 确认 `interface.json` 的 `agent` 段存在，否则 UI 根本不会拉起 agent。

### 4.3 采集模板图

1. `maasf3_shot` 截图（自动归一到 720 基准）。
2. `maasf3_probe` 在一次运行里批量问清「这些区域里有什么」（OCR 候选 / 模板分数），**替代反复试跑**。
3. `maasf3_crop` 按像素坐标裁出模板图 → 存 `assets/resource/image/`。
4. `maasf3_match` 在同一张图上自检分数（不用设备）。

---

## 5. 工具链与命令

### 本地校验（不需要设备）

```bash
# 在项目根执行；工具装在 node_modules（@nekosu/maa-tools@1.0.23）
npx @nekosu/maa-tools check        # 协议校验（CI 的 check.yml 也跑这个）

# JSON Schema 校验（Windows 必须先设 PYTHONIOENCODING，见「已知坑 #10」）
pip install json-with-comments jsonschema==4.26.0 referencing==0.37.0
python tools/validate_schema.py \
  --schema-dir deps/tools \
  --resource-dirs assets/resource \
  --interface-files assets/interface.json   # JSON Schema 校验
```

通过 MaaSmith 插件（`maasf3_*`）更省事：`maasf3_check`、`maasf3_diagnose`、`maasf3_lint`、
`maasf3_probe`、`maasf3_crop`、`maasf3_stage`、`maasf3_run`、`maasf3_test`、`maasf3_prune`。

> 注意：`maa-tools 1.0.23` 的退出码语义是「任何诊断（含 warning）都非零」，
> 所以**退出码不是失败判据**，看 `ok` 字段 / 输出内容。

### 设备相关

```bash
adb "D:\Program Files\Netease\MuMu\nx_main\adb.exe" connect 127.0.0.1:16416   # 端口会漂移
adb shell pm list packages | findstr xyjx                                      # 确认包名
```

### 调试选项（`config/maa_option.json`）

`logging`(true) / `save_draw`(false，保存识别可视化) / `stdout_level`(2) /
`save_on_error`(true) / `draw_quality`(85)。日志在 `debug/maafw.log`。

---

## 6. CI 与打包（怎么出「带 GUI 的成品」）

`.github/workflows/install.yml` 在打 tag 时：

1. 下载 **MaaFramework 成品包**（`MAAFW_VERSION`）与 **MFAAvalonia 成品包**（`MFAA_VERSION`）；
2. `tools/install.py` 把框架原生库放进 `runtimes/<rid>/native/`、GUI 放根目录；
3. 复制 `assets/resource` → `resource/`、`assets/interface.json` → 根目录，并把 `version` 改成 tag；
4. 复制 `agent/`、`README.md`、`LICENSE`；
5. 上传 artifact `MaaXZ-<os>-<arch>`，`release` job 打成 zip 发 Release。

**关键**：GUI 与框架都是**下载来的成品**，不是编译出来的——所以「打包」= 拼装，不需要编译 MaaFramework。

发版步骤：

```bash
git tag v1.0.0 && git push origin v1.0.0
```

前置条件：仓库 `Settings → Actions → General → Workflow permissions` 选 **Read and write**。
本地打包需先有 `deps/bin`（框架成品），否则 `tools/install.py` 会直接退出。

---

## 7. 已知坑（本项目实测）

| # | 坑 | 处理 |
| --- | --- | --- |
| 1 | **`assets/MaaCommonAssets` 子模块的 gitlink 指向不存在的提交**（`2327247…`，远端无此对象） | CI 里 `submodules: true` 会 checkout 失败。**已移除子模块**；OCR 模型走独立下载 |
| 2 | `tools/configure.py` 会从 `assets/MaaCommonAssets/OCR/ppocr_v6/small` 拷模型 | 该路径已随子模块移除失效；**改为直接下载** `ppocr_v6-small.zip` 解压到 `assets/resource/model/ocr/`（已完成） |
| 3 | 模板的 `install.yml` 原本 `MAAFW_VERSION` / `MFAA_VERSION` 为空 = 浮动拉 latest | 已钉 `v5.13.0` / `v2.16.1`，保证产物可复现 |
| 4 | `Toolkit.init_option("./")` 在 AgentServer 里已废弃（只转 `set_log_dir`） | 无害但会打警告；agent 侧不需要它 |
| 5 | pip 的 `MaaFw` 版本必须与框架发行版一致（它自带整套原生 DLL，默认走 `maa\bin`） | 本机已升到 **5.13.0**；可用 `MAAFW_BINARY_PATH` 环境变量改指向 |
| 6 | `MaaDbgControlUnit.dll` **不随 pip 包分发** | pip 环境里别用 `DbgController`；无头调试走 `maasf3_test` / `maa-tools test` |
| 7 | `TaskJob.wait()` 不接受 timeout；`Status` 是带 `.succeeded` 属性的包装类 | 别按旧习惯写 `.value` / int 比较 |
| 8 | `package.json` 没有 `name` 字段 | 名字只存在于 `package-lock.json`；npm 会自动改成 `MaaXZ` |
| 9 | 项目名做过全局替换后仍有残留（`.github/ISSUE_TEMPLATE/*.yaml` 里的 `MXX`） | 需要时按 `custom_configure.md` 改成 `MaaXZ` |
| 10 | **V1 平铺写法不报错但会被工具判 `form: "v1"`**，且面板读不到 `roi`（选中节点画不出框） | 一律按 V2 写；`maasf3_pipeline action=validate` 可直接测单个节点是不是 V2 |
| 11 | **截图预览会被缩放**：`maasf3_shot` 返回 720×1280 的图，但 `read_image` 预览常是 600×1066（×0.833） | 量坐标**必须在原图空间**（÷0.833），或直接用 `maasf3_ocr` / `maasf3_probe` 拿坐标 —— 目测预览图会差 ~20%，本项目已因此点错两次 |
| 12 | **手动裁模板图后自检会假阳性**：在裁剪原点所在图上匹配该模板，永远 score=1.0（匹配到自己） | 裁完**必须用 OCR 交叉核对那一区域到底是什么**；并换一张图再匹配 |
| 13 | 手写 `adb shell input tap` 吃**原生坐标**（1440×2560），不是 pipeline 的帧坐标（720×1280） | 手点时乘 k=native/短边（本项目 k=2）；pipeline 里写帧坐标，框架自己换算 |
| 14 | MuMu 桌面点图标会误开「MuMu 商店」等第三方应用 | 启动游戏用 `action: StartApp` + `package`（毫秒级、不依赖图标位置），别模仿点击桌面图标 |
| 15 | 游戏内左上有返回箭头，点错会弹「确定退出游戏吗？」 | 剧情页点「点击屏幕继续」时避开 y<100 与 x<60 区域；并给退出弹窗留兜底节点 |
| 16 | **冷启动会连弹多个公告/活动弹窗**，且公告先于登录界面出现（冷启动要 20s+） | 用「自环分发」写法处理：`公告_关闭`（OCR 认标题「公告」→ 点右上 X）`next: ["公告_关闭","分诊"]` 连续关叠着的多个 + `max_hit` 兜底；另有 `公告_确定` 兜底只认到底部按钮的公告。见 `启动游戏.json` |
| 17 | **「离线收益」弹窗时序不稳**（有时不出现、出现后会自动消失） | 给它较长识别窗口 + 高频轮询（`timeout: 8000` + `rate_limit: 500`），抓不到就把「主界面」放在 `next` 末位兜底 —— 别让它挡住任务成功 |
| 18 | `next` 里摘掉节点会让它变成 lint 报的「不可达节点」 | 每个弹窗处理节点都要有入边；用 `分诊`（DirectHit + DoNothing）当分发入口把候选串起来 |
| 19 | **`maasf3_run` 不会拉起 agent 进程** → Custom 动作报 `Action is null [param.name=...]` | 它是 headless runner；真跑 agent 要用通用 UI，或自己组装 AgentClient + 子进程（见 §3.3 守卫小节） |
| 20 | **`controller.cached_image` 是缓存、不会自动刷新** | agent 循环里连续判定会一直对着同一张旧图（表现为每轮 score 一模一样）。必须用 `post_screencap().wait().get()` 取新帧 |
| 21 | **一轮里多次截图会导致判定自相矛盾** | 每个探测各自截图时，顶部判定与底部判定基于不同时刻 → 出现「顶部命中、底部不命中」的横跳。**一轮只截一帧，所有判定共用** |
| 22 | 底部功能条用 OCR 认中文很不稳（实测只认到 1/5，且「比斗」易看成「比武」） | 改用模板匹配：主界面 0.97、子界面 0.33，判别力强得多。模板见 `主界面入口_宗门.png` / `主界面入口_游历.png` |
| 23 | **退出确认弹窗是覆盖层**，其后的主界面元素仍可见 | 守卫必须先处理退出弹窗再判主界面，否则会带着阻塞弹窗进入任务主体。该弹窗按钮实测：`再玩一会`(247,741,72,23)、`退出游戏`(400,733,70,21) |
| 24 | 返回键热区比图标大得多 | 帧坐标 (36,36)/(38,39) 与 (57,58) 实测都能触发返回；模板框中心未必等于最佳点击点 |
| 25 | 自建测试夹具时**不能在父进程 import `maa.agent.agent_server`** | import 会把该进程切成 AgentServer 模式，之后 `Resource()` 直接抛 `Failed to create resource`（AgentServer 不实现该 API） |
| 10 | `tools/validate_schema.py` 在 Windows 本地报 `UnicodeEncodeError: 'gbk' codec can't encode '\u2713'` | 它打印 `✓`/`❌`，GBK 控制台编不出来（CI 是 ubuntu 所以不炸）。跑之前设 `$env:PYTHONIOENCODING='utf-8'`。**这是编码问题不是校验失败** |
| 11 | `tools/requirements.txt` 只写了 `json-with-comments`，但 `validate_schema.py` 还需要 `jsonschema` / `referencing` | 手动补装：`pip install jsonschema==4.26.0 referencing==0.37.0`（CI 的 `check.yml` 里也是单独装的） |

---

## 8. 参考（需要细节时再查）

| 想了解 | 去哪 |
| --- | --- |
| MaaFramework 快速开始 / 术语 | <https://maafw.com/docs/1.1-QuickStarted> |
| **Pipeline 写法规范（V2）—— 写脚本前必读** | [pipeline-v2.md](./pipeline-v2.md)（本仓库内置的权威规范） |
| Pipeline 协议完整字段 | [pipeline-v2.md](./pipeline-v2.md) §2/§3，或 `maasf3_pipeline action=schema` |
| ProjectInterface 协议 | `deps/tools/interface.schema.json`（51KB）+ `docs/zh_cn/develop/how_to_develop.md` |
| Agent 写法与打包 | `docs/zh_cn/develop/agent.md` |
| 开发流程 / 发版 | `docs/zh_cn/develop/how_to_develop.md` |
| 格式化 / 插件 / Issue 模板 | `docs/zh_cn/develop/custom_configure.md` |
| 常见报错 | `docs/zh_cn/develop/faq.md` |
| Pipeline JSON Schema（编辑器补全用） | `deps/tools/pipeline.schema.json` |
