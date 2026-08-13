# 麦麦写真项目交接文档

> 面向后续开发 Agent、审查 Agent 和本地测试 Agent。本文档描述工作区 `D:\xm\MAITU` 在 2026-08-12 的实现和运行快照。运行态数据会变化，操作前请重新检查 SQLite 和容器日志。

## 1. 项目目标

本项目是 MaiBot SDK 2.7+ 插件“麦麦写真”，Manifest ID 为 `maitu.photo-studio`。插件向 MaiBot Planner 暴露：

- 常规图片生成。
- 必须使用全局人物参考图的真实照片生成。
- 人物、服装、场景参考图库管理。
- 图片任务状态查询。

远程仓库为 `https://github.com/tsuiraku9/maitu-photo-studio`。`生图测试用api.md` 是本地机密文件，禁止读取、输出、复制、挂载、提交或写入日志。不要把 API Key、WebUI token 或运行时 `config.toml` 写入仓库。

## 2. 当前目录

源码入口和元数据：

```text
_manifest.json       # MaiBot 插件 Manifest
plugin.py            # 薄入口、SDK Tool/Command 声明和权限边界
maitu_photo/         # 核心实现
tests/               # 单元和契约测试
dev/sync_plugin.ps1  # 白名单同步脚本
dev/docker-compose.local.yml
README.md
```

同步脚本只复制 21 个白名单源码文件到目标插件目录，不复制配置、数据库、日志或凭据。修改源码后，当前本地实例使用下面的同步命令：

```powershell
powershell -ExecutionPolicy Bypass -File dev/sync_plugin.ps1 `
  -MaiBotRoot .dev/release/MaiBot-1.1.4
```

当前运行实例的插件源码目录：

```text
D:\xm\MAITU\.dev\release\MaiBot-1.1.4\data\MaiMBot\plugins\maitu-photo-studio\
```

## 3. 架构速览

### SDK 层

- [plugin.py](D:/xm/MAITU/plugin.py)：声明 `MaiTuPhotoPlugin`、四个 Tool 和 `/maitu` 管理员 Command；`get_components()` 将配置中的工具描述和命令前缀注入注册元数据。
- [config.py](D:/xm/MAITU/maitu_photo/config.py)：`PhotoPluginConfig` 及八个配置分区。所有 WebUI 字段都有中文 `label`、`description`、`hint`；提示词字段使用 textarea 元数据。
- [sdk_compat.py](D:/xm/MAITU/maitu_photo/sdk_compat.py)：真实 SDK 与轻量测试替身的兼容层。

### 业务层

- [service.py](D:/xm/MAITU/maitu_photo/service.py)：`PhotoStudioService`，负责任务提交、worker 处理、生成、投递、Planner 唤醒、启动图库扫描和结果清理。
- [task_manager.py](D:/xm/MAITU/maitu_photo/task_manager.py)：SQLite-backed 异步队列；重启恢复 queued 任务，已进入付费请求的 running 任务不自动重试。
- [storage.py](D:/xm/MAITU/maitu_photo/storage.py)：SQLite WAL 持久化，核心表为 `reference_assets`、`image_tasks`、`task_references`、`group_continuity`。
- [selection.py](D:/xm/MAITU/maitu_photo/selection.py)：场景资格、场景指纹、服装/场景候选选择；服装排序在标签匹配后优先最近使用和复用。
- [continuity.py](D:/xm/MAITU/maitu_photo/continuity.py)：按群聊或私聊流隔离的 TTL、自然日、场景签名连续性。
- [gallery.py](D:/xm/MAITU/maitu_photo/gallery.py)：人物单例、分类哈希去重、启停、软删除、使用计数。
- [reference_service.py](D:/xm/MAITU/maitu_photo/reference_service.py)：人物 3x2、服装/场景 2x2 参考板提取、导入、压缩、自动标签、重标和重生成。
- [compression.py](D:/xm/MAITU/maitu_photo/compression.py)：统一 JPEG 压缩管线，目标默认 480,000 bytes，硬上限不超过 500,000 bytes。
- [provider.py](D:/xm/MAITU/maitu_photo/provider.py)：OpenAI 兼容 Images API、Images Edit、Chat Completions 适配器。
- [runtime.py](D:/xm/MAITU/maitu_photo/runtime.py)：调用上下文、管理员判断、当前/引用消息图片解析。
- [commands.py](D:/xm/MAITU/maitu_photo/commands.py)：管理员命令解析和帮助文本。

## 4. Planner 工具

工具均异步排队，成功时返回 `{success, task_id, status}`：

```text
generate_image(prompt, negative_prompt="", size="", model_id="")

generate_photo(
  description, outfit_hint="", scene_hint="", accessory_hint="",
  outfit_id="", scene_id="", use_person_reference,
  use_outfit_reference, use_scene_reference,
  force_new_outfit=false, force_new_scene=false,
  size="", model_id=""
)

manage_reference_gallery(operation, category="", asset_id="", name="", tags={},
                         source_message_id="", confirm_token="")

get_image_task_status(task_id="", include_image=false)
```

重要约束：

1. `generate_photo` 的人物参考是强制的。`use_person_reference=false`、配置关闭人物参考、人物不存在、人物非 `active` 都会拒绝任务；worker 还会在队列执行前再次检查。
2. provider 传图顺序固定为 `[person, outfit?, scene?]`，人物永远是第 0 张。
3. 明确指定的无效、禁用或分类错误的服装/场景 ID 直接报错；自动选择无匹配时才使用文字回退。
4. 生成成功后先保存结果，再调用 `ctx.send.image`；确认发送成功后追加 Maisaka 文本上下文并唤醒 Planner。通知失败不会重复发送图片。

## 5. 参考图库

### 目录

当前 Docker Compose 的宿主机数据目录：

```text
D:\xm\MAITU\.dev\release\MaiBot-1.1.4\data\MaiMBot-plugin-data\maitu.photo-studio\
```

容器内路径：

```text
/MaiMBot/data/plugins/maitu.photo-studio/
```

结构：

```text
maitu.photo-studio/
├─ maitu.sqlite3              # SQLite 主库（WAL 模式）
├─ maitu.sqlite3-wal
├─ maitu.sqlite3-shm
├─ references/
│  ├─ person/                 # 已处理人物参考板
│  ├─ outfit/                 # 已处理服装参考板
│  └─ scene/                  # 已处理场景参考板
├─ sources/                   # 压缩后的来源副本
├─ results/                   # 生图结果，按保留策略清理
├─ queue/                     # 持久化任务载荷
└─ uploads/                   # 管理员上传临时载荷
```

参考资产真实路径由 `ReferenceService._artifact_path()` 生成：

```text
<data_dir>/<sources|references>/<person|outfit|scene>/<uuid32>.jpg
```

### 直接投放文件

管理员可以把图片直接放到 `references/person`、`references/outfit` 或 `references/scene` 的**直接子目录**。插件每次启动/重载时后台扫描：

- 跳过已经登记的 UUID 文件、隐藏文件、临时扩展名、目录和符号链接。
- 走统一压缩和标签流程；导入成功后删除投放原文件。
- 服装/场景按原始哈希去重。
- 人物是全局单例；已有人物时不会覆盖，新文件会保留并记录冲突。
- 扫描是异步的，写真 worker 会等待扫描完成后再检查人物参考。

直接投放的图片应使用描述性文件名，例如 `summer-dress.jpg`。`import` 适合已经生成好的多角度参考板；`extract` 适合从普通原图调用模型提取多角度板。

## 6. 管理员命令

默认前缀 `/maitu`，只有 `plugin.admin_user_ids` 中的用户可执行。面向用户的命令使用中文；英语旧命令仍然可解析。

```text
/maitu 帮助
/maitu 诊断
/maitu 人物 查看|提取|导入|生成|重生成|清空
/maitu 参考 提取|导入 服装|场景 [名称=名称]
/maitu 参考 列表 [服装|场景]
/maitu 参考 查看|编辑|重标|重生成|替换|启用|停用|删除 <id>
/maitu 连续 查看|重置|固定|取消固定 [服装|场景] [id]
/maitu 任务 列表|查看|重试|取消 [任务ID]
```

`人物 生成` 仅在尚无人物参考时可用，会读取宿主 `personality.personality` 和 `bot.nickname` 无原图生成面部参考板。导入图片不要使用消息 ID：当前消息单图、回复/引用单图，或本聊天最近一张单图均可。危险操作 `人物 清空` 和 `参考 删除` 首次调用会返回五分钟有效确认令牌；不要把令牌写入交接记录或日志。

## 7. 配置要点

配置由 MaiBot WebUI 管理，持久化在插件目录自己的 `config.toml`。代码配置模型分区：

```text
plugin       插件启用、命令前缀、管理员 ID
openai       Base URL、API Key、生成/参考图模型、接口模式、超时
model_tasks  MaiBot 的 vlm/utils 任务名、token 和温度
references   人物/服装/场景开关、自动补库、压缩上限
continuity   TTL、自然日和时区
tasks        worker、队列和清理策略
output       图片投递、状态图片和 Planner 通知
prompts      全部生图、提取、标签、选择、连续性和 Planner 文本
```

默认参考图压缩目标为 `480000` bytes，配置校验不允许超过 `500000`。运行提示词会热更新；工具描述和命令前缀在注册时注入，修改后应重载插件。

## 8. 本地运行与测试

当前 Compose 项目：`maibot-114`，服务名 `maim-bot-core`，WebUI：

```text
http://127.0.0.1:18001
```

常用命令：

```powershell
# 同步源码
powershell -ExecutionPolicy Bypass -File dev/sync_plugin.ps1 -MaiBotRoot .dev/release/MaiBot-1.1.4

# 重启 Core（需要 Docker Desktop）
docker compose -p maibot-114 `
  -f .dev/release/MaiBot-1.1.4/docker-compose.yml `
  -f dev/docker-compose.local.yml restart core

# 测试（工作区提供隔离依赖）
$env:PYTHONPATH='.dev/test-deps;.dev/maibot-plugin-sdk'
python -m pytest -q

# 静态检查
.dev/ruff-env/Scripts/ruff.exe check .
.dev/ruff-env/Scripts/ruff.exe format --check .
python -m compileall -q plugin.py maitu_photo tests
```

验收时先看插件加载日志：

```powershell
docker compose -p maibot-114 `
  -f .dev/release/MaiBot-1.1.4/docker-compose.yml `
  -f dev/docker-compose.local.yml logs --since 2m core
```

期望看到 `已加载=1（maitu.photo-studio），失败=0` 和 `Reference startup scan complete`。

## 9. 当前运行快照

最近一次脱敏检查（2026-08-12）：

- Docker Core：running，端口 `18001 -> 8001`。
- 插件：loaded=1，failed=0。
- WebUI SDK Schema：8 个分区、61 个字段，中文标题/说明/提示齐全。
- 数据库 active 资产：人物 1、服装 1、场景 1。
- 资产使用次数：人物 2、服装 4、场景 4（会随测试变化）。
- 任务统计：photo 已发送 6；reference_extract 已发送 3、当前有 1 个管理员参考提取任务处于 running。

后续 Agent 接手时先查询该 running 任务，不要未经用户确认取消或重复提交，以免重复调用付费模型。

## 10. 推荐测试顺序

1. 在 WebUI 确认管理员 ID、OpenAI Base URL/API Key、生成模型、参考图模型以及 MaiBot `vlm`/`utils` 任务。
2. 用 `/maitu doctor` 检查配置状态；输出只应显示是否配置，不会显示密钥。
3. 管理员执行 `person show`，确认人物状态为 `active`；若为 `needs_review`，先 `person regenerate` 或 `ref retag`，再 `enable`。
4. 测试 `generate_image`，用 `get_image_task_status(include_image=true)` 观察任务和投递状态。
5. 测试 `generate_photo`，检查 provider 请求和 `task_references` 中人物、服装、场景 ID。
6. 在同一群同一场景连续提交第二张照片，确认服装 ID 复用、`selection_source` 为连续性来源。
7. 改变场景、跨群、跨日或传 `force_new_outfit=true`，确认服装重新选择。
8. 把一张图片放进对应 `references/*` 目录，重载插件后调用 `ref list`，确认压缩、标签、去重和状态。
9. 检查图片投递成功后 Planner 上下文追加和主动唤醒各只发生一次。

## 11. 已知边界与注意事项

- 真实 OpenAI 兼容服务的端到端行为取决于服务商是否正确支持多图 Images Edit 或多模态 Chat Completions；单元测试已覆盖请求结构和参考图顺序，但仍需用户用已配置模型验证。
- 自动标签必须返回严格 Schema JSON；`confidence` 范围是 0..1。失败条目进入 `needs_review`，不会自动参与图库选择。
- 场景参考只用于卧室、浴室、客厅等室内私密小空间；咖啡店、商场、街道、办公室等不会作为合格场景参考。
- 不要把完整用户提示词、API Key、WebUI Token 或确认令牌写入新日志、测试输出或文档。
- 不要使用 `git reset --hard`、`git checkout --` 等破坏性命令，也不要恢复/覆盖用户已有的配置和图库数据。

## 12. 接手检查清单

- [ ] 先读本文件、`README.md` 和当前 `AGENTS.md` 指令。
- [ ] `git status` 或等价方式确认工作区现有改动，保留用户改动。
- [ ] 检查容器状态、插件加载日志和 WebUI 是否可访问。
- [ ] 读取 SQLite 仅做脱敏汇总，确认 queued/running 任务后再操作。
- [ ] 修改源码后运行 70+ 单元测试、Ruff 和 compileall。
- [ ] 使用同步脚本更新实际插件目录，再重启/重载插件。
- [ ] 最终报告明确说明未做的真实模型测试和任何残余风险。
