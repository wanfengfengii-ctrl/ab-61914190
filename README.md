# Blend / Aliquot Genealogy Audit API

标准物质实验室的批次谱系复核服务：在签发混合与分装证书前，审计整条批次谱系。
合法谱系返回末端批次证书（含约分浓度分数与源料总量）；任何违规都以稳定原因码
整批拒绝，绝不产生部分结果。

纯 Python 标准库实现，镜像构建无需联网安装依赖。

## 运行

```bash
# 启动 API（宿主机端口默认 8080，可用 HOST_PORT 覆盖）
docker compose up --build api
HOST_PORT=9090 docker compose up --build api

curl -s http://localhost:${HOST_PORT:-8080}/health
# {"status":"ok"}
```

## 一次性验证（verify 服务）

`verify` 服务在干净构建的环境中依次执行：单元测试（代码测试）→ 对存活 API 的
冒烟测试（守恒、乱序、非法谱系），并以退出码报告结果（0 = 全部通过）：

```bash
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
echo "verify exit code: $?"
docker compose down
```

## API

### `POST /api/blends/audit`

请求体：

```json
{
  "sources": [
    {"id": "S1", "mass_ug": 1000, "analytes": {"A": 120, "B": 40}},
    {"id": "S2", "mass_ug": 2000, "analytes": {"A": 240, "B": 80}}
  ],
  "steps": [
    {"id": "st-01", "type": "merge", "inputs": ["S1", "S2"],
     "output":  {"id": "M1", "mass_ug": 3000, "analytes": {"A": 360, "B": 120}}},
    {"id": "st-02", "type": "split", "input": "M1",
     "outputs": [{"id": "P1", "mass_ug": 1500, "analytes": {"A": 180, "B": 60}},
                 {"id": "P2", "mass_ug": 1500, "analytes": {"A": 180, "B": 60}}]}
  ]
}
```

约束：

- 源批次 1–64 个，步骤 1–256 个，步骤可任意乱序；
- 每个批次（源与步骤产出）`mass_ug` 为正整数（微克），各分析物为非负整数（纳克）；
- `merge`：≥2 个输入，1 个产出；`split`：1 个输入，2–16 个产出；
- 每个产出批号只能由一步产生，且不得与源批号冲突；
- 每个批号最多被后续一步消耗一次；
- 谱系不得成环，不得引用未知批号；
- 合并逐项守恒（质量与每个分析物）；分装除总量守恒外，每个子批次的各分析物
  浓度（ng/µg）必须与母批次完全一致（按交叉相乘精确判定）；
- 步骤乱序不影响结果：合法谱系得到完全相同的证书，非法谱系得到相同原因码。

成功（`200`）：

```json
{
  "status": "ok",
  "terminal_batches": [
    {"id": "P1", "mass_ug": 1500, "analytes": {"A": 180, "B": 60},
     "concentrations": {"A": {"numerator": 3, "denominator": 25},
                        "B": {"numerator": 1, "denominator": 25}}}
  ],
  "source_totals": {"mass_ug": 3000, "analytes": {"A": 360, "B": 120}}
}
```

- `terminal_batches`：全部未被消耗的末端批次（按批号排序），`concentrations`
  为各分析物约分后的浓度分数（ng/µg，分子分母互质；0 表示为 `0/1`）；
- `source_totals`：整体源料总量（总质量与各分析物总量）。

失败（`400` 模式错误 / `422` 谱系规则违反）：

```json
{
  "status": "rejected",
  "reason_code": "RATIO_MISMATCH",
  "step_ids": ["st-02"],
  "batch_ids": ["M1", "P1"],
  "detail": "split step 'st-02': child 'P1' analyte 'A' concentration ..."
}
```

### 稳定原因码

| reason_code | 含义 | HTTP |
|---|---|---|
| `INVALID_SCHEMA` | 请求体结构/类型/取值范围不合法（含数量上限） | 400 |
| `DUPLICATE_STEP_ID` | 步骤 id 重复 | 422 |
| `DUPLICATE_SOURCE_ID` | 源批号重复 | 422 |
| `DUPLICATE_OUTPUT_ID` | 批号被多步产出或与源批号冲突 | 422 |
| `UNKNOWN_BATCH` | 步骤输入引用了未知批号（悬空引用） | 422 |
| `BATCH_RECONSUMED` | 批号被多于一步消耗（重复消耗） | 422 |
| `CYCLE_DETECTED` | 谱系成环 | 422 |
| `CONSERVATION_VIOLATION` | 合并/分装的质量或分析物数量不守恒 | 422 |
| `RATIO_MISMATCH` | 分装子批次与母批次分析物浓度比例不一致（比例漂移） | 422 |

失败响应只含错误信息，绝不含部分证书结果。

### `GET /health`

存活探针，返回 `200 {"status":"ok"}`（Dockerfile `HEALTHCHECK` 与 Compose
`healthcheck` 均使用它）。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `HOST_PORT` | 宿主机映射端口（compose） | `8080` |
| `PORT` / `HOST` | 容器内监听端口/地址 | `8080` / `0.0.0.0` |
| `API_BASE_URL` | 冒烟脚本目标地址（verify 服务） | `http://api:8080` |

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -v   # 单元测试
PORT=8080 python3 -m app.main              # 启动服务
API_BASE_URL=http://127.0.0.1:8080 python3 -m app.smoke   # 冒烟测试
```

## 结构

```
app/audit.py   核心谱系审计（图建模 + 拓扑排序，与步骤顺序无关）
app/main.py    HTTP API（标准库 http.server）
app/smoke.py   verify 服务执行的端到端冒烟测试
tests/         单元测试（审计逻辑 + HTTP 层）
Dockerfile     API 镜像（含 HEALTHCHECK）
docker-compose.yml  api（健康检查、可配置宿主机端口）+ verify（一次性验证）
```
