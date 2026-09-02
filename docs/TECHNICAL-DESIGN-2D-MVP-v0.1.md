# 2D 批处理 MVP 技术方案

> 版本：v0.1  
> 日期：2026-08-17  
> 开发方式：本地代码开发  
> 当前范围：电影静帧批量生成干净背景资产图

## 1. 当前交付边界

第一阶段只实现：

```text
本地输入文件夹
  → 图片校验与任务登记
  → 本地图片编码为 Base64 Data URL
  → 调用方舟 Seedream 5.0 lite Images API
  → 解码一张或多张生成结果
  → 本地输出文件夹
  → CSV/JSON 映射清单
```

暂不实现混元 3D、正式多人审核、电影数据库网站集成和像素级抠图。

## 2. 接入模式

本项目使用火山方舟 `Doubao-Seedream-5.0-lite`，而不是旧的即梦 4.0 Visual OpenAPI。

方舟 Images API 支持 URL 或 Base64 图片输入。第一版直接把本地图片编码为 Base64 Data URL，因此不需要 TOS Bucket，也不会产生对象存储中转费用。

默认使用“图生图－单张图生成一组图”，用于从一张静帧生成最多 3 张独立核心资产图；返工时可以切换为“图生图－单张图生成单张图”。

## 3. 技术选型

| 模块 | 方案 |
|---|---|
| 运行时 | Python 3.11+ |
| 本地状态 | SQLite |
| 图片输入 | 本地图片 Base64 Data URL |
| 模型调用 | 火山方舟 REST Images API + Bearer API Key |
| 模型 | `doubao-seedream-5-0-lite-260128` |
| 批处理 | 线程池，并发数可配置 |
| 结果清单 | CSV + JSON |
| 自动化测试 | Python `unittest`，模拟 API，不产生费用 |

## 4. Seedream 5.0 lite 接口规格

### 4.1 请求

- Endpoint：`https://ark.cn-beijing.volces.com/api/v3/images/generations`
- 鉴权：`Authorization: Bearer $ARK_API_KEY`
- Model：`doubao-seedream-5-0-lite-260128`

主要请求字段：

- `image`：当前静帧的 Base64 Data URL 数组。
- `prompt`：资产识别、结构补全和干净背景要求。
- `size`：默认 `2K`。
- `sequential_image_generation`：组图为 `auto`，单图为 `disabled`。
- `sequential_image_generation_options.max_images`：默认 3。
- `response_format`：默认 `b64_json`，结果直接落本地。
- `watermark`：默认 `false`。

### 4.2 响应

- 接口同步返回 `data` 数组。
- `b64_json` 存在时直接 Base64 解码保存。
- 如配置成 `url`，则立即下载结果，避免临时链接过期。
- HTTP 429 与 5xx 按退避策略自动重试；鉴权、参数和内容审核错误不盲目重试。

## 5. 断点续跑

SQLite 保存每个源文件的 SHA-256、任务状态、请求 ID、错误信息和输出文件。程序重启后：

- 已完成任务不重复生成。
- 可重试失败任务可由命令重置后再次执行。
- 源文件内容变化时会登记为新任务。

方舟 Images API 是同步请求；请求意外中断时，服务端可能已经计费但客户端未收到结果。系统不会自动无限重发，仅按有限次数处理明确的限流和服务端错误。

## 6. 输出命名

```text
<原文件名>__<任务短ID>__asset-01.png
<原文件名>__<任务短ID>__asset-02.png
```

一张静帧返回多张结果时，每张结果独立编号。`manifest.csv` 和 `manifest.json` 保存原图与所有结果图的映射。

## 7. 凭证安全

- 方舟 API Key 仅从 `.env` 或系统环境变量读取。
- `.env` 已加入 `.gitignore`。
- 日志、数据库和清单不记录 API Key。
- 任何曾出现在聊天、截图或代码中的 Key 都应立即撤销并重新创建。

## 8. POC 进入条件

真实 POC 需要：

1. 已开通 `Doubao-Seedream-5.0-lite`。
2. 一枚未公开泄露的方舟 API Key。
3. 20～50 张具有代表性的电影静帧。

在没有凭证时，`--mock` 模式可完整验证文件扫描、状态库、并发、结果命名和清单导出，但不会验证即梦生成质量。
