# 本地 ComfyUI 与办公室共享工作台

## 使用

1. 保持 ComfyUI 后端运行。
2. 本机运行 `start_office_workbench.ps1`。本机管理员地址为 `http://127.0.0.1:8765/objects`。
3. 在页面下方“管理成员”中创建同事的账号和密码。成员可查看共享资产与任务、提交生成、审核和下载；只有任务提交人或本机管理员可以重试该任务。
4. 同事访问 `http://算力主机办公室IP:8765/objects`，登录后使用。所有生成在算力主机运行。
5. 如同事无法连接，在主机上使用**管理员 PowerShell**执行 `./enable_office_firewall.ps1` 一次。规则只允许指定办公室网卡所在子网访问 TCP 8765，不开放 ComfyUI 8188。

`comfy.local.json` 是本机配置，已排除 Git。第一次安装可从 `comfy.example.json` 复制，并填入 `lan_address`。网站监听精确的办公室 IPv4 地址和本机回环地址，未监听其他接口。IP 变化后更新该字段并重新启动；旧 IP 的防火墙规则可按 `FilmAssetWorkbench-Office-*` 名称移除。

办公室共享使用可信局域网中的 HTTP 与 12 小时登录会话；Cookie 为 HttpOnly/SameSite Strict，密码采用带随机盐的 scrypt 哈希。若以后需要跨办公室或公网访问，应另行部署 HTTPS 和访问入口。当前无需云端 API 密钥。

## 三条生成路径

| 入口 | 输入与输出 |
|---|---|
| 补全优化图片 | 原图 + 提示词 → Qwen → 完整参考图 → 现有 2D 审核 |
| 单图生成模型 | 已有完整参考图 → Pixal3D → GLB |
| 补图并生模 | 原图 + 提示词 → Qwen → Pixal3D → 补图与 GLB |

现有工作台的“优化选中静帧”“生成选中资产”在本机配置 `enabled=true` 时也使用 ComfyUI。前者每个来源生成一张补全图，后者要求图片审核通过。提示词控制 Qwen 补图阶段，不是 Pixal3D 的文本输入。

物体资产页可批量上传最多 50 张 JPEG/PNG/WebP，单张最多 20MB、4000 万像素。图片内容实际解码验证。网页不接收客户端指定的工作流文件路径、执行代码或任意下载路径。

多视图配置已保留，但尚不开放提交：当前模板依赖 `pixal3d_multiview_int8_convrot.safetensors`，且使用针对示例四视图拼图的固定裁剪区域。下载模型后仍需核对前/左/后/右视图与裁剪区域，再启用；系统不会自动下载模型。单图生成多视图属于后续工作流。

## 工作流与任务架构

- `comfy.py`：读取保存的 UI JSON，展开子图和常量分支，仅保留输出节点依赖；上传图片、提交、查询及下载。按节点 ID 配置输入和输出，校验不通过时停止，避免静默改变工作流。
- `workflows/qwen.json`、`single.json`、`multiview.json`：从此次用户工作流保存的快照。原 ComfyUI 文件不修改。
- `comfy.local.json` 的 `profiles.<edit|model|full|multiview>.file` 可指定原始 JSON 的绝对路径，后续提交将读取该版本；`image`、`prompt`、`targets`、`outputs` 是对应节点映射。修改节点 ID 后须同步映射。
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
