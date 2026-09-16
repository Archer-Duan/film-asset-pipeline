# Changelog

All notable changes follow semantic versioning.

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
