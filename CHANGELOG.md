# Changelog

All notable changes follow semantic versioning.

## [1.2.0] - 2026-09-20 — 统一资产库与按日交付

- 生成与资产库共享导航、深绿配色及右上角账号入口；工作流设置顶部对齐，移除重复的打开资产库按钮。
- 原始上传、处理图片、模型和任务双向关联；增加图片生成中与生成失败分类，修复重复图片路径和多模型输出的入库遗漏。
- 本地生成按北京时间提交日期归入 `YYYY-MM-DD_2d` / `YYYY-MM-DD_3d`，包含工作流中间保存节点；文件名包含资产名称、生成方式、时间与任务短号。
- 新增可选资产名称和 CLI `--name`；资产库继续生成时沿用人工命名。
- 支持选中模型/图片批量 ZIP 下载，包含可读文件名与资产清单；单文件下载使用资产名称。
- 提供可重复执行的历史输出整理工具，校验文件内容、保留数据库备份及原本地缓存，不改变审核 ID。
- 修复本机切换成员后仍显示管理员、全部资产计数遗漏原图以及刷新时重复重绘缩略图的问题。
- 新增目录迁移、数据关联、多个模型、下载和登录回归；浏览器检查布局、关联跳转、缩略图和真实 ZIP 下载。

## [1.1.0] - 2026-09-20 — 多视图与可扩展工作流库

- 开放前、左、后、右四图生成模型，替换示例拼图固定裁剪，支持视场角参数。
- 新增声明式工作流库、版本导入/导出、依赖检查和管理员启停；支持 UI JSON 与 API JSON。
- 图片输入及参数由工作流定义生成，任务冻结工作流版本、输入映射和参数，支持多张图片/多个模型结果。
- 更新物体资产界面，提供创建资产、任务筛选和工作流管理；适配桌面与手机宽度。
- 保留旧队列、审核与 CLI，扩展多图命令行；补齐 ComfyUI 0.36 新增的 MoGe refine_steps 参数。

## [1.0.0] - 2026-09-16 — 本地 ComfyUI 与办公室共享

- 新增物体资产入口、本地 Qwen 补图、Pixal3D 生模、GLB 交互预览。
- Web 与 CLI 共用持久化单 GPU 队列，支持断线恢复和提交结果对账。
- 新增办公室成员登录、任务归属、局域网启动与按网段限制的防火墙脚本。
- 现有审核、标签、来源关联和云端 CLI 兼容保留；多视图预留但未开放提交。

## [0.5.0] - 2026-08-19

- Added a first-run local model configuration wizard.
- Added operating-system credential-store support with a protected local fallback.
- Added separate Tencent Cloud SDK and OpenAI-compatible Hunyuan credential modes.
- Added per-user runtime data directories for new installations while preserving existing project-local workspaces.
- Added a Windows installer, desktop launcher, CI workflow, and open-source project policies.
- Unified the package, API, and frontend version at 0.5.0.

## [0.4.3] - 2026-08-19

- Added selectable one-image or three-view 2D generation.
- Improved batch review, progress, sorting, list view, and thumbnail controls.
