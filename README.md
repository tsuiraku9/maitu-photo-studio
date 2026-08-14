# 麦麦写真

> 📷 让麦麦更真实的发送照片
>
> **⚠️ 免责声明**：本插件由 AI 辅助生成，代码可能存在未知缺陷或不当行为。使用前请自行审阅源码、评估风险，并对其在你的环境中产生的任何后果负责。
>
> **💳 额度提醒**：启用照片工具调用建议、自动补库或频繁使用生图功能，可能增加图片生成模型的调用次数并消耗更多服务商额度。请根据服务商的计费规则控制使用频率。

`麦麦写真` 是面向 MaiBot 的异步真实手机照片插件（MaiBot 配置页显示为 `MaiTu 写真`，Manifest ID：`maitu.photo-studio`）。它把 Planner 的一次“发张照片”请求变成可持久化、可追踪的后台任务：根据聊天语境生成自然的生活照片，在成功后投递回原聊天。

## 核心卖点

| 核心能力 | 为麦麦带来的体验 |
| --- | --- |
| 📷 **真实照片生成** | Planner 根据画面是否包含麦麦，选择人物写真或无人物环境照流程；支持动作、表情、地点、光线、构图与氛围等完整拍摄需求。 |
| 🗂️ **可控参考图库** | 用全局人物参考板稳定身份，用服装和合格私密室内场景参考板稳定穿搭与地点；参考图会经过压缩、标签校验和状态管理。 |

围绕这两项核心能力，插件补齐了照片发送所需的可靠性和连续性：

- 🔁 **聊天连续性**：同一群聊或私聊流在相同场景、同一自然日和有效时长内，优先复用上次的服装与场景参考；管理员可以固定或重置。
- ✨ **自动补库**：人物写真成功投递后，可从结果图异步补充本次缺失的服装或场景参考，不会阻塞用户收到照片。
- ⏱️ **任务可追踪**：每次工具调用都会返回 `task_id`；队列、结果、实际使用的参考图和投递状态都会持久化，可在重载后继续查看或处理。
- 🛡️ **队列与计费保护**：排队任务可在重载后恢复；已经发起付费图片请求却没有保存结果的运行中任务不会自动重试，以避免重复计费。

### 面向 Planner 的工具

| 使用场景 | 工具 | 行为 |
| --- | --- | --- |
| bot 本人出镜的自拍、他拍或生活照 | `generate_photo` | 默认使用全局人物参考板，并可复用服装和合格场景参考。 |
| 不含 bot 本人的房间、景物或物品照片 | `generate_scene_photo` | 不传人物或服装参考，可按需使用合格场景参考。 |
| 查询异步任务 | `get_image_task_status` | 返回任务状态；启用 `include_image` 时可附带已生成图片。 |
| 由 Planner 管理图库 | `manage_reference_gallery` | 默认不注册。需显式开启、重载插件，并且调用者必须是插件管理员。 |

所有生图工具均异步排队；参数、权限和配置校验通过后会立即返回 `{success, task_id, status}`，实际图片稍后由后台 worker 生成并投递。校验失败会同步返回错误且不会创建任务。同一需求不要重复提交；图片生成和参考图提取可能产生服务商费用。

## 工作流程

### 生图任务生命周期

```mermaid
flowchart TD
    A[聊天中出现照片需求] --> B{画面是否包含 bot 本人}
    B -- 包含 --> C[Planner 调用 generate_photo]
    B -- 不包含 --> D[Planner 调用 generate_scene_photo]
    C --> E{同步校验输入和已知边界条件}
    D --> E
    E -- 不通过 --> X[返回 success=false<br/>不创建任务]
    E -- 通过 --> F[持久化 queued 任务<br/>立即返回 task_id]
    F --> G[(SQLite 后台队列)]
    G --> H[Worker 原子领取任务]
    H --> I[等待启动时参考图库扫描完成]
    I --> J{任务类型}
    J -- 人物写真 --> K[校验或获取人物参考板]
    K --> L[选择服装和场景参考：显式 ID<br/>→ 聊天连续性 → 模型选择 → 标签评分 → 文字回退]
    J -- 无人物照片 --> M[不使用人物或服装参考<br/>只按需选择合格场景参考]
    L --> N[组装提示词和参考图<br/>人物写真顺序固定：人物 → 服装 → 场景]
    M --> N
    N --> O[标记图片请求已发起<br/>调用已配置的图片服务]
    O --> P{图片服务生成成功}
    P -- 否 --> Q[标记 failed；按设置通知 Planner]
    P -- 是 --> R[保存结果并标记 generated<br/>记录实际参考图使用和连续性]
    R --> S{图片投递成功}
    S -- 否 --> Q
    S -- 是 --> T[标记 sent；按设置追加上下文<br/>并唤醒 Planner]
    T --> U{含人物写真且已开启自动补库}
    U -- 是 --> V[进入自动补库流程]
    U -- 否 --> W[结束；可用 task_id 查询状态]
```

`success=true` 只表示任务已成功入队，不代表图片已经生成或发送；图片服务配置、参考图模型和实际调用在后台 worker 中处理，失败时任务会标记为 `failed`。`generate_photo` 在默认严格模式下需要可用的人物参考板；关闭严格要求后，人物板缺失时才会使用 MaiBot 昵称和人格文字回退。服装和场景只会使用已启用、标签有效的条目；场景还必须是卧室、浴室、客厅等合格的私密室内小空间。`generate_scene_photo` 不会传入人物或服装参考，也不会触发自动补库。

### 参考图生成与自动补库

```mermaid
flowchart TD
    A[管理员上传原图、导入参考板<br/>或基于人格生成人物参考] --> B[创建 reference_* 后台任务]
    C[人物写真已成功投递] --> D{references.auto_extract_missing 已开启}
    D -- 否 --> Z[不创建补库任务]
    D -- 是 --> E[检查本次没有选中的参考图]
    E --> F{允许服装参考且未选中服装板}
    E --> G{允许场景参考且未选中场景板<br/>目标场景合格，且生成结果复核通过并与原场景指纹一致}
    F -- 是 --> H[创建 reference_extract 子任务：服装]
    G -- 是 --> I[创建 reference_extract 子任务：场景]
    F -- 否 --> J[跳过服装补库]
    G -- 否 --> K[跳过场景补库]
    H --> B
    I --> B
    B --> L{操作是否需要参考图模型}
    L -- 提取、重生成、人物生成 --> M[调用参考图模型生成参考板<br/>人物为 3x2；服装和场景为 2x2]
    L -- 导入、替换或重标 --> N[使用已准备好或已有的参考板]
    M --> O[统一压缩待处理图片与参考板]
    N --> O
    O --> P[VLM 标签校验]
    P --> Q{标签有效且场景满足私密性要求}
    Q -- 否 --> R[保存并入库为 needs_review<br/>不参与自动选择]
    Q -- 是 --> T{references.auto_enable_generated_references 已开启}
    T -- 是 --> U[保存并入库为 active<br/>可参与后续选择]
    T -- 否 --> V[保存并入库为 disabled<br/>等待管理员启用]
    U --> W[自动补出的服装或场景仅在<br/>来源照片仍是当前连续性记录时写回连续性]
```

自动补库只处理含人物写真中缺失的**服装**和**场景**参考，不会自动生成人物参考板。它始终发生在图片确认投递之后，因此参考图模型慢、失败或待审核都不会阻塞已经完成的照片。场景补库额外要求：计划中的场景和生成结果都被判定为合格私密空间，且生成结果的场景指纹与原任务一致。补库完成得较晚时，新参考图仍会入库；但只有来源照片仍是该聊天当前、同场景的连续性记录时，才会写回连续性，避免旧任务覆盖较新的照片状态。

需要等待结果或排查状态时，使用 `get_image_task_status`，也可以让管理员用 `/maitu 任务 查看 <任务ID>` 查询。参考图任务同样有独立的 `task_id`，并可通过 `/maitu 任务 列表` 查看。

## 运行要求

- Python `>=3.12`
- MaiBot Plugin SDK `>=2.7.1,<3.0.0`
- 支持 Images API 或 Chat Completions 的 OpenAI 兼容图片服务
- MaiBot 中可用的 `vlm` 模型任务（参考图标签）和 `utils` 模型任务（场景判断与图库选择）

在 WebUI 中填入服务商实际支持的模型 ID。`openai.base_url` 可带或不带 `/v1`；`generation_mode` 与 `reference_mode` 必须和服务商接口能力一致，插件不会在两种模式间自动重试。

## 安装

将仓库放入 MaiBot 第三方插件目录，保留下面的结构：

```text
MaiBot/plugins/maitu-photo-studio/
  _manifest.json
  plugin.py
  requirements.txt
  README.md
  maitu_photo/
```

MaiBot 运行环境若未预装依赖，可执行：

```powershell
python -m pip install -r requirements.txt
```

本地开发与测试可安装开发依赖：

```powershell
python -m pip install -e ".[dev]"
```

## 首次使用

1. 在 MaiBot 加载或重载插件，然后打开 WebUI 的 `MaiTu 写真` 配置页。
2. 配置 `plugin.admin_user_ids`。只有这里列出的用户可以执行 `/maitu` 管理命令和受限图库操作。
3. 配置 `openai.base_url`、`openai.api_key`、`openai.generation_model`、`openai.reference_model`、两种接口模式，以及 MaiBot 的 `model_tasks.tagging_task_name`（通常为 `vlm`）和 `model_tasks.selection_task_name`（通常为 `utils`）。
4. 保存后重载插件，再由管理员执行：

   ```text
   /maitu 诊断
   ```

   诊断会显示配置状态、模型、参考图和排队任务摘要，但不会输出 API Key 内容。
5. 默认启用“强制要求人物参考图”。先把一张图片与下列命令放在同一条消息中，或回复一条只含单张图片的消息后发送命令：

   ```text
   /maitu 人物 提取
   ```

   也可以使用 `/maitu 人物 导入` 导入已处理好的面部参考板，或在没有人物图时使用 `/maitu 人物 生成` 根据 MaiBot 昵称和人格设定生成。默认 `references.person_reference_enabled=true` 且 `references.require_person_reference=true`：没有可用且已启用的人物参考板会拒绝任务。关闭 `references.require_person_reference` 后，缺图时回退到 MaiBot 昵称和人格文字；关闭 `references.person_reference_enabled` 后，默认不附人物板并直接使用该回退，但工具显式传 `use_person_reference=true` 仍会尝试人物参考（若同时启用严格要求，缺图会拒绝）。
6. 可选地导入服装与私密室内场景参考图。普通照片或原图用“提取”；已经排版好的 2×2 参考板才用“导入”：

   ```text
   /maitu 参考 提取 服装 名称=夏日连衣裙
   /maitu 参考 导入 场景 名称=卧室窗边
   ```

   场景参考只接受卧室、浴室、客厅等室内私密小空间；咖啡店、商场、街道、办公室等不会参与生图。
7. **可选建议，并不是安装或使用插件的必需配置：** 如需让 Planner 在合适时主动调用照片工具，可在 MaiBot 本机的 `bot_config.toml` 中，将下面这句话追加到现有 `behavior_style` 文本（沿用当前配置的 TOML 引号或多行写法，不要覆盖原有行为设定）：

   ```text
   你可以调用generate_scene_photo和generate_photo工具生成照片
   ```

   这只是提升自动调用效果的本地行为建议；不添加也不影响插件安装、配置或手动调用。根据画面是否包含 bot 本人选择工具，并避免对同一需求重复调用。`bot_config.toml` 是本地运行配置，不要提交到 Git；本仓库不跟踪这份文件。
8. 让 Planner 调用对应工具，并使用返回的 `task_id` 查询进度。图片成功投递后，默认会追加上下文并唤醒 Planner；可通过 `output.notify_planner` 关闭。

## 工具选择

### `generate_photo`

用于 bot 本人出镜的真实生活照。`description` 应一次说明动作、表情、服装、配饰、地点、光线、构图与氛围；可用 `outfit_hint`、`scene_hint`、`accessory_hint` 补充信息。

- 有可用人物参考板时，它始终是第一张参考图。
- 默认严格要求人物参考板，保证身份稳定：在 `person_reference_enabled=true` 且 `require_person_reference=true` 时，缺少可用人物板或显式传 `use_person_reference=false` 都会拒绝。关闭 `require_person_reference` 后，缺图才会回退到 MaiBot 昵称和人格文字；关闭 `person_reference_enabled` 后默认走该回退，但显式传 `use_person_reference=true` 仍会尝试使用人物板。
- 有服装参考板时由参考图控制服装；没有合适参考时使用 `clothing_style_prompt` 与文字提示回退。
- 场景参考只在目标被判定为合格私密室内空间时使用。
- 同一群聊按 `group_id` 隔离，私聊按聊天流隔离。默认同一自然日、同一场景指纹且 12 小时内优先复用服装和场景参考；管理员可固定或重置连续性。

### `generate_scene_photo`

用于不含 bot 本人的环境、景物或物品照片，例如房间一角、窗外、桌上的食物和空镜。它不会传入人物或服装参考，`description` 中仍应写清主体、环境、时间、光线、构图、氛围，以及是否允许路人或其他物品出现。

### `get_image_task_status`

不传 `task_id` 时查询当前聊天最近的任务；普通用户不能跨聊天查询。`include_image` 默认是 `false`；传 `true` 且 `output.include_image_in_status=true` 时，若结果文件仍在保留期内，响应会通过 SDK `content_items` 附带图片。配置关闭或结果文件已清理时仍返回状态，但不会附图。

### `manage_reference_gallery`

默认关闭，关闭时 Planner 看不到该工具。开启 `references.planner_gallery_management_enabled` 后必须重载插件，且调用上下文仍必须属于 `plugin.admin_user_ids` 中的管理员。日常图库维护建议优先使用下方管理员命令。

## 管理员命令

默认前缀为 `/maitu`。修改 `plugin.command_prefix` 后需要重载插件。命令中的 `<参考图ID>` 需要替换为实际 ID；图片导入、提取和替换支持当前消息、引用消息或本聊天最近的一张单图，不需要手填消息 ID。

```text
/maitu 帮助
/maitu 诊断

/maitu 人物 查看
/maitu 人物 提取
/maitu 人物 导入
/maitu 人物 生成
/maitu 人物 生成 补充=短发圆脸
/maitu 人物 重生成
/maitu 人物 清空

/maitu 参考 提取 服装 名称=夏天裙子
/maitu 参考 导入 场景 名称=卧室
/maitu 参考 列表
/maitu 参考 列表 服装
/maitu 参考 查看 <参考图ID>
/maitu 参考 编辑 <参考图ID> 名称=名称 标签='{"styles":["casual"]}'
/maitu 参考 重标 <参考图ID>
/maitu 参考 重生成 <参考图ID>
/maitu 参考 替换 <参考图ID>
/maitu 参考 启用 <参考图ID>
/maitu 参考 停用 <参考图ID>
/maitu 参考 删除 <参考图ID>

/maitu 连续 查看
/maitu 连续 重置
/maitu 连续 固定 服装 <参考图ID>
/maitu 连续 固定 场景 <参考图ID>
/maitu 连续 取消固定

/maitu 任务 列表
/maitu 任务 查看 <任务ID>
/maitu 任务 重试 <任务ID>
/maitu 任务 取消 <任务ID>
```

删除参考图和清空人物参考是二次确认操作。第一次会返回一个只对当前管理员、五分钟内有效的令牌；不要把令牌写入日志、截图或仓库。第二次执行示例：

```text
/maitu 人物 清空 确认令牌=<上一条返回的令牌>
/maitu 参考 删除 <参考图ID> 确认令牌=<上一条返回的令牌>
```

## 参考图与数据

插件使用 MaiBot 提供的插件 `data_dir`，不会把运行数据写回源码目录。默认结构如下：

```text
<MaiBot plugin data_dir>/
  maitu.sqlite3           # SQLite 元数据，WAL 模式
  references/
    person/               # 3x2 面部身份参考板
    outfit/               # 2x2 服装参考板
    scene/                # 2x2 私密场景参考板，含平面图
  sources/                # 压缩后的来源副本
  results/                # 已生成图片
  queue/                  # 未完成任务载荷
  uploads/                # 管理员上传的临时载荷
```

人物参考板只保留面部与身份，不复制服装。所有入库参考图都会去除元数据并压缩为 JPEG；默认最长边为 2048px、目标上限为 480,000 bytes，配置不能超过 500,000 bytes。默认结果图片保留 24 小时，任务元数据保留 30 天；图库资源不受结果清理影响。`references/`、`sources/`、`results/` 和 `uploads/` 可能包含用户图片，`queue/` 可能包含未完成任务载荷；应限制文件系统权限并纳入自己的备份与清理策略。

本仓库的 Docker 本地开发环境中，宿主机路径为 `.dev/release/MaiBot-1.1.4/data/MaiMBot-plugin-data/maitu.photo-studio/`，容器内路径为 `/MaiMBot/data/plugins/maitu.photo-studio/`。

可以将待导入图片直接放入 `references/person`、`references/outfit` 或 `references/scene` 的直接子目录。插件在启动或重载后异步扫描：成功导入才删除投放文件；人物目录已经有全局人物参考时，新文件会保留并记录冲突。使用描述性文件名，例如 `summer-dress.jpg`。

运行时 `config.toml`、插件数据、日志、数据库、确认令牌和 API Key 都不应提交到 Git，也不应写入新日志或文档。

## 本地开发与验证

仓库提供的同步脚本只复制白名单中的插件源码和 README，不会复制运行配置、数据、日志、`.dev/` 或本地凭据。

```powershell
powershell -ExecutionPolicy Bypass -File dev/sync_plugin.ps1 `
  -MaiBotRoot .dev/release/MaiBot-1.1.4

docker compose -p maibot-114 `
  -f .dev/release/MaiBot-1.1.4/docker-compose.yml `
  -f dev/docker-compose.local.yml `
  up -d core

# 已运行实例同步源码后重载 Core
docker compose -p maibot-114 `
  -f .dev/release/MaiBot-1.1.4/docker-compose.yml `
  -f dev/docker-compose.local.yml `
  restart core
```

本地 WebUI 默认地址为 `http://127.0.0.1:18001`。使用仓库提供的本地 SDK 依赖运行测试：

```powershell
$env:PYTHONPATH='.dev/test-deps;.dev/maibot-plugin-sdk'
python -m pytest -q
.dev/ruff-env/Scripts/ruff.exe check --no-cache .
.dev/ruff-env/Scripts/ruff.exe format --check --no-cache .
python -m compileall -q plugin.py maitu_photo tests
```

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| `generate_photo` 提示没有人物参考图 | 默认严格组合要求 active 人物参考板；由管理员提取、导入或生成。关闭 `references.require_person_reference`，或将 `references.person_reference_enabled=false` 且不显式请求人物参考，才会使用人格文字回退；工具显式请求人物参考时仍按开关校验。 |
| 场景参考没有生效 | 检查目标是否是合格私密室内空间、场景参考是否已启用且标签通过校验。 |
| 任务已排队但没有图片 | 用 `get_image_task_status(task_id=...)` 或 `/maitu 任务 查看 <任务ID>` 查询；管理员可按状态决定重试或取消。 |
| Planner 无法管理图库 | 检查图库管理开关、管理员 ID，并在修改开关后重载插件。 |
| 图片发送后没有 Planner 后续动作 | 检查 `output.notify_planner`、Maisaka 能力和插件日志；图片不会因通知失败而重复发送。 |
