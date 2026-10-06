# 双分区固件刷写协调 HTTP API

面向 **A/B 双分区设备** 的固件刷写协调服务（Python · FastAPI · SQLite）。
服务端负责裁决刷写流程：先校验镜像输入，再只向**非活动分区**按页序下发写页指令，
逐页核对设备回报的写后 SHA-256，全部匹配后才下发分区切换指令，并基于设备重启回报
裁决切换结果（成功完成 / 回滚重试 / 故障）。

- 端口：**8080**
- 数据库：SQLite（WAL 模式），数据卷 `/data`，**进程重启后原会话继续**
- 交互式文档：`http://localhost:8080/docs`

## 状态机

```
                1..8 页全部按序 ack（摘要全部匹配）
 writing ───────────────────────────────────────────▶ switch_pending
    │                                                      │
    │ 任一页摘要不符                                        ├─ 重启进目标分区 + 目标摘要 ─▶ completed
    ▼                                                      ├─ 重启进原分区 + 原摘要 ────▶ switch_pending
 failed ◀──────────────────────────────────────────────────┘ （生成新的切换尝试，不重写页）
                  其他任何重启结果（分区/摘要不符）
```

核心规则：

1. **先校验后下发**：创建会话时校验每页 base64 合法性、每页 SHA-256；
   目标镜像 SHA-256 = `SHA256(各页解码字节按页序拼接)`。任何非法编码或摘要返回 **422**，不产生任何状态。
2. **只写非活动分区**：写页指令的目标分区固定为创建会话时的非活动分区，
   全流程绝不向原活动分区写入。
3. **按页序、单飞**：一次只有一条未确认的写页指令；重复拉取返回**同一指令 ID 和内容**。
4. **指令先持久化后返回**：每条指令在 HTTP 返回前已落库（重启后仍可重复拉取同一条）。
5. **切换后不得再写页**；切换结果未知时**不得重复切换**——重复拉取返回当前待裁决的同一条切换指令。
   切换指令语义为“交换活动分区”，对同一条指令重复执行会在新旧分区之间来回切换，因此服务端
   只允许每轮尝试下发一次。
6. **重启回报裁决**：回报必须包含唯一 `report_id`、所针对的切换指令 ID、活动分区及镜像摘要；
   只有绑定**当前切换尝试**的回报可裁决：
   - 目标分区 + 目标摘要 → `completed`
   - 原分区 + 原摘要 → 回滚，**生成新的切换尝试**（新指令 ID，不重写任何页）
   - 其他结果 → `failed`
7. **冲突与幂等**：同值回报重复提交幂等（返回原裁决结果）；同 `report_id` 异值、
   针对已裁决/过期指令的回报返回 **409** 且不改变状态。
8. **单设备单未结束会话**：同一设备存在 `writing`/`switch_pending` 会话时，再创建返回 **409**。
9. **原子裁决**：同一设备的并发拉取/回报通过“进程内设备锁 + SQLite `BEGIN IMMEDIATE`”串行化，
   跨进程（多 worker）同样安全。

## 一键启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8080
```

停止并保留数据：`docker compose down`；连数据卷一并清除：`docker compose down -v`。

### Docker

```bash
docker build -t flashcoord .
docker run -d --name flashcoord -p 8080:8080 \
  -v flashcoord-data:/data flashcoord
```

### 本地直接运行（无 Docker）

```bash
python3 -m pip install -r requirements.txt
export FLASHCOORD_DB_PATH=./flashcoord.db
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## API 概览

设备侧所有请求需带头 `X-Device-Id: <设备ID>`（否则 422）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions` | 创建刷写会话（请求体带设备信息，无需设备头） |
| `POST` | `/devices/me/instructions/pull` | 拉取下一条待执行指令（写页/切换） |
| `POST` | `/devices/me/instructions/{id}/page-report` | 回报某写页指令的写后页摘要 |
| `POST` | `/devices/me/reboot-reports` | 上报切换后的重启结果 |
| `GET`  | `/devices/me/sessions/current` | 当前未结束会话状态 |
| `GET`  | `/devices/me/sessions?limit=` | 本设备会话列表 |
| `GET`  | `/devices/me/sessions/{sid}` | 会话详情（含各页与切换尝试） |
| `GET`  | `/devices/me/sessions/{sid}/instructions` | 指令历史（按发送顺序，含载荷） |
| `GET`  | `/healthz` | 健康检查 |

### 指令载荷

写页指令：

```json
{
  "id": "ab12…", "kind": "write_page", "page_index": 0, "status": "pending",
  "payload": {
    "type": "write_page",
    "partition": "b",
    "page_index": 0,
    "data": "<base64 字节>",
    "expected_sha256": "<该页 SHA-256>"
  }
}
```

切换指令（交换活动分区；`from` 为当前非活动（目标）分区，`to` 为原活动分区）：

```json
{
  "id": "cd34…", "kind": "switch", "switch_attempt": 1, "status": "pending",
  "payload": {"type": "switch", "attempt": 1, "swap": {"from": "b", "to": "a"}}
}
```

## 调用示例

以下示例中 `D0/D1` 为页字节 SHA-256，`B0/B1` 为 base64 编码，`TARGET` 为
`SHA256(decode(B0)‖decode(B1))`（由服务端计算并在会话详情中返回）。

### 1. 创建会话（1～8 页）

```bash
curl -sS -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' -d '{
    "device_id": "dev-001",
    "active_partition": "a",
    "active_image_digest": "1111111111111111111111111111111111111111111111111111111111111111",
    "pages": [
      {"data": "B0…", "sha256": "D0…"},
      {"data": "B1…", "sha256": "D1…"}
    ]
  }'
```

- base64 非法 / 页摘要不符 / 页数不在 1~8 / 摘要非 64 位十六进制 → **422**
- 设备已有未结束会话 → **409** `open_session_exists`

### 2. 设备拉取指令并逐页回报

```bash
# 拉取（重复调用在该页确认前始终返回同一条指令）
curl -sS -X POST http://localhost:8080/devices/me/instructions/pull \
  -H 'X-Device-Id: dev-001'

# 回报页写后摘要（匹配 -> match:true；最后一页匹配后进入 switch_pending）
curl -sS -X POST \
  http://localhost:8080/devices/me/instructions/<指令ID>/page-report \
  -H 'Content-Type: application/json' -H 'X-Device-Id: dev-001' \
  -d '{"sha256": "<设备实测页 SHA-256>"}'
```

- 摘要与期望不符 → 会话立即 `failed`（响应中 `match:false`），未决指令全部作废
- 对已确认指令重报同值摘要 → 200 `idempotent:true`；重报异值 → **409**

### 3. 拉取切换指令并上报重启结果

```bash
curl -sS -X POST http://localhost:8080/devices/me/instructions/pull \
  -H 'X-Device-Id: dev-001'
# -> kind=switch, switch_attempt=1

# 设备重启进目标分区 b 且运行目标镜像
curl -sS -X POST http://localhost:8080/devices/me/reboot-reports \
  -H 'Content-Type: application/json' -H 'X-Device-Id: dev-001' -d '{
    "report_id": "rpt-20261006-01",
    "switch_instruction_id": "<切换指令ID>",
    "active_partition": "b",
    "active_image_digest": "<TARGET>"
  }'
# -> {"outcome": "completed", "session_status": "completed"}
```

回滚场景（设备重启后仍在原分区、原镜像，例如掉电/自检失败导致交换回去）：

```json
{ "report_id": "rpt-…-rb", "switch_instruction_id": "<切换指令ID>",
  "active_partition": "a", "active_image_digest": "<原镜像摘要>" }
```

响应 `outcome=rollback`，并给出 `new_switch_instruction_id`；设备再次 `pull`
得到**第 2 次切换尝试**，不会重写任何页。

- 同 `report_id` 同值重放 → 200 `idempotent:true`（回滚重放仍返回同一后续指令 ID）
- 同 `report_id` 异值 → **409** `conflicting_report`
- 回报旧的/已裁决的切换尝试 → **409**（`stale_instruction` 或 `instruction_already_reported`）
- 分区/摘要既不是目标也不是原状态 → `outcome=failure`，会话 `failed`

### 4. 查询状态与指令历史

```bash
curl -sS http://localhost:8080/devices/me/sessions/current -H 'X-Device-Id: dev-001'
curl -sS http://localhost:8080/devices/me/sessions/<会话ID> -H 'X-Device-Id: dev-001'
curl -sS http://localhost:8080/devices/me/sessions/<会话ID>/instructions \
  -H 'X-Device-Id: dev-001'
```

### 一键脚本演示完整流程

```bash
BASE_URL=http://localhost:8080 DEVICE_ID=demo-device bash examples/demo.sh
```

## 测试

```bash
python3 -m pip install pytest httpx
PYTHONPATH=. python3 -m pytest tests/ -q
```

覆盖：输入校验、完整成功路径、拉取幂等（同 ID 同内容）、页摘要不符故障态、
回滚生成新尝试且旧尝试不可再裁决、重启回报幂等/冲突/过期、同设备单会话、
只写非活动分区、进程重启后续跑，以及并发拉取/并发回报/并发生成会话的原子性。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLASHCOORD_DB_PATH` | `/data/flashcoord.db` | SQLite 数据库路径（目录需可写） |
