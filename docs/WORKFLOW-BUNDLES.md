# 工作流包规范（v1.1）

## 使用步骤

主机管理员在“工作流库”导出现有配置作为模板。修改 `manifest.id`、`version`、输入/输出映射和 `workflow`，上传 JSON 工作流包。导入后默认停用，检查依赖后点击“检查并启用”。不安装自定义节点、不下载模型、不执行导入文件内的脚本。工作流本身的节点在任务执行时由 ComfyUI 运行。

用户只需选择已启用的工作流，上传图片并填写表单。局域网成员可查看和使用，但不能导入、导出或启停工作流。工作流包保存在本机 `data/local-assets/workflows.sqlite3`，随运行数据备份，不随源码上传。

同一 ID 可导入多个版本；版本不可覆盖，同时仅启用一个版本。停用只阻止新提交，已排队任务继续使用冻结的工作流和参数。依赖检查通过只说明节点与模型名称匹配，正式使用前仍应试运行验收结果。

## 示例：图片转存

下面是可运行的最小 API 格式示例，仅将输入保存为图片，无生成模型依赖。可用来理解配置；实际生成工作流将 `workflow` 换成 ComfyUI 导出的 API JSON，并修改对应映射。

```json
{
  "manifest": {
    "id": "image-copy",
    "version": "1.0.0",
    "label": "参考图转存",
    "description": "将上传图片归档，用于验证接口。",
    "inputs": [
      {"key": "reference", "label": "参考图片", "type": "image", "node": "1", "input": "image"}
    ],
    "parameters": [],
    "outputs": {"2": "image"}
  },
  "workflow": {
    "1": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
    "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "example"}}
  }
}
```

## 映射与边界

- 包顶层只有 `manifest` 与 `workflow` 两项必需。上限 5MB。普通 UI JSON 也支持，但仅限现有编译器支持的节点、常量分支和子图结构；含前端脚本、特殊动态节点的流程需要额外适配。推荐 API 格式。
- `inputs` 支持 1–8 个图片字段，字段 `key` 唯一且为小写字母、数字、下划线。`node` 为顶层节点 ID，`input` 为该节点的输入名称。所有图片输入必填。单图片输入可批量生成独立任务，多图片输入合为一项任务。
- `parameters` 支持 `text`、`integer`、`number`、`boolean`、`choice`，通过相同的 `node`/`input` 映射注入。可配置 `default`、`min`、`max`、`required`；`choice` 使用字符串数组 `choices`。声明 `required` 的参数应给默认值，以便做静态预检。`omit_empty: true` 可让空字符串沿用 JSON 中的原值。
- 输出节点使用 `outputs: {"节点ID": "image"或"model"}`，须包含 `filename_prefix` 输入。每个任务会覆盖保存前缀以隔离输出。暂支持 JPEG/PNG/WebP 和 GLB；一个节点输出多张图片或多个 GLB 时全部归档，并在任务中展示。
- 输入字段必须连接到声明的输出依赖，不能将未使用的节点暴露成看似有效的参数。输出需通过 ComfyUI history 返回包含 filename/subfolder/type 的文件描述；非标准输出结构需适配。
- 首张图片、首个模型兼容原资产审核流程；其他生成结果在任务卡片预览和下载。自动把图片组串接到下一阶段工作流尚未实现。
- 工作流执行地点仍由 `comfy.local.json` 的 `url` 统一决定；本版本没有实现多设备调度、远程执行程序或浏览器扫描用户本机工作流。

数值参数示例：

```json
{"key":"fov","label":"水平视场角（度）","type":"number","node":"324","input":"fov","default":20,"min":1,"max":170}
```

## 验证约定

导入检查包结构、映射、参数定义和版本冲突；启用与提交时连接实际 ComfyUI 做节点、必填参数和模型枚举检查。ComfyUI 最终运行校验或执行失败会写入任务状态。

升级 ComfyUI 后如缺失新参数，发布新的工作流包版本并验证。不要静默补齐未知节点参数，避免历史任务悄悄改变行为。
