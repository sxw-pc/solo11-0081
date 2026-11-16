# synthlab 规格说明

## 1. 范围

synthlab 是离线、确定性、声明式的多表合成数据生成与校验工具。输入是一份 YAML 蓝图，输出是一组可独立复核的本地数据文件。

本规格覆盖：

1. 蓝图加载与静态校验；
2. 生成器注册表与基础类型；
3. 依赖规划与自引用；
4. 确定性生成；
5. 约束与边界覆盖；
6. SQLite / CSV / JSONL 导出与 manifest；
7. 独立校验器与报告；
8. CLI 演示与只读预览。

不覆盖真实数据脱敏、外部数据源连接、调度、增量同步、图形化建模和在线服务部署。

## 2. 蓝图模型

### 2.1 数据集级

```yaml
dataset: demo_shop          # 必填，非空字符串
seed: 20251115              # 必填，整数
version: "1.0.0"            # 必填
require_boundary_coverage: false  # 可选，默认 false
max_rows: 100000            # 可选，默认 100000
tables: [...]               # 必填，非空列表
```

单表行数必须满足 `0 <= rows <= max_rows`。默认上限为 100000。

### 2.2 表级

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `name` | 是 | 合法标识符，表名 |
| `rows` | 是 | 行数 |
| `primary_key` | 是 | 单列主键名 |
| `columns` | 是 | 非空列列表 |
| `unique_constraints` | 否 | 列组列表，支持复合唯一 |
| `checks` | 否 | 简单表级检查 |

`checks` 支持字符串和映射：

```yaml
checks:
  - "total_amount >= 0"
  - {column: quantity, op: ">=", value: 1}
```

比较运算支持 `>=`、`<=`、`!=`、`==`、`>`、`<`。NULL 行按 SQL 三值逻辑跳过表级检查；非空由 `nullable` 独立检查。

### 2.3 列级

| 字段 | 必填 | 默认 | 含义 |
| --- | --- | --- | --- |
| `name` | 是 | — | 合法标识符 |
| `type` | 是 | — | 12 种类型之一 |
| `nullable` | 否 | `true` | 主键始终按非空处理 |
| `null_rate` | 否 | `0.0` | 0–1；大于 0 要求 nullable |
| `unique` | 否 | `false` | 单列唯一 |
| `references` | 否 | — | `parent_table.parent_column` |
| `boundary` | 否 | `[]` | `min/max/null/empty` 子集 |
| 生成参数 | 否 | — | 按类型定义 |

YAML 裸写 `boundary: [null]` 时，PyYAML 会将 `null` 解析为 Python `None`；加载器把该值规范化为字符串标记 `null`。

### 2.4 类型与参数

| type | 参数 | 输出 |
| --- | --- | --- |
| `int` | `min=0`, `max=1000`, `step=1` | Python int |
| `float` | `min=0`, `max=100`, `precision=2` | Python float |
| `decimal` | `min=0`, `max=100`, `precision=2` | 固定小数位字符串 |
| `string` | `min_length=4`, `max_length=12`, `charset=alnum`, `prefix=""`, `pattern` | 字符串 |
| `text` | `min_words=2`, `max_words=6`, `words=[...]` | 短句字符串 |
| `date` | `start`, `end`, `step_days=1` | `YYYY-MM-DD` |
| `datetime` | `start`, `end`, `step_seconds=1` | UTC `...Z` |
| `bool` | `true_weight=0.5` | bool |
| `enum` | `values` 或 `choices` | 枚举原值 |
| `uuid` | 无 | 确定的 UUID v4 风格字符串 |
| `sequence` | `start=1`, `step=1`, `format="{value}"`, `pattern_start` | 格式化字符串 |
| `foreign_key` | `references` 必填 | 父键值 |

补充规则：

- `string.pattern` 支持 `{seq}`、`{seq:05d}`、`{row}`，`seq = pattern_start + row_index`。
- `string` 长度包含 `prefix`，且 prefix 不得长于 `max_length`。
- `text.words` 缺省使用代码内的本地虚构词表。
- `enum` 支持 `[a, b]`、`[{value, weight}]`、`{value: weight}`；权重必须为正。
- `sequence.format` 支持 `value`、`seq`、`row` 命名占位符。
- `uuid` 不使用系统随机源，不使用真实 UUID 库随机源，只从派生 RNG 产生 16 字节。
- 所有生成器只接受显式 RNG；不读全局随机状态。

### 2.5 外键分布声明（distribute）

`foreign_key` 列可声明 `distribute`，按父表某个离散属性的取值精确约束子表外键分布：

```yaml
- name: customer_id
  type: foreign_key
  nullable: false
  references: customers.customer_id
  distribute:
    by: region              # 父表上的离散属性列（必填）
    quotas: {east: 5, west: 2}      # 每个属性值分到的子行数；与 proportions 二选一
    # proportions: {east: 0.7, west: 0.3}   # 或者按比例（最大余数法取整）
    unlisted: forbid        # 未列出的父属性值：forbid（默认）禁止引用 / allow 吸收剩余行
    route: hash             # 同类多键时的分配路线：hash（默认，稳定哈希）/ capacity（按容量优先）
```

规则：

- `by` 必须是被引用父表上的离散列（`enum` / `string` / `bool` / `int` / `sequence` / `foreign_key`）；父属性与键的对应关系只取父表实际生成值。
- `quotas` 为非负整数映射；`unlisted: forbid` 时其和必须等于可分配行数，`allow` 时不得超过；`proportions` 为正数映射，和不超过 1，`forbid` 时必须恰好为 1。
- 可分配行数 = 子表行数 − 强制 NULL 行数（`boundary: [null]`）。声明 `distribute` 的列 `null_rate` 必须为 0；强制 NULL 只能通过 `boundary: [null]` 表达。
- 不支持自引用外键。
- 唯一约束容量计入可行性：单列唯一时每个父键至多出现一次；复合唯一时每键容量受同组其他列的可区分取值数限制。配额超过容量、属性值无父键、边界强制值超出配额或落入未列值，都判不可行。
- 可行性在子表生成前根据父表实际生成结果判定；不可行蓝图在导出前报 `GEN004`，结构非法在蓝图校验期报 `SPEC011`。
- 两条分配路线都满足精确配额：`hash` 按行号稳定哈希选键（容量冲突时确定性地顺移探测），`capacity` 按父表行序先用满前一个键的容量。同一路线在同一蓝图与 seed 下结果确定；路线选择写入 manifest 的 `distributions` 段，独立验证按声明核查业务分布，而不是把某一次具体键分配当作唯一合法答案。

## 3. 引用与唯一性

- `references` 只能出现在 `foreign_key` 列。
- 目标必须是父表主键、`unique: true` 列，或位于唯一约束中的列。
- 目标列必须存在。
- 自引用必须可空，且只允许指向本表主键。
- 自引用生成时只从更早生成的行中采样；第 0 行固定为 NULL。
- 唯一约束中的 NULL 不互相冲突；非 NULL 值必须唯一。
- 主键、单列 unique、复合 unique 都参与生成期去重。
- 去重失败的确定性重试上限为 50；耗尽后报 `GEN001`。

## 4. 依赖规划

生成边为 `child.column -> parent.column`。

- 非自引用父表必须先于子表。
- 采用稳定分层拓扑：每轮收集所有依赖已满足的表，按表名排序后整层输出。
- 自引用记录为独立标记，不构成环。
- 非自引用环报 `SPEC005`。
- `distribute` 声明在规划期做父属性依赖检查（属性必须存在于父表，否则 `SPEC011`），并作为独立边写入计划与可读输出。
- 可读计划包含表顺序、依赖边、自引用和分布边。

## 5. 确定性

派生种子：

```text
sha256("synthlab|<dataset_seed>|<dataset>|<table>|<column>|<row>|<attempt>")
取前 8 字节作为整数
```

因此：

- 同一蓝图、seed、版本、行号与重试序号产生相同值；
- 不为“唯一列重试”引入不可控熵；
- 表顺序、列顺序、行序、CSV/JSONL 字段顺序稳定；
- 空值率判定与取值共享同一派生 RNG，行为可复现；
- 时间戳不参与内容指纹。

## 6. 约束检查

生成后检查：

1. 主键非空且唯一；
2. 外键值存在于父表目标列；
3. 非空列无 NULL；
4. 枚举值在允许集合内；
5. 数值、日期、日期时间在声明范围内；
6. 字符串长度与文本词数在声明范围内；
7. 唯一约束无重复非 NULL 组合；
8. 表级 check 满足；
9. 边界覆盖按标记统计；
10. `distribute` 声明的每类子行数与外键归属（从数据独立重算）。

字符串/文本的空字符串视为显式 `empty` 边界，可豁免最小长度/词数检查；它不同于 NULL。

约束问题使用 `CST001`–`CST010` 定位表与列。verifier 将其映射为 `VER*`（`CST010` 映射为 `VER013`）。

## 7. 边界覆盖

支持标记：

- `min`：数值下界、日期起点、枚举首项、布尔 false、最小长度等；
- `max`：对应上界；
- `null`：NULL；
- `empty`：空字符串，仅 `string` / `text`。

引擎会在非唯一列上做确定性边界注入，优先使用靠前行；唯一列仅允许注入 `null`（若可空）。自然命中的边界也计入统计。行数不足或值不可表达时可能未覆盖。

默认只报告；`require_boundary_coverage: true` 时，任何未覆盖标记产生失败（生成期 `CST009`，校验期 `VER009`）。

## 8. 导出

### SQLite

- 单文件 `data.sqlite3`；
- 建表包含主键、NOT NULL、UNIQUE、外键和可用 CHECK；
- 每个外键列建索引；
- 写入后执行 `PRAGMA foreign_key_check` 和 `PRAGMA integrity_check`；
- decimal 存 TEXT，bool 存 INTEGER。

### CSV

- 每表 `csv/<table>.csv`，UTF-8、LF；
- 表头与蓝图列顺序一致；
- 行序与生成顺序一致；
- NULL 使用 `\N`，空字符串保留为空字段；
- 布尔输出 `true` / `false`。

### JSONL

- 每表 `jsonl/<table>.jsonl`，UTF-8、LF；
- 每行一个对象，字段顺序等于蓝图列顺序；
- JSON 原生 bool / null。

### 其他文件

- `blueprint.yaml`：输入蓝图原字节副本；
- `manifest.json`：机器可读清单；
- `verification.json` / `verification.md`：独立校验产物。

## 9. Manifest 与内容指纹

manifest 至少包含：

- 数据集名称、版本、seed；
- 蓝图文件路径与 sha256；
- 时间戳 `created_at`；
- 表清单、行数、列统计、表数据 sha256；
- `distributions`：每个 `distribute` 声明的父属性、路线、未列值策略、声明与解析后的配额（无声明时为空列表）；
- 边界覆盖；
- 输出文件相对路径、格式、字节数、sha256；
- `content_fingerprint`。

指纹输入：

```text
蓝图 sha256
+ 数据集名称 / seed / version
+ 每表 name / columns / row_count / data_sha256
+ 每输出 path / bytes / sha256
```

输出按 path 排序，表按 name 排序；`created_at`、`checked_at` 不参与。

同版本、同 SQLite 运行库、同输入时，重复生成除时间戳外完全一致。SQLite 二进制可能随运行库版本变化。

## 10. 独立校验

verifier 不读取生成过程内存状态，只读取：

- 输出目录中的 manifest；
- 蓝图副本（或显式 `--spec`）；
- 已导出数据文件。

检查项：

- manifest 结构与元数据；
- 输出文件存在、字节数、sha256；
- 蓝图副本 sha 与校验；
- 每表行数与数据 sha；
- 主键、外键、非空、枚举、范围、长度、唯一、表检查；
- `distribute` 分布（manifest 声明与蓝图一致性、按数据重算的每类行数）；
- 边界覆盖；
- 内容指纹重算。

报告：

- `verification.json`：状态、检查列表、错误码、定位、摘要、边界覆盖；
- `verification.md`：表格化人读摘要；
- 全部通过进程退出码 0，否则 1。

## 11. CLI 合同

| 命令 | 行为 |
| --- | --- |
| `python -m synthlab demo` | 生成 fixture 到 `out/demo` 并校验 |
| `generate --spec --out [--formats]` | 生成并导出 |
| `verify --out [--spec]` | 独立校验并写报告 |
| `plan --spec` | 打印执行计划 |
| `serve --out [--port]` | 启动只读预览，固定绑定 `127.0.0.1` |

蓝图、生成、导出或校验的业务失败使用非零退出码并打印稳定错误码；不输出无上下文堆栈。

## 12. 限制

- 主键为单列。
- 预览服务无鉴权，仅用于 loopback。
- 不读取外部数据源。
- 数值随机分布为均匀分布；枚举按权重采样。
- decimal 是文本定点值，不提供高精度算术保证。
- 枚举的跨 CSV/SQLite 原类型还原未承诺，建议使用字符串枚举。
