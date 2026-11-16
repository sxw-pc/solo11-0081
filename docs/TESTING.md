# 测试指南

## 1. 环境

- Python 3.10+
- `PyYAML>=6.0`
- `pytest>=7.0`

安装后，在项目根目录运行一条命令：

```bash
python -m pytest
```

或使用较简洁的输出：

```bash
python -m pytest -q
```

当前基线：**122 passed**，本机约 6 秒完成。测试不需要网络。

## 2. 测试分层

| 文件 | 关注点 |
| --- | --- |
| `test_spec_loader.py` | YAML 读取、SHA 记录、重复键拒绝、模型规范化、错误码 |
| `test_spec_validator.py` | 必填字段、类型、引用、唯一约束、自引用、环、聚合错误 |
| `test_generators.py` | 12 类生成器、种子隔离、范围、精度、边界候选 |
| `test_planner.py` | 稳定分层拓扑、依赖边、自引用、环检测 |
| `test_engine.py` | 确定性、行/列顺序、外键、自引用、唯一重试、统计 |
| `test_constraints.py` | 主键、外键、非空、枚举、范围、长度、唯一、表检查、边界 |
| `test_exporters.py` | 三种格式、CSV NULL 标记、JSONL 键顺序、SQLite schema、重复导出 |
| `test_manifest.py` | manifest 字段、时间戳独立性、摘要变化、往返读写 |
| `test_verifier.py` | 独立复读、文件缺失、篡改文件、篡改数据、蓝图哈希、报告 |
| `test_cli.py` | demo / generate / plan / verify、退出码、只读 preview、重复生成 |
| `test_golden.py` | 演示蓝图的内容指纹和表数据摘要基线 |
| `test_distribution.py` | distribute 声明、可行性、两条分配路线、逐格式篡改检测 |

共享 fixture 在 `tests/conftest.py`：

- `demo_spec`：加载并校验演示蓝图；
- `demo_data`：生成一次演示数据，会话级复用；
- `demo_output`：把演示数据导出到临时目录并写 manifest；
- `write_yaml`：按测试内容写临时 YAML。

这些 fixture 使用 pytest 临时目录，不修改仓库中的 `out/`。

## 3. 覆盖的关键失败路径

- YAML 语法错误、根节点非映射、重复键；
- 缺少必填字段、未知类型、非法标识符；
- 引用不存在、引用非唯一列、自引用不可空、依赖环；
- 唯一重试耗尽；
- 非空列出现 NULL、主键重复、外键悬空；
- 枚举越界、数值/日期越界、长度或词数越界；
- 表级检查失败；
- 必需边界未覆盖；
- manifest 缺失或损坏、输出文件缺失、输出哈希不匹配；
- 数据内容被篡改但哈希同步问题；
- 蓝图副本被篡改；
- 只读预览拒绝 POST，并保持本地绑定。

失败路径既检查错误码，也检查退出码和报告写入行为。

## 4. 演示与手工验证

```bash
python -m synthlab demo
python -m synthlab verify --out out/demo
python -m synthlab plan --spec fixtures/demo/dataset.yaml
```

预期：

- demo 退出码 0；
- 内容指纹为：
  `e4caa7a6ec7b35cf9cc9e795dca219142d9501cf99fe61033241dc9387ede823`；
- verifier 输出 `41 passed, 0 failed`；
- `out/demo/verification.json` 的 `status` 为 `pass`。

预览手工检查：

```bash
python -m synthlab serve --out out/demo --port 8000
```

浏览器访问 `http://127.0.0.1:8000/`。预期能看到表卡片、manifest、校验报告，以及清单内 CSV/JSONL/蓝图的原始内容。页面不应引用外部网络资源。

只读检查可用任意 HTTP 客户端向 `/` 发送 POST，预期返回 405。测试中使用 loopback HTTP 客户端覆盖该行为。

## 5. 篡改检测演示

先复制演示输出，再修改副本中的任意数据文件：

```bash
cp -r out/demo out/demo-tampered
printf '{}\n' > out/demo-tampered/jsonl/regions.jsonl
python -m synthlab verify --out out/demo-tampered
echo $?
```

预期：

- 进程退出码 1；
- 报告出现 `VER002`（文件 sha256/大小不匹配）；
- 可能出现 `VER003` / `VER011` / `VER008` 等派生失败；
- 原 `out/demo` 不受影响。

完成后可删除该临时副本。

## 6. 确定性与 golden

`tests/test_golden.py` 校验三件事：

1. 演示蓝图 sha256；
2. 整体 `content_fingerprint`；
3. 每张表的 `table_data_sha256`。

golden 文件位于 `golden/demo_fingerprint.json`。它用于发现非预期输出漂移。只有当蓝图、fixture 或确定性算法发生**有意变更**时，才应更新 golden：先审查内容差异，再重新计算基线并提交变更说明。

测试中构建 manifest 时固定 `created_at`，以证明时间戳不参与指纹。CLI 测试则比较两次真实生成的 manifest，忽略时间戳差异。

## 7. 环境注意事项

- 若全局 Python 环境自动加载 `pytest-asyncio`，可能看到未设置 fixture loop scope 的弃用警告；这是外部插件提示，不影响本项目测试结果。
- SQLite 二进制输出可能随 SQLite 运行库版本变化；同一环境内的字节重复性有测试覆盖。
- Windows 上预览测试使用真实 loopback 套接字；预览处理器会读取并丢弃写请求的 body，避免连接复用时的半包/重置竞态。
- 测试不访问外网。若在网络受限环境运行，仍应全部通过。
- 测试默认在 `tests/` 下发现用例；`pyproject.toml` 已配置 `testpaths = ["tests"]`。

## 8. 不覆盖的内容

- 真实数据脱敏效果；
- 大表（接近 100000 行）的性能基准；
- 浏览器兼容性和视觉效果；
- 恶意输入下的资源耗尽防护；
- TLS、鉴权、反向代理和公网部署；
- 跨 SQLite 版本字节级一致性。

这些属于非目标或需在部署环境中单独验证。
