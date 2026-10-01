# 山洪水位预警裁定服务

基于 **Python 3.12 + FastAPI + SQLite(WAL)** 的山洪水位预警裁定服务：
登记监测站阈值与设备，设备按 `(设备号, 设备内递增序号)` 补传带时区的观测时间和水位，
服务按观测时间重排有效读数并自动裁定预警/解除；审核员可带原因作废或更正读数（原值与历次修订全程可查），
裁定随之重算；发布操作冻结各站预警时间线，之后的补传/修订只改变**待发布**结果，重新发布后才更新**公开**结果。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 首次启动自动建表；空库默认写入一套演示数据（演示站 `DEMO-001`、设备 `DEV-DEMO-0001`，
  包含一次完整的“预警 → 解除”过程和一个断段后的孤立高读数）。
- 服务地址：<http://localhost:8000>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

停止 / 清空数据重来：

```bash
docker compose down            # 停止，数据保留在命名卷 flood-data
docker compose down -v         # 停止并删除数据卷（下次启动重新初始化）
```

## 2. 本地裸跑（可选）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 配置（均有默认值）
export FLOOD_DB_PATH=./data/flood.db      # SQLite 文件路径，默认 /data/flood.db
export FLOOD_REVIEW_TOKEN=change-me       # 审核/发布令牌；留空则不鉴权
export FLOOD_SEED_DEMO=true               # 空库时自动种演示数据

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest
python -m pytest -q
```

## 3. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLOOD_DB_PATH` | `/data/flood.db` | SQLite 数据库文件路径 |
| `FLOOD_REVIEW_TOKEN` | _空_ | 审核/发布接口令牌；为空时这些接口不鉴权（仅建议内网演示） |
| `FLOOD_SEED_DEMO` | `true` | 空库启动时自动写入演示站、设备与读数 |
| `FLOOD_PORT`（仅 compose） | `8000` | 宿主机映射端口，如 `FLOOD_PORT=9000 docker compose up` |

裁定时间常量（15 分钟断段、3 读数/10 分钟预警、2 读数/5 分钟解除）在
`app/config.py` 中，按需求固定。

---

## 4. 裁定规则（实现见 `app/engine.py`）

1. **重排与断段**：取该站全部**有效**读数（已作废不参与），按观测时间升序（同一时刻按读数 id 稳定排序）；
   相邻读数间隔 **> 15 分钟**即断为不同段。
2. **预警（rised）**：同一段内**连续 ≥ 3 个**读数水位 **≥ 上升阈值**，且首、尾观测时间相隔
   **≥ 10 分钟**，在**首次同时满足两项条件**的读数处发出预警。段内出现低水位读数会清零高位连续计数。
3. **解除（cleared）**：预警之后，**连续 ≥ 2 个**读数水位 **≤ 解除阈值**，且首、尾相隔
   **≥ 5 分钟**则解除。**间隔断段不自动解除**（预警状态保持），但断段会打断低位连续计数，
   需要重新累计 2 个低位读数。
4. 预警/解除严格交替；解除后重新按规则 2 评估，可再次预警。
5. 阈值比较均为闭区间（`>=` / `<=`），水位与阈值以 `Decimal` 精确存储与比较。

## 5. 发布冻结模型

- `GET /api/stations/{code}/timeline`：**待发布（草稿）**时间线，随补传/修订实时重算；
  字段 `in_sync_with_public` 指示是否与公开版本一致。
- `POST /api/publish`：将各站当前时间线冻结为**严格递增的新版本**（JSON 快照整存，含当时阈值与读数）。
- `/public/...`：只返回**已发布**快照；补传、修订在重新发布前不会影响公开结果，历史版本永久保留。

## 6. 补传语义

- 唯一键 `(device_id, seq)`。
- 同设备同序号、**同内容**（观测时刻 + 水位均相同，时间支持不同时区的等价写法）：
  返回 `200` 且 `"replayed": true` 及原结果。
- 同设备同序号、**异内容**：返回 `409 Conflict`。
- 观测时间必须带时区（`+08:00` / `Z`），拒绝无时区、无法解析、未来时间。
- 未知设备 → `404`；非法水位/阈值/序号/时间 → `422`。

## 7. HTTP API 一览

> 标记 🔒 的接口需要请求头 `X-Reviewer-Token: <FLOOD_REVIEW_TOKEN>`（令牌为空时免鉴权）。
> 审核/作废/发布可用 `X-Reviewer: <审核员标识>` 记录操作人，默认 `anonymous-reviewer`。

### 登记

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | `/api/stations` | 登记站（`rise_threshold`/`clear_threshold`/`devices`），同码幂等 | 🔒 |
| GET | `/api/stations` | 站列表 | |
| GET | `/api/stations/{code}` | 站详情 | |
| POST | `/api/stations/{code}/devices` | 为已登记站追加设备 | 🔒 |
| GET | `/api/devices?station_code=` | 设备列表 | |

### 设备补传与读数查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/readings` | 补传读数（新建 `201`；同内容重试 `200 replayed=true`；异内容 `409`） |
| GET | `/api/stations/{code}/readings?device_no=&status=active\|voided\|all` | 按站查读数（默认仅有效） |
| GET | `/api/readings/{id}` | 读数详情（含作废状态） |

### 审核

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | `/api/readings/{id}/correct` | 更正水位，必须带 `reason` | 🔒 |
| POST | `/api/readings/{id}/void` | 作废读数，必须带 `reason` | 🔒 |
| GET | `/api/readings/{id}/revisions` | 原值与历次修订（更正链 + 作废记录） | |

### 裁定与发布

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| GET | `/api/stations/{code}/timeline` | 待发布时间线（实时重算） | |
| POST | `/api/publish` | 冻结发布，body `{"stations": ["..."]}`，空数组/缺省发布全部站 | 🔒 |
| GET | `/api/stations/{code}/publications` | 发布版本列表 | |

### 公开读取（无需令牌）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/public/stations` | 已发布站最新公开状态索引 |
| GET | `/public/stations/{code}/timeline?version=N` | 公开时间线（默认最新版本，可取历史版本） |

## 8. curl 调用示例

```bash
BASE=http://localhost:8000
TOKEN=change-me   # 与 FLOOD_REVIEW_TOKEN 对应；未设令牌时省略该请求头

# 登记站与设备
curl -sS -X POST $BASE/api/stations \
  -H "X-Reviewer-Token: $TOKEN" -H "Content-Type: application/json" -d '{
    "code": "QS-001", "name": "青石沟1号站",
    "rise_threshold": "5.00", "clear_threshold": "4.00",
    "devices": [{"device_no": "DEV-QS-001", "name": "雷达水位计"}]
  }'

# 设备补传（观测时间必须带时区）
curl -sS -X POST $BASE/api/readings -H "Content-Type: application/json" -d '{
  "device_no": "DEV-QS-001", "seq": 1,
  "observed_at": "2026-10-01T08:00:00+08:00", "water_level": "5.40"
}'

# 同内容原样重试 -> 200, "replayed": true；改水位/时间重试 -> 409

# 待发布时间线
curl -sS $BASE/api/stations/QS-001/timeline

# 审核更正 / 作废（必须带原因）
curl -sS -X POST $BASE/api/readings/4/correct \
  -H "X-Reviewer-Token: $TOKEN" -H "X-Reviewer: lisi" \
  -H "Content-Type: application/json" \
  -d '{"water_level": "3.60", "reason": "现场校测，受漂浮物影响"}'
curl -sS $BASE/api/readings/4/revisions

# 发布冻结 & 公开读取
curl -sS -X POST $BASE/api/publish \
  -H "X-Reviewer-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"stations": ["QS-001"]}'
curl -sS $BASE/public/stations/QS-001/timeline
curl -sS "$BASE/public/stations/QS-001/timeline?version=1"   # 历史冻结版本
```

也可以直接运行端到端示例脚本（自动生成相对当前时间的时间戳）：

```bash
TOKEN=change-me bash scripts/demo.sh
```

## 9. 错误响应

| 场景 | 状态码 |
| --- | --- |
| 未知设备 / 站 / 读数 / 站点尚无发布版本 | `404` |
| 同设备同序号但内容不同 | `409` |
| 缺/错审核令牌 | `401` |
| 无时区/非法时间、非法水位、非法阈值（含解除阈值 ≥ 上升阈值）、序号为负、缺修订原因、重复作废、作废后更正 | `422` |
| 请求体字段类型错误（Pydantic） | `422` |

## 10. 并发与持久化

- SQLite 开启 **WAL**（读写并发）与 `busy_timeout=30s`；所有写事务以
  `BEGIN IMMEDIATE` 开启，写操作全局串行化。
- 补传幂等/冲突由 `(device_id, seq)` 唯一约束在事务内兜底；发布在**单个写事务**内
  取快照并落库，与并发补传/修订互不丢记录（见 `tests/test_concurrency.py`：
  8 线程 200 条补传、同序号竞争、补传与 5 次发布并行、并发修订全部保留）。
- 数据库文件在 compose 命名卷 `flood-data` 中持久化，容器重建不丢数据。

## 11. 项目结构

```
app/
  config.py      # 环境变量配置与业务时间常量
  validation.py  # 时间/水位/阈值/序号校验，Decimal 规范化
  db.py          # SQLite 连接、schema、WAL、BEGIN IMMEDIATE 事务
  engine.py      # 纯函数裁定引擎（断段、预警、解除状态机）
  service.py     # 业务服务层与序列化（登记/补传/审核/裁定/发布/公开）
  schemas.py     # Pydantic 请求模型
  seed.py        # 空库演示数据
  main.py        # FastAPI 路由与错误映射
scripts/demo.sh  # curl 端到端调用示例
tests/           # 引擎规则、HTTP API、并发安全测试（共 24 个用例）
Dockerfile, docker-compose.yml, requirements.txt
```
