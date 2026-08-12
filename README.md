# MaiTu 写真

`MaiTu 写真` 是面向 MaiBot SDK 2.7+ 的异步真实手机照片插件，Manifest ID 为 `maitu.photo-studio`。插件让 bot 像真人一样发送手机拍摄的生活照片：含人物写真、不含人物的环境/景物照、管理员参考图库维护和任务状态查询。

## 主要行为

- OpenAI 兼容接口支持 Images API 与 Chat Completions 两种模式，不会在失败后跨模式重试。
- `generate_photo` 用于 bot 本人出镜的生活照。人物参考全局唯一；配置开启人物参考时，写真任务必须使用已启用的人物参考板且人物始终是第一张参考图；配置关闭后改为文字人物描述。
- `generate_scene_photo` 用于不含 bot 本人的环境/景物/物品照片，不传人物和服装参考，仅可按需使用场景参考。
- 人物为 3×2 多角度参考板，服装与场景为 2×2 参考板，场景第四格为平面图。
- 场景参考只用于模型判定合格的室内私密小空间。默认提示词允许卧室、浴室、客厅，不允许咖啡店和公共空间。
- 照片连续性按群聊隔离；私聊使用聊天流隔离。默认同一自然日、同一场景且 12 小时内复用服装。
- 所有参考图统一转为去元数据 JPEG，最长边默认 2048px，硬目标 480,000 bytes，配置绝不允许超过 500,000 bytes。
- 图片任务持久化排队。已进入付费请求但没有结果的中断任务不会自动重试；已有结果的任务只恢复投递。
- 图片发送成功后追加 Maisaka 文本上下文并主动唤醒 Planner，不把图片 Base64 重复塞入上下文。
- 插件启动时会扫描数据目录下 `references/person`、`references/outfit`、`references/scene` 的直接投放图片，统一压缩、自动标签并入库；成功导入后会移除投放原文件。人物已有单例时，新投放文件会保留并记录冲突，需管理员处理。

## 安装

将本仓库放到 MaiBot 的第三方插件目录：

```text
MaiBot/plugins/maitu-photo-studio/
  _manifest.json
  plugin.py
  requirements.txt
  maitu_photo/
```

MaiBot 镜像已经包含 SDK、HTTPX 和 Pillow。非 Docker 环境可执行：

```powershell
python -m pip install -r requirements.txt
```

首次加载后在 WebUI 配置以下内容：

- `openai.base_url`
- `openai.api_key`
- `openai.generation_model`
- `openai.reference_model`
- MaiBot 模型任务 `vlm`（标签）和 `utils`（选择）
- `plugin.admin_user_ids`

API Key 只从插件运行时配置读取。`doctor` 仅显示是否已配置，不显示密钥内容。

## Planner 工具

- `generate_scene_photo(...)`：不含 bot 本人的手机真实环境/景物/物品照片；可按需使用场景参考，不使用人物和服装参考。立即返回任务 ID。
- `generate_photo(...)`：bot 本人出镜的手机真实生活照片。人物参考配置开启时必须使用全局人物参考板；关闭后改用文字人物描述。会选择并积极复用服装与合格场景参考。立即返回任务 ID。
- `manage_reference_gallery(...)`：管理员图库管理。
- `get_image_task_status(...)`：查询本聊天任务；管理员可跨聊天查询明确的任务 ID。

规划器调用时应一次给出完整详细的拍摄需求（动作/表情/服装/场景/光线/构图/氛围）。`include_image=true` 时，状态工具通过 SDK 官方 `content_items` 返回结果图片。

## 管理员命令

默认前缀为 `/maitu`，前缀修改后需要重载插件才能更新 Command 注册信息。

```text
/maitu person extract|import|show|regenerate|clear
/maitu ref extract|import <outfit|scene> [name=名称]
/maitu ref list [outfit|scene]
/maitu ref show <id>
/maitu ref edit <id> [name=名称] [tags='{"styles":["casual"]}']
/maitu ref retag|regenerate|replace|enable|disable <id>
/maitu ref delete <id> [confirm_token=令牌]
/maitu continuity show|reset|pin|unpin [outfit|scene] [id]
/maitu task list|show|retry|cancel [task_id]
/maitu doctor
/maitu help
```

`extract` 从当前或引用消息的唯一图片生成多角度参考板；`import` 直接导入已经处理好的参考板。所有上传仍会强制压缩。删除与人物清空需要使用五分钟有效的二次确认令牌。

## 运行数据与参考图目录

插件使用 MaiBot 授予的数据目录，不把参考图写回源码目录。默认目录结构为：

```text
<MaiBot data_dir>/maitu.photo-studio/
  maitu.sqlite3
  references/person/    # 人物参考板；直接投放图片会在启动时处理
  references/outfit/    # 服装参考板
  references/scene/     # 场景参考板
  sources/              # 压缩后的来源副本
  results/              # 生图结果（按保留策略清理）
```

Docker 本地部署时，宿主机对应目录是 `.dev/release/MaiBot-1.1.4/data/MaiMBot-plugin-data/maitu.photo-studio/`，容器内对应 `/MaiMBot/data/plugins/maitu.photo-studio/`。

直接投放前建议使用不会与已托管 UUID 文件冲突的描述性文件名，例如 `summer-dress.jpg`。插件只在启动/重载时扫描；人物目录已有全局人物条目时不会自动覆盖现有条目。

## 本地开发

`dev/sync_plugin.ps1` 只同步明确列出的插件源码，不会复制 `config.toml`、插件数据、日志、`.dev/` 或 `生图测试用api.md`。

```powershell
powershell -ExecutionPolicy Bypass -File dev/sync_plugin.ps1 `
  -MaiBotRoot .dev/release/MaiBot-1.1.4

docker compose -p maibot-114 `
  -f .dev/release/MaiBot-1.1.4/docker-compose.yml `
  -f dev/docker-compose.local.yml `
  up -d core
```

WebUI 默认地址为 `http://127.0.0.1:18001`。

运行测试：

```powershell
python -m pytest -q
```
