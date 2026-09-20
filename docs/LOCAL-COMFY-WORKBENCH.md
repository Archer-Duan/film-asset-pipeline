# 本地 ComfyUI 与办公室共享工作台

## 使用

1. 保持 ComfyUI 后端运行。
2. 本机运行 `start_office_workbench.ps1`。本机管理员地址为 `http://127.0.0.1:8765/objects`。
3. 在页面下方“管理成员”中创建同事的账号和密码。成员可查看共享资产与任务、提交生成、审核和下载；只有任务提交人或本机管理员可以重试该任务。
4. 同事访问 `http://算力主机办公室IP:8765/objects`，登录后使用。所有生成在算力主机运行。
5. 如同事无法连接，在主机上使用**管理员 PowerShell**执行 `./enable_office_firewall.ps1` 一次。规则只允许指定办公室网卡所在子网访问 TCP 8765，不开放 ComfyUI 8188。

`comfy.local.json` 是本机配置，已排除 Git。第一次安装可从 `comfy.example.json` 复制，并填入 `lan_address`。网站监听精确的办公室 IPv4 地址和本机回环地址，未监听其他接口。IP 变化后更新该字段并重新启动；旧 IP 的防火墙规则可按 `FilmAssetWorkbench-Office-*` 名称移除。

办公室共享使用可信局域网中的 HTTP 与 12 小时登录会话；Cookie 为 HttpOnly/SameSite Strict，密码采用带随机盐的 scrypt 哈希。若以后需要跨办公室或公网访问，应另行部署 HTTPS 和访问入口。当前无需云端 API 密钥。

## 四条生成路径

| 入口 | 输入与输出 |
|---|---|
| 补全优化图片 | 原图 + 提示词 → Qwen → 完整参考图 → 现有 2D 审核 |
| 单图生成模型 | 已有完整参考图 → Pixal3D → GLB |
| 补图并生模 | 原图 + 提示词 → Qwen → Pixal3D → 补图与 GLB |
| 四视图生模 | 正面 + 左侧 + 背面 + 右侧 → Pixal3D MultiView → GLB |

现有工作台的“优化选中静帧”“生成选中资产”在本机配置 `enabled=true` 时也使用 ComfyUI。前者每个来源生成一张补全图，后者要求图片审核通过。提示词控制 Qwen 补图阶段，不是 Pixal3D 的文本输入。

物体资产页可批量上传最多 50 张 JPEG/PNG/WebP，单张最多 20MB、4000 万像素。图片内容实际解码验证。网页不接收客户端指定的工作流文件路径、执行代码或任意下载路径。

v1.1 已启用四视图生模。选择“四视图生成模型”，分别上传前、左、后、右四张图，共同生成一个模型。方向以物体自身为准；主体必须完整入画，尽量保持比例、姿态、光照一致。视场角默认 20°，可调整。系统不下载模型。

使用 `multiview-inputs.json` 替换原模板的四个固定裁剪节点为四个 LoadImage，保留抠图、主体裁切、多视图条件与模型生成链。原 `multiview.json` 保留作来源记录；不支持直接把任意四视图拼图当作单张输入。

工作流库支持管理员导入 JSON 工作流包、检查依赖、按版本启用/停用及导出模板。同一自定义工作流同时只能启用一个版本。导入不会运行工作流；未通过依赖检查不能启用。详见 [工作流包规范](WORKFLOW-BUNDLES.md)。

单图生成多视图尚未预装对应工作流；可按工作流包规范接入支持多图片输出的工作流。服务器部署与桌面打包不在本次 v1.1 范围。

## 工作流与任务架构

- `comfy.py`：读取保存的 UI JSON，展开子图和常量分支，仅保留输出节点依赖；上传图片、提交、查询及下载。按节点 ID 配置输入和输出，校验不通过时停止，避免静默改变工作流。
- `workflows/catalog.json` 定义内置工作流，`qwen.json`、`single.json`、`multiview-inputs.json` 是运行快照。原 ComfyUI 文件不修改；项目快照为 ComfyUI 0.36 的 MoGe 节点显式补上 refine_steps=3。
- `comfy.local.json` 的 profiles 可覆盖内置工作流文件与映射，v1.0 的 image/prompt 映射仍兼容。多图工作流使用 inputs 映射；自定义工作流优先通过工作流库导入。
- `local_engine.py`：SQLite 持久化任务与批次；每项任务冻结当时的工作流、映射与提示词。Web/CLI 共用 OS 文件锁，同一时刻只有一个进程处理 GPU 队列，也等待 ComfyUI 手工任务结束。
- 已提交任务保存 `prompt_id`，服务重启继续查询。若 POST 响应丢失，用队列和历史中的 `workbench_task` 对账；无法确定时标记“需管理员核查”，不自动重复生成。
- GLB 文件头、版本和长度验证后原子归档。结果读取失败保留任务 ID 重新获取，不重新生模。运行状态是实际排队/计算/归档状态，进度百分比表示批次完成比例，不伪造单张采样百分比。
- `workflow.py` 将本地结果并入现有资产视图，不覆盖云端 manifest；来源、审核、标签和备注沿用原体系。资产按 ID 去重。
- 任务和结果位于 `data/local-assets`；账号与会话也保存在此目录，勿提交或分享数据库。

## 命令行

```powershell
# 本地补图，等待完成；生成使用已配置工作流默认参数
.\.venv\Scripts\film-assets.exe run-local --input reference.png --kind edit --prompt "保留器型，补全底部" --wait
.\.venv\Scripts\film-assets.exe run-local --input reference.png --kind model --wait
.\.venv\Scripts\film-assets.exe run-local --input reference.png --kind full --wait
# 不加 --wait 仅入队，由运行中的网站或 worker-local 执行
.\.venv\Scripts\film-assets.exe status-local
.\.venv\Scripts\film-assets.exe retry-local --task-id <任务ID>
.\.venv\Scripts\film-assets.exe worker-local
.\.venv\Scripts\film-assets.exe member-local --name colleague
```

原有 `run`、`run-3d` 及云端配置保留兼容；本地生成使用 `run-local`。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ruff check src tests
```

`tests/browser_local_check.cjs` 是对运行中的本机工作台进行只读浏览器验收：要求已有完整生成结果，检查模型加载、资产编号唯一性和页面脚本错误，不提交生成。需要 Node.js、Playwright 和 Edge；`PLAYWRIGHT_PACKAGE` 可指定 Playwright 路径。

模型预览使用本地打包的 Google model-viewer 4.1.0，许可证随文件放在 `web_assets/model-viewer.LICENSE`。项目不依赖同事浏览器访问 CDN 加载预览组件。上游：https://modelviewer.dev/ 。

## 多视图命令行

准备 `views.json`，相对图片路径以该 JSON 文件所在目录为基准：

```json
{"front":"front.png","left":"left.png","back":"back.png","right":"right.png"}
```

```powershell
.\.venv\Scripts\film-assets.exe run-local --kind multiview --inputs views.json --parameters '{"fov":20}' --wait
.\.venv\Scripts\film-assets.exe workflows-local
.\.venv\Scripts\film-assets.exe workflows-local --import-file custom-workflow.json
.\.venv\Scripts\film-assets.exe workflows-local --id custom-image --version 1.0.0 --enable
```

队列保存全部输入路径、参数和实际工作流快照。CLI 使用的原文件在任务结束前应保持存在且不修改。网页上传的图片按内容哈希保存。数据库新增字段自动迁移，v1.0 已提交任务继续使用其旧工作流快照；如旧快照与升级后的 ComfyUI 不兼容，会明确报错，不自动更改参数或重复提交。

新增浏览器验收：`tests/browser_workflow_check.cjs` 检查四视图表单、库、任务筛选、GLB 预览和手机宽度。默认不提交任务；可用 RENDER_TEST_VIEWS=1 渲染已有模型的四视图供手工 GPU 验收。

真实网页 GPU 验收脚本为 `tests/browser_multiview_submit.cjs`，只有显式设置 `RUN_GPU_SMOKE=1` 和包含 front.png/left.png/back.png/right.png 的 `MULTIVIEW_INPUT_DIR` 后才提交任务。此脚本会实际使用算力，普通自动测试不执行它。


## v1.2：资产库与按日期交付

- 两个页面共用左侧「创建资产 / 任务中心 / 资产库 / 工作流设置」和资产阶段导航。右上角显示当前账号；算力主机免登录显示主机管理员，也可以切换成员账号。
- 原始素材包括资产库上传和生成模块已提交的参考图。详情中可跳转到生成结果和对应任务；同一图片的重复上传按内容合并，关联全部任务。
- 阶段使用「原始素材、待处理图片、图片生成中、图片待审核、待生成模型、模型生成中、模型待审核、已通过模型、审核未通过、生成失败」。全部资产数量包括原始素材。
- 本地补图是 **Qwen Image Edit 2511 INT8**，当前生成单张完整图；本地四视图生模是 **Pixal3D multiview INT8**，接收正、左、后、右四图。旧的三张图选项属于云端 Seedream 5.0 Lite，在启用本地服务时隐藏。
- 已通过模型勾选后点击「批量下载模型」。ZIP 包含按资产名称命名的 GLB 和 `资产清单.json`。其他图片也支持批量下载；混合选中图片和模型时，按钮明确显示下载图片。一次最多 100 项、5GB，缺失文件会拒绝整批并提示重新选择，不悄悄漏项。

### 日期与名称

日期按**任务提交时间、北京时间 UTC+8**计算，同一天多批任务归入同一目录，跨午夜仍保持该批次提交日期。模型与图片分开：

```text
ComfyUI-Shared/output/
  2026-09-20_3d/青铜罐__多视图模型__143015__a1b2c3d4__模型-372_00001.glb
  2026-09-20_2d/青铜罐__多视图模型__143015__a1b2c3d4__正面-340_00001.png
```

名称由「资产名称或原图名 + 工作流类型 + 时间 + 任务短号 + 结果角色」组成。ComfyUI 可能追加自身流水号，但不再出现文件只有 `00001` 而无法区分资产的情况。最终下载使用资产库最新名称；对资产库改名不会移动已生成的磁盘文件。

工作台下载缓存也按日期保存在 `data/local-assets/results/`。生成模块可以填写资产名称，命令行使用 `run-local --name "青铜罐" ...`。该规则作用于从工作台提交的本地工作流，包括其 `filename_prefix` 保存节点；独立在 ComfyUI 手工运行的其他工作流使用它们自己的保存设置。

### 整理历史输出

先等待队列结束并停止工作台服务（本机及局域网两个进程）。以下命令默认预览，加 `--apply` 才执行：

```powershell
.\.venv\Scripts\python.exe -m film_asset_pipeline.organize_outputs --output-root D:\Comfy-Desktop\ComfyUI-Shared\output
.\.venv\Scripts\python.exe -m film_asset_pipeline.organize_outputs --output-root D:\Comfy-Desktop\ComfyUI-Shared\output --apply
.\start_office_workbench.ps1 -NoBrowser
```

只处理已知任务的 `workbench/<任务ID>` 文件，以及 ComfyUI 历史能确认属于这些任务的根目录图片。复制后核验 SHA-256，再更新缓存引用并移除原生旧位置文件。不会递归清除输出目录或修改无关日期目录；审核以内容 ID 关联，保持不变。重复执行不会重复生成文件。

`data/local-assets/organization/paths.json` 保存新旧路径，`tasks-before.sqlite3` 是首次整理前的任务数据库备份；旧本地结果缓存保留供回退。历史 ComfyUI 记录仍记着旧文件位置，整理后请通过工作台或新目录查看旧结果。

### 验收

`python -m pytest` 只收集 `tests/`，避免执行 `output/` 中的临时制作脚本。

`tests/browser_asset_library_check.cjs` 使用运行中的工作台做只读检查：顶部对齐、共享导航、账号入口、原图/结果关联、缩略图加载、已通过模型的 ZIP 下载以及平板宽度。需要至少一项已通过模型，不提交 GPU 任务，也不更改审核状态。可用 `PLAYWRIGHT_MODULE` 指定 Playwright 包路径。
