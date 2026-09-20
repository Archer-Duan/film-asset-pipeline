# Film Asset Pipeline · 电影数字资产工作台

本地电影数字资产工作台：通过 ComfyUI 的 Qwen 工作流补全参考图，再使用 Pixal3D 生成 GLB；支持办公室成员通过网页共享主机算力。原 Seedream / 混元云端流水线和命令行保留兼容。

当前版本包含 2D/3D 批处理、阶段审核、失败重试、进度提示、资产检索与本地 GLB 下载。项目采用 Apache-2.0 许可证。用户自行申请并承担第三方模型 API 费用；本地文件不会上传到本项目的开发者服务器，但生成请求中的图片会发送到用户配置的模型服务。

## 本地算力与办公室共享

“创建资产”支持参考图上传、Qwen 补图、Pixal3D 单图／四视图生模、任务状态、失败重试及 GLB 预览。“资产库”关联原始素材与生成结果，支持审核和批量下载；“工作流设置”用于导入、检查和启停工作流。补图与生模均可使用本地 ComfyUI，办公室同事通过成员登录共享本机算力。

本机配置从 `comfy.example.json` 复制为 `comfy.local.json`，设置办公室网卡 `lan_address`，运行 `start_office_workbench.ps1`。详细部署、成员管理、工作流映射、CLI 与多视图接入边界见 [本地工作台说明](docs/LOCAL-COMFY-WORKBENCH.md)。

本地输出按日期进入 ComfyUI 的 `output/YYYY-MM-DD_2d` 和 `output/YYYY-MM-DD_3d`，文件名包含资产名称和任务短号；同事从浏览器批量下载 ZIP 即可交付。

## Windows 快速安装

1. 从 GitHub Releases 下载源码压缩包并解压到长期保留的目录，也可以使用 `git clone`。
2. 安装 Python 3.11 或更高版本，并勾选 `Add Python to PATH`。
3. 双击 `安装电影数字资产工作台.cmd`；也可以在 PowerShell 中运行 `install.ps1`。
4. 安装完成后双击桌面的 `Film Asset Workbench`。
5. 首次打开进入“模型设置”，填写你自己的即梦和混元密钥。

新安装的数据默认保存到：

```text
%LOCALAPPDATA%\FilmAssetPipeline\
├── config.toml
├── user-settings.json
├── data\input
├── data\output
├── data\models
├── data\state
└── logs
```

如果项目根目录已经存在 `config.toml`，启动器会继续使用原来的项目内工作区，已有数据不会迁移或丢失。

## 保留的云端命令行工作方式

- 模型：`doubao-seedream-5-0-lite-260128`
- 主模式：一张静帧生成一组图，最多 3 张独立资产图
- 返工模式：一张静帧生成单张图
- 输入：本地 JPG、PNG 或 WebP，自动转换为 Base64 Data URL
- 输出：本地图片、`manifest.csv` 和 `manifest.json`
- 状态：SQLite 保存，已完成任务不会重复生成

## 学习项目逻辑

可以按下面的调用链阅读源码：

```text
__main__.py
  -> cli.py                 解析命令、加载配置、组装依赖
  -> pipeline.py            2D 扫描、任务去重、并发生成、保存图片
    -> ark.py            调用方舟 API，统一解析 Base64 / URL 图片
    -> state.py          SQLite 任务状态、输出记录、失败重试
  -> model_pipeline.py      3D 扫描 2D 清单、提交任务、轮询、下载 GLB
    -> hunyuan.py         适配腾讯云 SDK 或兼容 HTTP API
    -> model_state.py     保存远端任务 ID，支持中断后继续轮询
  -> workflow.py             合并 2D/3D 清单和审核元数据，推导资产阶段
    -> web.py             FastAPI 接口和本地工作台
```

### 2D 主流程

1. `BatchPipeline.scan()` 扫描图片，并用“路径 + 文件哈希 + 提示词 + 模式 + 数量”计算任务 ID。重复运行不会重复提交；修改提示词会形成新任务。
2. `BatchPipeline.run()` 从 SQLite 取出 `pending/retry` 任务，使用线程池并发执行 `_process_task()`。
3. `_process_task()` 调用 `ArkImageClient`。只有 429、5xx 或网络错误等可重试错误才会退避重试，参数或审核错误直接标记失败。
4. 图片先通过临时文件写入并原子替换，再写入 `outputs` 表，最后导出 JSON/CSV manifest。

### 3D 主流程

3D 是异步任务：先 `submit()` 得到远端任务 ID，再循环 `query()`。任务 ID 每一步都写入 `model_state.py`，因此进程中断后会继续查询原任务，而不是重复提交并可能重复计费。远端完成后还要下载并检查 GLB 文件头，成功后才标记 complete。

### 工作台数据流

`workflow.py` 不复制图片或模型，而是读取两份 manifest，并通过路径关联 2D 输出与 3D 任务；审核标题、标签、备注和审核状态单独存入 `workflow.sqlite3`。因此生成流水线、审核数据和网页展示彼此解耦。

## 安全提醒

如果 API Key 曾经出现在聊天、截图、代码或公开仓库中，请立即在火山方舟控制台撤销并重新创建。新 Key 只放在本地 `.env`，不要写入 `config.toml`。

## 环境要求

- Python 3.11 或更高版本
- 已在火山方舟开通 `Doubao-Seedream-5.0-lite`
- 一枚未泄露的方舟 API Key

安装脚本会自动安装 FastAPI、腾讯云 SDK、系统凭据库适配器等依赖。

## 开发者初始化

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\film-assets.exe init
```

如果不希望安装为命令，也可以使用：

```powershell
$env:PYTHONPATH = "src"
python -m film_asset_pipeline init
```

初始化会从 `config.example.toml` 创建 `config.toml`，不会覆盖已有配置。

## 配置 API Key

普通用户直接使用工作台右上角的“模型设置”。程序优先将密钥保存到操作系统凭据库；如果当前系统没有可用凭据库，则回退到配置目录内的本地 `.env` 文件（明文，仅适合作为兼容方案）。HTTP API 只返回是否已配置和末四位掩码，不返回完整密钥。

开发者也可以复制 `.env.example` 为 `.env`：

复制 `.env.example` 为 `.env`，填写新 Key：

```dotenv
ARK_API_KEY=你的新Key
```

`.env` 已被 Git 忽略。

## 免费验证本地流程

先把几张 JPG/PNG 放入 `data/input`，然后执行：

```powershell
.\.venv\Scripts\film-assets.exe run --mock
```

模拟模式不会调用方舟，也不会产生生图费用。它会用输入图模拟多结果输出，用于验证扫描、数据库、命名和清单。
模拟结果保存在 `data/output/_mock`，状态保存在 `data/state/pipeline-mock.sqlite3`，不会占用或跳过后续真实任务。

## 真实组图批处理

```powershell
.\.venv\Scripts\film-assets.exe run --mode group --max-images 3
```

对应方舟控制台中的“图生图－单张图生成一组图”。默认参数：

```json
{
  "model": "doubao-seedream-5-0-lite-260128",
  "size": "2K",
  "sequential_image_generation": "auto",
  "sequential_image_generation_options": {"max_images": 3},
  "response_format": "b64_json",
  "watermark": false
}
```

## 指定单个物品返工

```powershell
.\.venv\Scripts\film-assets.exe run `
  --mode single `
  --prompt "只提取画面中央的红色皮质扶手椅。保持原造型、材质、颜色和磨损，合理补全被遮挡结构。只生成一件完整物品，居中，纯白干净背景，无人物、文字和其他道具，写实，适合作为图生3D参考图。"
```

注意：提示词和模式也是任务身份的一部分。修改提示词或模式会创建新任务，不会覆盖历史结果。

## 状态与失败重试

```powershell
.\.venv\Scripts\film-assets.exe status
.\.venv\Scripts\film-assets.exe retry
.\.venv\Scripts\film-assets.exe run
```

HTTP 429 和 5xx 会在当前运行中自动有限重试。鉴权、参数和内容审核错误会记录到清单中，不会无限重试。

## 目录

```text
data/
  input/                 # 放入电影静帧
  output/                # 生成结果和 manifest
  state/pipeline.sqlite3 # 本地任务状态
docs/
  PRD-v0.1.md
  TECHNICAL-DESIGN-2D-MVP-v0.1.md
```

## 运行测试

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## 混元图生 3D

当前默认使用腾讯云 API 3.0/SDK 接口，模型版本为 `3.1`，开启 PBR，目标面数 1,000,000，下载 GLB。SDK 负责 API 3.0 签名，需要腾讯云密钥对；它与 `sk-...` 的 OpenAI 兼容 API Key 是两套不同凭证。只在本地 `.env` 增加：

```dotenv
TENCENTCLOUD_SECRET_ID=你的SecretId
TENCENTCLOUD_SECRET_KEY=你的SecretKey
```

不要把真实密钥写进 `.env.example`、聊天、截图或源码。原 OpenAI 兼容路径仍保留；切换为 `api_style = "ai3d_openai"` 时读取 `HUNYUAN_3D_API_KEY`。

先运行不计费的 3D 模拟流程：

```powershell
.\.venv\Scripts\film-assets.exe run-3d --mock
```

模拟结果保存在 `data/models/_mock`，不会占用真实任务状态。真实运行：

```powershell
.\.venv\Scripts\film-assets.exe run-3d
```

程序默认读取 `data/output/manifest.json` 中已完成的 2D 资产图。异步任务 ID 会先写入 SQLite；中断后继续查询，不会重复提交。状态和人工重试：

```powershell
.\.venv\Scripts\film-assets.exe status-3d
.\.venv\Scripts\film-assets.exe retry-3d --task-id 你的任务ID
```

省略 `--task-id` 会重置全部失败任务；真实计费环境推荐始终指定任务 ID。

详细方案见 `docs/TECHNICAL-DESIGN-3D-MVP-v0.1.md`。

## 本地数字资产工作台

Windows 用户可以直接双击项目根目录中的 `启动电影数字资产工作台.cmd`。启动器会自动检查后台服务，必要时启动服务，等待就绪后使用默认浏览器打开工作台；重复双击不会重复启动服务。

也可以使用命令行启动界面：

```powershell
.\.venv\Scripts\film-assets-web.exe
```

浏览器访问 `http://127.0.0.1:8765`。工作台当前支持：静帧批量上传、2D 图生图按来源选择生成 1 张完整主视图或 3 张多视图、2D/3D阶段状态、资产搜索与批量选择、测试/正式3D生成、2D与3D审核、标签和备注、GLB下载。所有凭证只保存在本机的系统凭据库或本地 `.env`，页面接口不会返回完整密钥或本地绝对路径。

### 资产命名与结果流转

- 上传静帧：`{来源名称}__{内容哈希前8位}.{扩展名}`，相同内容不会重复保存。
- 2D实体文件：`{来源名称}__2d-{结果序号2位}__{任务ID前8位}.{扩展名}`。
- 工作台规范名：`{来源名称}__2D-{结果序号2位}__{资产ID}.{扩展名}`。
- 一张静帧生成多件物品时，工作台标题显示为“来源名称 · 资产 01/02/03”。
- 2D完成后结果自动进入“2D待审核”，详情页保留来源静帧、结果序号、任务ID和规范文件名，可反查原始截图。
- 同一静帧被重复提交时，主工作台只展示最新一组完整2D结果；已经关联3D模型的历史结果继续保留。

界面与后续网站集成边界见 `docs/WORKFLOW-UI-MVP-v0.1.md`。

### v1.1：多视图与工作流库

物体资产页现支持前/左/后/右四张图生成模型、按工作流自动生成表单、任务筛选及多个结果预览下载。管理员可导入/导出工作流包，检查节点和模型依赖后启用指定版本。已有任务冻结原工作流，不受后续版本切换影响。

- [使用与多视图 CLI](docs/LOCAL-COMFY-WORKBENCH.md)
- [自定义工作流包规范](docs/WORKFLOW-BUNDLES.md)
