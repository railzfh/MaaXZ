# MaaFramework pipeline 节点写法规范（V2）

> 给接手脚本的 AI / 人看的**唯一写法规范**。
> **一律按 V2 写**（`recognition: { type, param }` / `action: { type, param }`）——这是本文档的全部内容。
> MaaSmith 插件**写只出 V2**（旧脚本被保存时自动无损迁成 V2）；**读兼容两种**，只为让存量 V1 脚本在工具里还能用、能看图能看 ROI，
> 不是让你照着 V1 写。
>
> - 目标项目：`<项目根>/assets/resource/pipeline/*.json`（一个文件 = 一个 JSON 对象，键是节点名）
> - 协议镜像（本仓库的权威数据源）：`lib/project/nodes.js`，逐条对齐 `deps/tools/pipeline.schema.json`
> - 坐标系：**短边 720** 的绝对像素坐标（横屏 1280×720 / 竖屏 720×1280，朝向由设备当帧决定，见 `lib/core/space.js`）

---

## 0. 一句话铁律

**一个节点 = 一个 JSON 对象；识别与动作各是一个 `{ type, param }` 对象；一切「这个类型才有的参数」都放在 `param` 里。**

```jsonc
{
  "节点名": {
    "doc": "这个节点在干什么（lint 会查，必写）",
    "recognition": { "type": "OCR", "param": { "expected": ["开始"], "roi": [500, 600, 200, 60] } },
    "action": { "type": "Click", "param": { "target": true } },
    "next": ["下一个节点"],
    "on_error": ["异常处理"],
    "post_delay": 500
  }
}
```

**不要这样写（V1 平铺，插件不支持）：**

```jsonc
{
  "节点名": {
    "recognition": "OCR",          // ❌ 字符串：参数无处可放
    "expected": ["开始"],           // ❌ 平铺在节点顶层
    "roi": [500, 600, 200, 60],     // ❌ 同上
    "action": "Click",              // ❌
    "target": true                  // ❌
  }
}
```

V1 的两个坏处：① 每个文件里「哪些键属于哪个类型」要靠一张隐式清单推断，工具与人都容易读错（曾经导致面板读不到 `roi`、选中节点不画框）；② 识别与动作的参数混在同一层，改一个动作可能撞上识别参数名。**新脚本一律 V2；旧脚本被工具打开并保存时会被无损迁成 V2。**

---

## 1. 节点的完整字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `doc` | string | — | 说明这个节点干什么。**必写**（lint 会报「缺 doc」） |
| `recognition` | `{ type, param? }` | `DirectHit` | 先认到，才执行动作。10 种，见 §2 |
| `action` | `{ type, param? }` | `DoNothing` | 识别命中后干什么。21 种，见 §3 |
| `next` | string[] | `[]` | 命中后接着试哪些节点（**按顺序**，第一个命中的赢）。可带 `[JumpBack]` / `[Anchor]` 前缀 |
| `on_error` | string[] | `[]` | 识别超时 / 动作失败时跳这里 |
| `timeout` | number(ms) | 20000 | 本节点识别总超时 |
| `rate_limit` | number(ms) | 1000 | 两次识别之间的最小间隔 |
| `pre_delay` | number(ms) | 200 | 识别**前**等待 |
| `post_delay` | number(ms) | 200 | 动作**后**等待 |
| `repeat` | number | 1 | 本节点重复执行几次 |
| `repeat_delay` | number(ms) | 0 | 每次重复之间等待 |
| `max_hit` | number | — | 整个任务里这个节点最多命中几次 |
| `enabled` | bool | true | false = 跳过这个节点 |
| `inverse` | bool | false | 识别结果取反 |
| `anchor` | string[] | — | 锚点（配合 `[Anchor]` 跳转） |
| `pre_wait_freezes` / `post_wait_freezes` / `repeat_wait_freezes` | number \| object | 0 | 等画面静止：毫秒，或 `{ time, target, target_offset, threshold, method, rate_limit, timeout }`（`time` = 连续多少毫秒画面没大变化才算静止，默认 1） |
| `focus` | any | — | 调试标记（工具用它高亮），不影响运行 |
| `attach` | any | — | 附加数据，自定义动作可读 |
| `interrupt` / `is_sub` | — | — | **5.1 已废弃**，不要写（写了只给 warning） |

> `recognition` / `action` 省略时分别按 `DirectHit` / `DoNothing` 处理；类型没有参数时可以省略 `param`。

---

## 2. 识别（10 种）

`*` = 必填。`roi` = `[x,y,w,h]`（省略或 `[0,0,0,0]` = 全屏）。所有识别都可带 `roi` + `roi_offset`。

| type | 参数（除 roi） | 用途 |
|---|---|---|
| `DirectHit` | — | 无条件命中。做「纯执行动作」或调度节点用 |
| `TemplateMatch` | `template*`: 文件名 or 文件名数组<br>`threshold`: 0.7<br>`order_by`: Horizontal/Vertical/Score/Random，`index`: 0<br>`method`: 1/3/5/10001（10001=带缩放）<br>`green_mask`: false | 图标/按钮匹配（最常用） |
| `FeatureMatch` | `template*`, `count`: 4, `detector`: SIFT, `ratio`: 0.6, `order_by`, `index`, `green_mask` | 特征点匹配，抗缩放/旋转，比模板慢 |
| `ColorMatch` | `lower*`/`upper*`: `[r,g,b]` 或 `[[..],[..]]` 多段<br>`method`: 4, `count`: 1, `connected`: false, `order_by`, `index` | 找纯色块（血条、按钮底色） |
| `OCR` | `expected`: 文本数组（空 = 全都要）<br>`threshold`: 0.3, `replace`: `[["原","新"]]`<br>`only_rec`: false, `model`: 模型目录名, `color_filter`: 节点名<br>`order_by`, `index` | 认文字 |
| `NeuralNetworkClassify` | `model*`, `labels`: 数组, `expected`: 数组, `order_by`, `index` | 分类模型判「现在是哪个界面」 |
| `NeuralNetworkDetect` | `model*`, `labels`, `expected`, `threshold`: 0.3, `order_by`, `index` | 检测模型找目标框 |
| `And` | `all_of*`: 子识别数组，`box_index`: 0 | 全部满足才命中；框取第 `box_index` 个 |
| `Or` | `any_of*`: 子识别数组 | 任一满足即命中 |
| `Custom` | `custom_recognition*`: 名字（必须已在 `agent/*.py` 注册）<br>`custom_recognition_param`: 任意 JSON | 自己写 Python 识别 |

**子识别怎么写（And / Or）**：`all_of` / `any_of` 的元素**是字符串（引用另一个节点名，运行时用它那套识别）或内联识别对象**：

```jsonc
"recognition": { "type": "And", "param": { "all_of": [
  { "type": "OCR", "param": { "expected": ["背包"], "roi": [0, 0, 200, 80] } },
  { "type": "TemplateMatch", "param": { "template": "icon_bag.png", "roi": [0, 0, 200, 120] } },
  "某个已有节点名"
] } }
```

---

## 3. 动作（21 种）

| type | 参数 | 用途 |
|---|---|---|
| `DoNothing` | — | 只识别不操作（试跑时所有动作都会被换成它） |
| `Click` | `target`: true / `[x,y]` / `[x,y,w,h]` / 节点名<br>`target_offset`: `[x,y,w,h]`<br>`contact`: 0, `pressure`: 1 | 点击（最常用） |
| `LongPress` | 同 Click + `duration`: 1000 | 长按 |
| `Swipe` | `begin`/`end`: target 形态，`begin_offset`/`end_offset`<br>`duration`: 200, `end_hold`: 0, `only_hover`: false, `contact`, `pressure` | 滑动/拖拽 |
| `MultiSwipe` | `swipes*`: `[{ starting, begin, end, duration, ... }]` | 多指同时滑（双指缩放等） |
| `TouchDown` / `TouchMove` / `TouchUp` | `contact`, `target`, `target_offset`, `pressure` | 手动按下/移动/抬起（配合用） |
| `Scroll` | `target`, `target_offset`, `dx`, `dy` | 滚轮（Win32 控制器） |
| `ClickKey` | `key*`: 键码数组 | 按一次键 |
| `LongPressKey` | `key*`, `duration`: 1000 | 长按键 |
| `KeyDown` / `KeyUp` | `key*`: 单个键码 | 按下/抬起（组合键用） |
| `InputText` | `input_text*`: 文本 | 输入文本（仅 ASCII） |
| `StartApp` | `package*`: 包名或 activity | 启动应用 |
| `StopApp` | `package*` | 关闭应用 |
| `StopTask` | — | 结束当前任务 |
| `Command` | `exec*`, `args`: `string[]`, `detach`: false | 跑外部程序（`args` 支持运行期替换：`{ENTRY}` / `{NODE}` / `{IMAGE}`） |
| `Shell` | `cmd*`, `shell_timeout`: 20000 | 跑 adb shell 命令 |
| `Screencap` | `filename`, `format`: png, `quality`: 100 | 存一张截图 |
| `Custom` | `custom_action*`（须已注册）, `custom_action_param`: 任意 JSON, `target`, `target_offset` | 自己写 Python 动作 |

---

## 4. `target` 的四种形态（Click / Swipe / Custom 都用它）

| target 写法 | 含义 |
|---|---|
| `true`（默认） | **点击本节点刚识别到的位置**（识别框中心）——最常用：先 OCR/模板认到按钮，再点它 |
| `[x, y]` | 点绝对坐标（项目坐标系，短边 720） |
| `[x, y, w, h]` | 点这个矩形 —— 实际取**矩形中心** |
| `"节点名"` | 点**那个节点之前识别到**的位置（不是重新识别） |
| `[Anchor]锚点名` 等 | 带前缀指令的引用形式，见 §5 |

再加 `target_offset: [x,y,w,h]` 做微调（在算出的点/框上加偏移）。

> 常见组合：`OCR` 认到文字 → `Click` + `target: true`。若按钮位置固定、识别只是校验，用 `target: [x, y]` 更稳（面板上「取点」按钮就是往这里写值）。
> ⚠️ `true` 依赖**识别框**：用 `DirectHit` 时识别框是整屏（`[0,0,W,H]`），点它等于点屏幕正中 —— 那种情况请显式写坐标。

---

## 5. 命名与引用规则（跨文件）

- **节点名在整份工程里共享一个命名空间**：两个文件里出现同名节点 = 冲突（lint 会报，运行时行为不确定）。
- `next` / `on_error` / `anchor` 里写的名字必须**真的存在**（lint 报「悬挂引用」）。
- 名字里可以带前缀指令：`[JumpBack]节点名`（返回上一层再跳）、`[Anchor]`、`[Repeat]` 等；运行时拼出来的名字（含 `${...}`）静态解析不了，lint 会跳过。
- 任务入口（`assets/interface.json` 的 `task[].entry`）指向某个节点名；**入口节点可以不是文件里的第一个**。
- 文件名与节点名**不必相同**，但按项目习惯让入口节点与文件同名更好找（本插件的「新建脚本」就是这么做的）。

---

## 6. 写新脚本的最小可用模板

```jsonc
{
  "start": {
    "doc": "起点：拉起游戏并等待进入主界面",
    "action": { "type": "StartApp", "param": { "package": "com.example.game" } },
    "post_delay": 3000,
    "next": ["主界面"]
  },
  "同意按钮": {
    "doc": "隐私协议弹窗，点右下「同意」（拒绝在左侧，不能点错）",
    "recognition": { "type": "OCR", "param": { "expected": ["同意"], "roi": [420, 950, 150, 60], "threshold": 0.3 } },
    "action": { "type": "Click", "param": { "target": true } },
    "post_delay": 1500,
    "next": ["主界面"]
  },
  "主界面": {
    "doc": "主界面判定；认不到就等一会儿再试",
    "recognition": { "type": "TemplateMatch", "param": { "template": "icon_home.png", "threshold": 0.7 } },
    "action": { "type": "DoNothing" },
    "next": []
  }
}
```

`next` 是**候选列表**：从上到下逐个试，第一个命中的执行。所以「A 情况跳 A 节点、B 情况跳 B 节点」就写成 `"next": ["A节点", "B节点"]`。

---

## 7. 坐标怎么量（别凭感觉写）

1. 用 `maasf3_shot` 抓一张**归一化后**的截图（横屏 1280×720 / 竖屏 720×1280），在图上量像素 → 直接就是 `roi` / `target` 的值。
2. 面板里更省事：框选 → 「写入节点」写 `roi`；动作是 `Click` 时点 `target` 行的「取点」→ 在画面上点一下写坐标。
3. **坐标系是「短边 720」**：换分辨率不影响（自动缩放），但换分辨率后**模板图必须重裁**；设备横竖屏切换会让整幅画面的坐标全变（`roi` 与模板图都要重来）。
4. 别用模拟器桌面的尺寸（`adb shell wm size`）当基准 —— 游戏自己转向后它与实际帧不一致。

---

## 8. 自检清单（写完照着对）

- [ ] 每个节点都有 `doc`，说清「认什么、点什么」。
- [ ] `recognition` / `action` 都是 `{ type, param }` 对象，参数全在 `param` 里（没有顶层 `roi`/`expected`/`target`）。
- [ ] 必填参数齐（`template`、`expected` 之外的 `lower`/`upper`、`model`、`key`、`package`、`custom_*`）。
- [ ] `template` 里写的文件名**真的在** `assets/resource/image/` 下。
- [ ] `next` / `on_error` 里的节点名都存在（或带 `[JumpBack]` 前缀）。
- [ ] 末尾节点不会把整条链跑成死循环（要么 `next: []` 结束，要么回环回一个会自然失败的节点）。
- [ ] `Custom` 的名字确实在 `agent/*.py` 里注册了。
- [ ] 跑 `maasf3_lint`（或 CLI `node bin/maasf3.mjs lint`）0 error。

---

## 9. 工具会替你挡住的坑

| 症状 | 原因 | 工具怎么说 |
|---|---|---|
| 「必填缺失」但你明明写了 | 用了 V1 平铺（参数在顶层） | `validateNode` 会给一条 V1 warning，并列出要搬的顶层键；保存一次即自动迁成 V2 |
| 选中节点画面上没有 ROI 框 | 同上（读不到 `roi`） | 迁成 V2 后正常 |
| `template` 匹配一直不中 | 模板图不是从**当前分辨率/朝向**的帧上裁的 | `maasf3_match` 给分数与最佳框，`maasf3_test` 跑识别回归 |
| 点击位置偏了 | `target: true` 点的是识别框中心；识别框不准 | 改用 `[x, y]` 绝对坐标（面板「取点」） |
| 跨文件重名 / 悬挂引用 | 见 §5 | `maasf3_lint` / `maasf3_diagnose` 带 `file:line:col` |
