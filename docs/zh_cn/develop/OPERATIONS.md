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

```jsonc
{
    "节点名": {
        "doc": "干什么用的（必填）",
        "recognition": "OCR",              // 怎么认
        "expected": ["开始", "点击开始"],   // 认什么
        "action": "Click",                 // 认到后做什么
        "next": ["下一个节点"],             // 之后去哪
        "roi": [0, 0, 0, 0],               // 限定识别区域，[0,0,0,0]=全屏
        "timeout": 20000,                  // 识别超时 ms，-1=一直等
        "on_error": ["错误处理节点"]        // 超时/失败的去处
    }
}
```

**识别类型（10 种）**

| type | 用途 | 关键参数 |
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

**动作类型（常用）**

| type | 用途 | 关键参数 |
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

契约只有一条：**pipeline 里的名字 ↔ Python 装饰器里的名字必须一致**。

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
| 10 | `tools/validate_schema.py` 在 Windows 本地报 `UnicodeEncodeError: 'gbk' codec can't encode '\u2713'` | 它打印 `✓`/`❌`，GBK 控制台编不出来（CI 是 ubuntu 所以不炸）。跑之前设 `$env:PYTHONIOENCODING='utf-8'`。**这是编码问题不是校验失败** |
| 11 | `tools/requirements.txt` 只写了 `json-with-comments`，但 `validate_schema.py` 还需要 `jsonschema` / `referencing` | 手动补装：`pip install jsonschema==4.26.0 referencing==0.37.0`（CI 的 `check.yml` 里也是单独装的） |

---

## 8. 参考（需要细节时再查）

| 想了解 | 去哪 |
| --- | --- |
| MaaFramework 快速开始 / 术语 | <https://maafw.com/docs/1.1-QuickStarted> |
| Pipeline 协议完整字段 | `docs/` 上游文档，或 `maasf3_pipeline action=schema` |
| ProjectInterface 协议 | `deps/tools/interface.schema.json`（51KB）+ `docs/zh_cn/develop/how_to_develop.md` |
| Agent 写法与打包 | `docs/zh_cn/develop/agent.md` |
| 开发流程 / 发版 | `docs/zh_cn/develop/how_to_develop.md` |
| 格式化 / 插件 / Issue 模板 | `docs/zh_cn/develop/custom_configure.md` |
| 常见报错 | `docs/zh_cn/develop/faq.md` |
| Pipeline JSON Schema（编辑器补全用） | `deps/tools/pipeline.schema.json` |
