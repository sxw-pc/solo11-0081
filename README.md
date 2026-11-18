# synthlab / 造数台

隔离业务素材增加 `synthlab/commerce_audit.py`：对已生成的商品、订单、订单行
核对原价、折扣后行额、订单合计与库存用量。运行
`python -m synthlab.commerce_audit --spec fixtures/demo/dataset.yaml` 会报告当前
独立列生成的实际不一致；此审计不修改数据或生成器。

`synthlab/campaign_audit.py` 对 `fixtures/campaign/trial.json` 的 80 个客户 / 40
个家庭做**有界样本合同裁决**。家庭是随机化单位；审计只读试验实际列
（`assigned` / `exposed` / `converted` / `outcome_observed`）与引擎生成的客户
身份，绝不调用 `synthlab/campaign_design.py` 的结构生成规则填补未观测转化
（该模块只在测试中提供单调触达与排他约束的独立证据）。两份事先授权合同不可
互换：

- `offer_rollout`：向所有家庭发邀请，按家庭随机指派的意向治疗差（ITT）裁决；
- `reached_only`：只为实际触达的增量客户付费，仅在单调触达、排他约束经设计
  契约核证且触达第一阶段为正时，用指派作工具变量计算 complier effect
  （ITT ÷ 触达率差）裁决。

缺失转化按 0 和 1 完整传播为区间：区间整体严格高于门槛 0.34 才批准，整体不
超过门槛则拒绝，跨越门槛为证据不足。直接比较 exposed/unexposed 的朴素关联只
作反例对照并明确标注为观察相关，不具因果含义。运行
`python -m synthlab.campaign_audit --trial fixtures/campaign/trial.json`
（加 `--json` 输出机器可读结果）。当前 fixture：ITT 区间 0.275–0.300、首阶段
0.725，故 `offer_rollout` 拒绝、`reached_only` 的 complier 区间
0.379–0.414 批准。本裁决只针对该合成试验，不声称真实人群效果或对外统计保证。

一个**纯本地、完全离线、声明式、确定性的多表合成数据生成与校验工作台**。

造数台读取一份 YAML 数据集蓝图，先做结构与引用完整性校验，再按依赖拓扑逐表生成数据，执行约束和边界覆盖检查，导出 SQLite / CSV / JSONL，并生成 manifest、数据说明书和独立校验报告。它适合开发、测试、演示和回归场景，也可以作为 Coding Agent 评测中的离线素材源。

```text
读取 YAML 蓝图
  → 结构 / 引用 / 唯一性 / 环依赖校验
  → 稳定拓扑排序
  → 派生种子逐表生成
  → 约束与边界覆盖检查
  → 导出 SQLite / CSV / JSONL
  → manifest + content_fingerprint
  → 独立重读校验
  → 127.0.0.1 只读预览
```

## 它是什么，不是什么

它是：

- 声明式、可复现的多表合成数据生成器；
- 以外键引用完整性为核心的拓扑生成器；
- 生成后独立复核的校验台；
- 完全离线的本地工程样例；
- 带只读预览的演示项目。

它不是：

- 真实数据脱敏、匿名化或合规迁移工具；
- 爬虫、RPA、抓取器或数据同步器；
- 通用 Faker 库、大模型内容生成器；
- 数仓、ETL 调度平台、报表系统或 CRUD 后台；
- 远程数据库连接器或版本化数据仓库。

所有 fixture、名称、标识、地址与文本词表均为虚构内容。运行期不发起网络请求。

## 环境

- Python 3.10+
- 运行期第三方依赖：`PyYAML`
- 测试依赖：`pytest`
- 预览服务：Python 标准库 `http.server`

```bash
python -m pip install -r requirements.txt
```

如果本机已经预装上述依赖，可完全断网运行。

## 30 秒演示

```bash
python -m synthlab demo
```

该命令会：

1. 读取 `fixtures/demo/dataset.yaml`；
2. 生成到 `out/demo`；
3. 导出 `sqlite`、`csv`、`jsonl`；
4. 写出 `manifest.json`；
5. 独立重读输出并写出 `verification.json` / `verification.md`；
6. 全部通过时退出码为 `0`，否则为 `1`。

成功输出包含内容指纹，例如：

```text
content_fingerprint: 453cc399fd804aefc0bc66d97cdad2e89c77a4296b0a55d115f39852f1867401
verification: pass
exit code: 0
```

## 常用命令

```bash
# 运行全部测试
pytest

# 从任意蓝图生成
python -m synthlab generate \
  --spec fixtures/demo/dataset.yaml \
  --out out/demo \
  --formats sqlite,csv,jsonl

# 独立校验输出目录
python -m synthlab verify --out out/demo

# 查看稳定拓扑执行计划
python -m synthlab plan --spec fixtures/demo/dataset.yaml

# 本地只读预览，仅绑定 127.0.0.1
python -m synthlab serve --out out/demo --port 8000
```

`verify` 全部检查通过时退出码为 `0`，存在失败时为 `1`。

## 输出布局

```text
out/demo/
  blueprint.yaml          # 蓝图副本，校验可独立定位输入
  manifest.json           # 元数据、行数、列统计、文件哈希、内容指纹
  data.sqlite3            # 主键 / NOT NULL / UNIQUE / 外键 / 索引
  csv/
    customers.csv
    orders.csv
    ...
  jsonl/
    customers.jsonl
    orders.jsonl
    ...
  verification.json
  verification.md
```

CSV 中的 `\N` 表示 NULL，空字符串保持为空字段。所有文本文件使用 UTF-8 和 LF 换行。

## 蓝图示例

```yaml
dataset: example
seed: 20251115
version: "1.0.0"
require_boundary_coverage: false
tables:
  - name: customers
    rows: 20
    primary_key: customer_id
    columns:
      - name: customer_id
        type: sequence
        nullable: false
        start: 1
        format: "CUST-{value:04d}"
      - name: email
        type: string
        nullable: false
        unique: true
        pattern: "user{seq:04d}@demo.invalid"
      - name: region_id
        type: foreign_key
        nullable: false
        references: regions.region_id
```

支持的列类型：

`int`、`float`、`decimal`、`string`、`text`、`date`、`datetime`、`bool`、`enum`、`uuid`、`sequence`、`foreign_key`。

支持的自引用外键必须可空，并只引用本表更早生成的行。表依赖使用稳定分层拓扑排序；同层按表名排序。

边界标记支持 `min`、`max`、`null`、`empty`（YAML 中裸写 `null` 会解析为空值，加载器会规范成 `null` 标记）。默认只报告覆盖率；在数据集中设置 `require_boundary_coverage: true` 后，未覆盖会失败。

## 确定性

- 全局 `seed` 与数据集名、表名、列名、行号、重试序号一起派生 `sha256` 子种子。
- 不使用全局随机状态。
- 行数、列顺序、行序、文件路径与排序均确定。
- 相同蓝图、seed 与版本重复生成时，除 `manifest.json` 的 `created_at` 和 `checked_at` 外，内容与 `content_fingerprint` 一致。
- `content_fingerprint` 基于蓝图 sha256、seed、表数据摘要与实际输出文件哈希，不包含时间戳。

SQLite 文件哈希可能随 SQLite 运行库版本变化；因此**跨 SQLite 版本**的内容指纹不承诺一致，同版本同输入承诺一致。

## 错误码

蓝图错误：

- `SPEC000` YAML / 根结构错误
- `SPEC001` 缺少必填字段
- `SPEC002` 未知列类型
- `SPEC003` 重复表名、列名或 YAML 键
- `SPEC004` 外键目标不存在、类型不匹配或引用目标不唯一
- `SPEC005` 表依赖存在环
- `SPEC006` 行数、null_rate 或数值范围非法
- `SPEC007` 枚举值或权重非法
- `SPEC008` 唯一约束冲突
- `SPEC009` 边界、检查表达式或生成参数非法
- `SPEC010` 非法标识符

生成与导出：

- `GEN001` 唯一约束重试耗尽或确定性值无法满足
- `GEN002` 生成器参数或格式错误
- `GEN003` 外键没有可用的父键
- `EXP001` 文件写入或 SQLite 导出失败
- `EXP002` SQLite 完整性检查失败
- `EXP003` 未知导出格式

校验：

- `VER001` 文件缺失或不可读
- `VER002` 文件大小 / sha256 不匹配
- `VER003` 行数不符
- `VER004` 主键重复或为空
- `VER005` 外键缺失
- `VER006` 非空、枚举、范围、长度或检查约束失败
- `VER007` 唯一约束失败
- `VER008` 内容指纹不一致
- `VER009` 必需的边界标记未覆盖
- `VER010` manifest 结构或元数据不合法
- `VER011` 表数据摘要不匹配
- `VER012` 蓝图副本缺失或哈希不匹配

约束检查内部使用对应的 `CST001`–`CST009` 定位问题，并在校验报告中映射为 `VER*`。

## 目录

```text
synthlab/             Python 包与 CLI
fixtures/demo/        可复现演示蓝图
fixtures/invalid/     稳定错误码样例
tests/                单元、集成、CLI、golden 测试
golden/               演示内容指纹基线
docs/                 SPEC / ARCHITECTURE / COMPLIANCE / TESTING
```

## 文档

- [规格说明](docs/SPEC.md)
- [架构说明](docs/ARCHITECTURE.md)
- [离线与合规](docs/COMPLIANCE.md)
- [测试指南](docs/TESTING.md)

## 已知边界

- `decimal` 以固定小数位字符串保存，利于确认性，不用于高精度金融计算。
- CSV 和 SQLite 中枚举值的通用类型还原有限；推荐枚举使用字符串值。
- 自引用外键只允许引用本表主键。
- 预览服务没有鉴权，只应绑定 loopback 使用；代码固定绑定 `127.0.0.1`。
- 本项目不提供真实数据脱敏能力，也不会读取外部数据源。
