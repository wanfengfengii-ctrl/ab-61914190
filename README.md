# 标准物质混合 / 分装批次谱系审计服务

签发混合与分装证书前，对整条批次谱系进行复核：防止批号被重复消耗、成分在分装中凭空变化、无来源批次混入成品。全部校验通过后签发确定性末端证书；任何一项违规都整批拒绝，绝不产生部分结果。

服务仅依赖 Python 3.12 标准库，无需联网安装依赖即可构建运行。

## API

### `POST /api/blends/audit`

请求体（JSON）：

```json
{
  "sources": [
    {"id": "A", "mass_ug": 1000, "analytes": {"Cu": 30, "Zn": 10}},
    {"id": "B", "mass_ug": 2000, "analytes": {"Cu": 60, "Zn": 20}}
  ],
  "steps": [
    {"id": "m1", "type": "merge", "inputs": ["A", "B"],
     "output":  {"id": "M", "mass_ug": 3000, "analytes": {"Cu": 90, "Zn": 30}}},
    {"id": "s1", "type": "split", "input": "M",
     "outputs": [{"id": "P1", "mass_ug": 1200, "analytes": {"Cu": 36, "Zn": 12}},
                 {"id": "P2", "mass_ug": 1800, "analytes": {"Cu": 54, "Zn": 18}}]}
  ]
}
```

约束：

| 项 | 约束 |
| --- | --- |
| `sources` | 1..64 个源批次 |
| `steps` | 1..256 个步骤，可任意乱序 |
| 批次 `id` / 步骤 `id` | 1..128 字符字符串 |
| `mass_ug` | 正整数（微克），≤ 10^18 |
| `analytes` | 分析物名 → 非负整数（纳克），≤ 10^18；可缺省为 `{}` |
| `merge` | `inputs` ≥ 2 个批号，`output` 为单个批次 |
| `split` | `input` 单个批号，`outputs` 为 2..16 个子批次 |

审计规则（按此顺序检查，违规即整批拒绝）：

1. 每个产出批号只能由一个步骤产生（且不得与源批号重复）；
2. 每个批号最多被一个后续步骤消耗一次；
3. 谱系不得成环，不得引用未知批号；
4. 合并须逐项守恒（质量与每个分析物量分别等于输入之和）；
5. 分装须保持质量与分析物总量守恒，且每个子批次与母批次的分析物比例完全一致（按整数交叉相乘精确判定，无浮点误差）。

### 成功响应 `200`

```json
{
  "status": "ok",
  "terminal_batches": [
    {
      "id": "P1",
      "mass_ug": 1200,
      "analytes": {"Cu": 36, "Zn": 12},
      "concentrations_ng_per_ug": {
        "Cu": {"numerator": 3, "denominator": 100},
        "Zn": {"numerator": 1, "denominator": 100}
      }
    }
  ],
  "source_totals": {"mass_ug": 3000, "analytes": {"Cu": 90, "Zn": 30}}
}
```

* `terminal_batches`：全部未被消耗的末端批次，按批号排序；`analytes` 覆盖请求中出现过的全部分析物（缺省补 0）。
* `concentrations_ng_per_ug`：各分析物浓度（纳克/微克）的约分分数；0 约分为 `0/1`。
* `source_totals`：整体源料总量（质量与逐项分析物量）。
* 无论步骤与源批次如何排列，合法谱系都得到字节级一致的证书。

### 失败响应 `400` / `422`

```json
{
  "status": "error",
  "error": {
    "code": "RATIO_MISMATCH",
    "message": "every split output must keep the parent batch analyte ratios exactly",
    "step_ids": ["s1"],
    "batch_ids": ["M", "P1", "P2"]
  }
}
```

结构问题（JSON 非法、字段缺失/越界）返回 `400`；谱系规则违规返回 `422`。响应中不含任何部分证书。

稳定原因码：

| code | 含义 |
| --- | --- |
| `INVALID_SCHEMA` | 请求结构或取值非法 |
| `DUPLICATE_STEP` | 步骤 id 重复 |
| `DUPLICATE_BATCH` | 批号被重复产生（或与源批号冲突） |
| `UNKNOWN_BATCH` | 悬空引用：步骤引用了从未产生的批号 |
| `DUPLICATE_CONSUMPTION` | 批号被重复消耗 |
| `CYCLE` | 谱系成环 |
| `MERGE_NOT_CONSERVED` | 合并不守恒 |
| `SPLIT_NOT_CONSERVED` | 分装总量不守恒（含子批次出现母批次没有的分析物） |
| `RATIO_MISMATCH` | 分装比例漂移 |

### `GET /health`

返回 `200 {"status": "ok"}`，供 Docker 健康检查使用。

## 运行

```bash
# 构建并启动 API（宿主机端口默认 8080，可用 API_PORT 覆盖）
docker compose up --build
API_PORT=9090 docker compose up --build
```

Dockerfile 内置 `HEALTHCHECK`，Compose 中的 `api` 服务也配置了健康检查；容器内端口固定 8080。

## 一次性验证

```bash
docker compose --profile verify up --build --exit-code-from verify
```

`verify` 服务在干净容器中依次执行：源码字节编译检查 → 单元测试 → 等待 API 健康 → 冒烟测试（守恒证书、乱序一致性、重复消耗 / 比例漂移 / 不守恒 / 成环 / 悬空引用 / 重复批号 / 非法结构均被整批拒绝且无部分结果），最后以退出码 0（全部通过）或 1（存在失败）报告结果。

## 本地开发

```bash
python -m unittest discover -s tests -t .   # 单元测试
python -m app.main                          # 本地启动（PORT 环境变量可改端口，默认 8080）
API_URL=http://127.0.0.1:8080 python -m verify.run   # 对本地实例跑验证
```
