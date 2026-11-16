# synthlab 架构

## 1. 分层

```text
CLI (cli.py, __main__.py)
  │
  ├── spec/loader.py ── spec/validator.py ── spec/rules.py
  │        │
  │        └── spec/model.py
  │
  ├── planner.py
  ├── engine.py ── generators/{registry,primitives,common,boundary}.py
  ├── distribution.py
  ├── constraints.py
  ├── exporters.py ── sqlite_export.py
  ├── manifest.py
  ├── verifier.py ── readers.py ── report.py
  └── preview.py
```

依赖方向保持单向：生成器不知道导出器，导出器不知道校验器，校验器不调用生成引擎。

## 2. 数据流

```text
YAML bytes
  → Blueprint(raw data, path, sha256)
  → validate_blueprint()
  → DatasetSpec
  → ExecutionPlan
  → DatasetData (TableData rows + column stats)
  → ConstraintReport
  → OutputFile[] + manifest
  → verifier reads files back independently
```

### 2.1 蓝图层

- `loader.py` 只负责安全读取、重复键检测、YAML 解析、模型转换。
- `validator.py` 负责结构、引用、唯一约束、参数与环检测。
- `rules.py` 隔离参数级规则，保持 validator 可读。
- `model.py` 使用 dataclass 表达蓝图与物化数据；不包含 IO。

### 2.2 生成层

- `planner.py` 构建依赖图，进行稳定分层拓扑排序，自引用独立标记，`distribute` 父属性依赖检查。
- `engine.py` 负责行循环、派生 RNG、空值率、唯一重试、边界注入和统计。
- `distribution.py` 在子表生成前把 `distribute` 声明解析为确定性的行→父键计划：从父表生成值建立属性→键映射，核对唯一容量、强制 NULL 与边界强制值后给出可行/不可行结论（`GEN004`），并按 `hash` / `capacity` 路线分配。
- `generators/registry.py` 按类型分发。
- `generators/primitives.py` 实现 12 种生成器；每个生成器可独立构造和单测。
- `generators/boundary.py` 提供纯函数边界候选与命中判断，生成和约束检查共用其语义，但不共享运行状态。
- `common.py` 提供种子派生、模板、枚举权重、日期和定点小数辅助。

### 2.3 检查与导出层

- `constraints.py` 只接收 `DatasetSpec` 和 `TableData`，重新计算主键、外键、非空、枚举、范围、长度、唯一和表检查。
- `exporters.py` 负责 CSV / JSONL、文件记录和格式校验。
- `sqlite_export.py` 负责带约束的 SQLite schema、索引和完整性查询。
- `manifest.py` 负责 canonical JSON、表摘要、输出摘要和内容指纹。

### 2.4 校验与预览层

- `readers.py` 从 JSONL / CSV / SQLite 重建 `TableData`；优先 JSONL，其次 CSV，最后 SQLite。
- `verifier.py` 重读 manifest、蓝图和输出文件，执行文件哈希、行数、数据摘要、约束、边界与指纹检查。
- `report.py` 将结构化结果渲染为 JSON + Markdown。
- `preview.py` 使用标准库 `ThreadingHTTPServer`，只绑定 `127.0.0.1`，只允许 GET/HEAD。

## 3. 确定性种子

```python
sha256("synthlab|" + seed + "|" + dataset + "|" + table + "|" + column + "|" + row + "|" + attempt)
```

取摘要前 8 字节为大整数，构造独立 `random.Random`。

设计取舍：

- 每次单元格尝试单独派生，避免一个列的重试扰动其它列；
- 同一行的不同列互不影响，便于并行扩展；
- 不用全局 `random`；
- 时间、进程、主机名和绝对路径不进入数据；
- 输出文件排序固定。

## 4. 生成循环

对每个表：

1. 按规划顺序取表；
2. 构造边界注入计划（非唯一列，或唯一列上的可空 NULL）；
3. 对每行：
   - 逐列派生 RNG；
   - 应用强制边界值；
   - 自引用第 0 行强制 NULL；
   - 按 null_rate 判定 NULL；
   - 调用生成器；
   - 检查所有唯一组；
   - 冲突时 attempt + 1，最多 50 次；
   - 成功后将行追加到表数据，并追加各列父键池；
4. 计算列统计；
5. 全部表完成后计算边界覆盖。

父键池按列保存，因此外键可以引用父表主键或唯一列。

## 5. SQLite 设计

- 先建表，再插入，再建索引；
- 使用单事务；
- `PRAGMA foreign_keys=ON`；
- 插入后运行 `foreign_key_check` 与 `integrity_check`；
- 主键、单列 unique、复合 unique、NOT NULL、外键与表 check 写入 schema；
- 所有标识符加双引号并转义。

## 6. 内容指纹

canonical JSON：UTF-8、键排序、无多余空格、`allow_nan=False`。

指纹输入：

- 蓝图 sha256；
- 数据集名称、seed、version；
- 每表名称、列顺序、行数、表数据 sha256；
- 每个输出文件 path、bytes、sha256。

时间戳、manifest 自身文件哈希和绝对输出路径不进入指纹（path 使用相对路径）。

## 7. 独立校验

verifier 与 engine 的独立性边界：

- 不读取 engine 内存对象；
- 不信任 manifest 声称的行数或哈希；
- 重新计算所有可计算项；
- 数据从导出文件重建；
- 校验失败仍写出结构化报告；
- 输出文件存在哈希错误时，允许继续做语义检查，以同时暴露多个问题。

## 8. 安全边界

- 预览只绑定 loopback；
- 只读 HTTP 方法；
- 原始文件下载只允许 manifest 白名单；
- 路径解析后必须仍位于输出根目录；
- 无模板引擎、无 CDN、无外部字体、无前端依赖；
- 所有 HTML 动态值经过 `html.escape`。

## 9. 可维护性

- 单文件不超过 300 行；
- 每个模块只承担一个边界的职责；
- 错误码集中可查；
- 生成器注册表易于追加新类型；
- 公开 API 集中在包根与各层模块，便于测试。

## 10. 扩展点

新增列类型：

1. 在 `spec/model.py` 的 `SUPPORTED_TYPES` 增加类型；
2. 在 `spec/rules.py` 增加参数校验；
3. 在 `generators/primitives.py` 实现生成器；
4. 注册到 `GENERATOR_CLASSES`；
5. 在边界模块和约束模块补语义（如适用）；
6. 增加生成、导出、校验测试。

新增导出格式：实现 writer、扩展 `FORMATS`、保证 manifest 输出记录和 reader 回读路径。
