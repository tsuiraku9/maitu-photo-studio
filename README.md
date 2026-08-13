# MaiTu 写真

`MaiTu 写真` 是面向 MaiBot SDK 2.7+ 的异步真实手机照片插件，Manifest ID 为 `maitu.photo-studio`。插件让 bot 像真人一样发送手机拍摄的生活照片：含人物写真、不含人物的环境/景物照、管理员参考图库维护和任务状态查询。

## 主要行为

- OpenAI 兼容接口支持 Images API 与 Chat Completions 两种模式，不会在失败后跨模式重试。
- `generate_photo` 用于 bot 本人出镜的生活照。人物参考全局唯一；配置开启人物参考时，写真任务必须使用已启用的人物参考板且人物始终是第一张参考图；配置关闭后改为文字人物描述。
- `generate_scene_photo` 用于不含 bot 本人的环境/景物/物品照片，不传人物和服装参考，仅可按需使用场景参考。
- 人物为 3×2 面部身份参考板，不描述、不复制服装；服装与场景为 2×2 参考板，场景第四格为平面图。服装完全由服装参考图控制。
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

默认前缀为 `/maitu`，只有 `plugin.admin_user_ids` 中的用户可执行。修改前缀后需要重载插件才能更新 Command 注册信息。

### 帮助与诊断

```text
/maitu 帮助
/maitu 诊断
```

### 人物参考

人物参考板只保留面部与身份，不描述、不复制服装。服装完全由服装参考图控制。

```text
/maitu 人物 查看
/maitu 人物 提取
/maitu 人物 导入
/maitu 人物 生成
/maitu 人物 生成 补充=短发圆脸
/maitu 人物 重生成
/maitu 人物 清空
```

- `人物 提取`：从当前消息、引用消息或本聊天最近一张单图，整理成 3×2 面部参考板。
- `人物 导入`：直接导入已经处理好的面部参考板。
- `人物 生成`：当前没有人物参考时，读取 MaiBot 的人格设定，无原图生成面部参考板。可用 `补充=` 追加不含服装的外貌说明。
- `人物 清空`：首次返回五分钟有效确认令牌，再次执行才真正清空。

### 服装与场景参考

```text
/maitu 参考 提取 服装 [名称=夏天裙子]
/maitu 参考 导入 场景 [名称=卧室]
/maitu 参考 列表 [服装|场景]
/maitu 参考 查看 <参考图ID>
/maitu 参考 编辑 <参考图ID> [名称=名称] [标签='{"styles":["casual"]}']
/maitu 参考 重标|重生成|替换|启用|停用 <参考图ID>
/maitu 参考 删除 <参考图ID>
```

### 连续性与任务

```text
/maitu 连续 查看
/maitu 连续 重置
/maitu 连续 固定 服装|场景 <参考图ID>
/maitu 连续 取消固定 [服装|场景]
/maitu 任务 列表
/maitu 任务 查看 [任务ID]
/maitu 任务 重试 <任务ID>
/maitu 任务 取消 <任务ID>
```

### 如何导入图片

不要填写消息 ID。任选下面一种方式即可：

1. 把图片和命令发在同一条消息里。
2. 回复或引用一条只含单张图片的消息，再发命令。
3. 先单独发一张图，再发命令；插件会使用本聊天最近的一张单图。

所有上传仍会强制压缩。删除与人物清空需要五分钟有效的二次确认令牌。英语旧命令（如 `person extract`）仍然可用。

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
