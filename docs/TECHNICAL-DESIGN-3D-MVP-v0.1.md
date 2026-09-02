# 3D 批处理 MVP 技术方案

版本：v0.1  
日期：2026-08-17

## 1. 当前交付边界

本阶段把 2D 审核通过的资产图批量提交给腾讯混元生 3D，下载 GLB 到本地，并记录图片与模型的映射关系。

暂不包含：网页审核界面、人工修模、自动拓扑二次处理、PBR 贴图拆包检查、资产网站入库。

## 2. 接入选择

项目同时支持两条互不混用的接入路径。当前默认使用腾讯云 API 3.0，并通过官方 Python SDK Common Client 完成签名；原 OpenAI 兼容接口保留用于兼容和诊断。此前 `sk-...` 路径的图生3D请求返回 `Code 1001 / Invalid param`，没有创建任务，因此 POC 转向参数和示例更完整的 API 3.0 路径。

| 项目 | 选择 |
|---|---|
| 接入方式 | 腾讯云 API 3.0 / Python SDK Common Client |
| 接口域名 | `ai3d.tencentcloudapi.com` |
| API 版本 | `2025-05-13` |
| 地域 | `ap-guangzhou` |
| 提交 Action | `SubmitHunyuanTo3DProJob` |
| 查询 Action | `QueryHunyuanTo3DProJob` |
| 鉴权 | `TENCENTCLOUD_SECRET_ID` + `TENCENTCLOUD_SECRET_KEY` |
| 模型 | `3.1` |
| 输入 | 顶层 `ImageBase64`，只含原始 Base64 数据 |
| 类型 | `Normal` |
| PBR | 开启 |
| 目标面数 | 1,000,000 |
| 交付格式 | GLB |
| 默认并发 | 1 |

专业版 3.1 相比 3.0 提升了几何与纹理质量，并支持更多视角输入。单图 MVP 先使用 3.1；后续可升级为多视图生成。

## 3. 输入门禁

官方建议输入图满足：

- 纯色、简洁背景；
- 只有一个主体；
- 无文字和渐变背景；
- 主体占画面 50% 以上；
- 分辨率 128×128 至 5000×5000；
- 实际文件建议不超过 8MB。

当前 Seedream 输出的 2048×2048 单资产图符合此门禁。

## 4. 请求与结果

SDK 提交请求使用文档规定的 PascalCase 参数；`ImageBase64` 不包含 Data URL 前缀：

```json
{
  "Model": "3.1",
  "ImageBase64": "/9j/4AAQ...",
  "EnablePBR": true,
  "FaceCount": 1000000,
  "GenerateType": "Normal"
}
```

提交后立即保存远端任务 `JobId`，查询时提交 `{ "JobId": "..." }`。`WAIT/RUN` 时继续轮询，`DONE` 后从 `ResultFile3Ds` 选择 GLB 下载地址并立即保存；`FAIL` 时记录 `ErrorCode/ErrorMessage`。`JobId` 有效期为24小时，应及时完成查询与下载。

不设置 `result_format`：专业版默认结果包含 OBJ 和 GLB，而该字段的可选值主要是 STL、USDZ、FBX。

## 5. 断点续跑与计费保护

3D 是异步、计费任务，必须先保存远端任务 ID，再轮询和下载。

- `pending`：尚未提交；
- `submitting`：请求已发出但还未记录远端 ID；
- `submitted/running`：已有远端 ID，可安全恢复查询；
- `complete`：GLB 已下载；
- `failed`：远端失败或参数错误。

`submitting` 阶段如果程序中断，不能自动重发，因为原请求可能已经创建任务并计费。系统会标记失败，要求先去控制台核对再手动 `retry-3d`。

轮询超时和下载失败不会重新提交任务，而是保留原远端 ID，下次继续查询或下载。

## 6. 文件映射

输入来自 `data/output/manifest.json` 中状态为 `complete` 的真实 2D 输出，不读取 `_mock`。

输出：

```text
data/models/
  <2D文件名>__<任务短ID>.glb
  <2D文件名>__<任务短ID>__preview.jpg
  manifest-3d.csv
  manifest-3d.json
```

清单保存：2D 路径与哈希、模型版本、PBR、目标面数、远端任务 ID、GLB 路径、预览图路径、积分消耗、错误信息和时间戳。

## 7. 成本

原平台专业版按功能计积分。`Normal` 生成当前为 25 积分；PBR 和自定义面数各增加 10 积分。因此最小参数专业版测试约为 25 积分（约 3 元），正式的 PBR + 100万面配置约为 45 积分（约 5.4 元）。实际扣费以腾讯云控制台账单和最新价格页为准；没有返回 `JobId` 的参数校验失败通常不会创建生成任务。

## 8. 真实 POC 进入条件

1. 确认混元生 3D 专业版服务可用，并领取或购买积分；
2. 创建腾讯云 API 3.0 密钥对，并为子账号授予所需的混元生3D权限；
3. 将 SecretId/SecretKey 只写入本地 `.env`；
4. 先执行 `run-3d --mock`；
5. 首次真实 POC 只生成 1 个模型，人工检查 GLB、PBR 和几何后再批量。

## 9. 官方依据

- [混元 OpenAI 兼容接口调用示例](https://cloud.tencent.com/document/product/1804/126189)
- [混元生 3D 快速入门（API 3.0/SDK）](https://cloud.tencent.com/document/product/1804/120757)
- [混元生 3D 专业版参数](https://cloud.tencent.com/document/product/1804/123447)
- [混元生 3D 专业版查询接口](https://cloud.tencent.com/document/product/1804/123448)
- [混元生 3D 计费概述](https://cloud.tencent.com/document/product/1804/123461)
