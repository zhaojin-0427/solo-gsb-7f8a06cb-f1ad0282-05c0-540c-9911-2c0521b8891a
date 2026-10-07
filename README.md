# 双分区设备固件刷写协调 API

基于 **Python 3.11 + FastAPI + SQLite** 的固件刷写协调服务。设备具有 A/B 双分区，
服务负责协调“向非活动分区分页写入 → 校验每页写后摘要 → 切换活动分区 → 依据重启
回报裁决”的完整流程。

## 核心规则

1. **创建会话（先校验，再落库）**：提交设备 ID、当前活动分区与活动镜像 SHA-256、
   1～8 页 base64 字节及各页 SHA-256。目标镜像摘要 = 各页字节**按页序拼接**后计算
   SHA-256。非法 base64、页摘要不符、页数不在 1～8、摘要不是 64 位小写十六进制
   一律返回 **422**。
2. **只写非活动分区**：活动分区为 A 则写 B，反之亦然。任何情况下都不向原活动分区
   写入。
3. **按页序写页**：设备拉取指令时惰性发放 `WRITE_PAGE`，严格按页序（0,1,2…）。
4. **指令先持久化再返回**：每条指令在响应前已提交到 SQLite。未确认的写页指令重复
   拉取时**保持同一指令 ID 与内容**（响应中 `redelivery: true`），进程重启后亦然。
5. **逐页裁决**：设备按指令 ID 回报写后页 SHA-256；与期望一致才放行下一页；摘要
   不符则该指令与会话立即进入 `FAILED` 故障态。
6. **切换分区**：所有页确认后才发放一次 `SWITCH` 指令（设备侧执行即交换活动分区，
   重复执行会切回旧分区）。切换指令发出后：
   - 不再接受/发放任何写页指令（返回 **409**）；
   - 当前切换尝试结果未知前，不会产生新的切换指令（重复拉取只回传同一条待裁决
     切换指令）。
7. **重启回报裁决**：回报包含唯一 `report_id`、所针对的 `switch_command_id`、
   重启后活动分区与镜像摘要。仅绑定**当前待裁决切换尝试**的回报可裁决：
   - 活动分区=目标分区 **且** 镜像摘要=目标摘要 → `SWITCHED`，会话 `COMPLETED`；
   - 活动分区=原分区 **且** 镜像摘要=原摘要 → `ROLLED_BACK`，旧尝试结案并**生成
     新的切换尝试**（新指令 ID，下次拉取时发放）；
   - 其他分区/摘要组合 → `FAILED` 故障态。
8. **幂等与冲突**：同值重复回报幂等（`idempotent: true` / `result: DUPLICATE`）；
   同 `report_id` 异值回报、针对过期（已裁决/被取代）切换指令的回报、跨会话或
   错误类型指令的回报返回 **409** 且不改状态；全局不存在的指令 ID 返回 **404**。
9. **同设备唯一未结束会话**：同一设备同时只能有一个 `WRITING` / `AWAITING_SWITCH`
   会话，冲突返回 **409**（数据库唯一部分索引兜底竞态）。
10. **按设备原子裁决**：并发拉取/回报由“进程内每设备互斥锁 + SQLite `IMMEDIATE`
    写事务”串行化。
11. **可查询、可恢复**：提供会话状态与指令历史查询；数据持久化在 SQLite
    （WAL），进程/容器重启后继续原会话。

## 会话状态机

```
WRITING ──(任一页摘要不符)──────────────► FAILED
WRITING ──(全部页 ACK，发放 SWITCH)──────► AWAITING_SWITCH
AWAITING_SWITCH ──(重启=目标分区/摘要)───► COMPLETED
AWAITING_SWITCH ──(重启=原分区/摘要)─────► AWAITING_SWITCH（新切换尝试）
AWAITING_SWITCH ──(重启=其他结果)────────► FAILED
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 监听端口：**8080**（`http://localhost:8080`）
- 数据库文件持久化在命名卷 `firmware-data`（容器内 `/data/firmware.db`）
- Swagger 交互文档：<http://localhost:8080/docs>
- 健康检查：`GET /health`

停止：

```bash
docker compose down            # 保留数据卷
docker compose down -v         # 同时删除数据库卷
```

## 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export FIRMWARE_DB_PATH=./data/firmware.db   # 默认 /data/firmware.db
uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
```

> 必须使用单 worker（`--workers 1`）：每设备互斥锁位于进程内，数据库事务再保证
> 持久化层面的原子性。

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FIRMWARE_DB_PATH` | `/data/firmware.db` | SQLite 数据库路径 |
| `FIRMWARE_PORT` | `8080` | 监听端口（仅影响容器默认运行方式） |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 创建刷写会话 |
| GET | `/sessions?device_id=` | 会话列表（可按设备过滤） |
| GET | `/sessions/{id}` | 查询会话状态 |
| GET | `/devices/{device_id}/open-session` | 查询设备当前未结束会话 |
| POST | `/devices/{device_id}/commands:pull` | 设备拉取下一条指令 |
| POST | `/sessions/{id}/page-reports` | 设备回报写后页摘要 |
| POST | `/sessions/{id}/reboot-reports` | 设备重启回报（裁决切换） |
| GET | `/sessions/{id}/commands` | 指令历史 |
| GET | `/sessions/{id}/reboot-reports` | 重启回报历史 |
| GET | `/health` | 健康检查 |

指令对象（`WRITE_PAGE`）包含 `data_b64`、`page_index`、`page_sha256`、
`target_partition`；指令对象（`SWITCH`）只含 `target_partition`。

## 调用示例

下面示例设备当前运行在 A 分区，刷写两页固件到 B 分区。

### 1. 创建会话

```bash
# 构造两页 base64 数据及摘要
P0=$(printf 'firmware page AAAA' | base64)
P1=$(printf 'firmware page BBBB' | base64)
S0=$(printf 'firmware page AAAA' | sha256sum | cut -d' ' -f1)
S1=$(printf 'firmware page BBBB' | sha256sum | cut -d' ' -f1)
OLD=$(printf 'old-firmware-image' | sha256sum | cut -d' ' -f1)

curl -sS -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d "{
    \"device_id\": \"dev-001\",
    \"active_partition\": \"A\",
    \"active_image_sha256\": \"$OLD\",
    \"pages\": [
      {\"data_b64\": \"$P0\", \"sha256\": \"$S0\"},
      {\"data_b64\": \"$P1\", \"sha256\": \"$S1\"}
    ]
  }"
```

响应（节选）包含会话 `id`、服务端按拼接字节计算的 `target_image_sha256`、
`target_partition: "B"`、`status: "WRITING"`。

### 2. 设备逐页拉取并回报（对每页重复直到确认）

```bash
curl -sS -X POST http://localhost:8080/devices/dev-001/commands:pull
# => {"redelivery": false, "command": {"id": "c1...", "type": "WRITE_PAGE",
#        "target_partition": "B", "page_index": 0, "data_b64": "...", ...}}

# 未确认前重复拉取：同一 ID、同一内容，redelivery=true
curl -sS -X POST http://localhost:8080/devices/dev-001/commands:pull

# 设备写入后按指令 ID 回报实际页摘要
curl -sS -X POST http://localhost:8080/sessions/<SESSION_ID>/page-reports \
  -H 'Content-Type: application/json' \
  -d "{\"command_id\": \"c1...\", \"page_sha256\": \"$S0\"}"
# => {"result": "ACKED", "idempotent": false, "session": {...}}
```

重复以上步骤直到两页全部 `ACKED`。若回报摘要不符：

```json
{"result": "FAILED", "idempotent": false,
 "session": {"status": "FAILED", "failure_reason": "page 0 post-write digest ..."}}
```

### 3. 拉取切换指令并执行重启

```bash
curl -sS -X POST http://localhost:8080/devices/dev-001/commands:pull
# => {"command": {"type": "SWITCH", "target_partition": "B", "id": "sw1..."}, ...}
```

设备执行 A/B 切换并重启，然后回报。成功进入目标分区：

```bash
curl -sS -X POST http://localhost:8080/sessions/<SESSION_ID>/reboot-reports \
  -H 'Content-Type: application/json' \
  -d "{
    \"report_id\": \"rep-0001\",
    \"switch_command_id\": \"sw1...\",
    \"active_partition\": \"B\",
    \"active_image_sha256\": \"<target_image_sha256>\"
  }"
# => {"result": "SWITCHED", "new_switch_command_id": null,
#     "session": {"status": "COMPLETED", ...}}
```

若重启后发现仍在旧分区（设备对切换指令执行了两次等情况），回报原分区/原摘要：

```json
{"result": "ROLLED_BACK",
 "new_switch_command_id": "sw2...",
 "session": {"status": "AWAITING_SWITCH", "current_switch_command_id": "sw2..."}}
```

设备下次拉取会得到**新的** `SWITCH` 指令（ID 为 `sw2...`），再次执行并回报即可。

### 4. 查询状态与历史

```bash
curl -sS http://localhost:8080/sessions/<SESSION_ID>
curl -sS http://localhost:8080/sessions/<SESSION_ID>/commands
curl -sS http://localhost:8080/sessions/<SESSION_ID>/reboot-reports
curl -sS http://localhost:8080/devices/dev-001/open-session
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：输入校验 422、完整成功路径、未确认指令同 ID 重投（含重启恢复）、页摘要
不符故障态、切换后拒写、回滚后生成新切换尝试、异常重启结果故障、回报幂等与
409 冲突、并发创建唯一会话、并发拉取/回报原子裁决、进程重启后续跑会话。
