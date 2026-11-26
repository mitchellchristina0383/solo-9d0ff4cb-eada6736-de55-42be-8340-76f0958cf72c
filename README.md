# 坏盘救援台（Disk Rescue）

旧盘部分扇区读不出来时，值班员用本服务把恢复工具已读出的数据先存下来，
多轮补读直到整盘救回。Flask 实现，Docker Compose 一键启动。

## 功能与关键语义

- **样盘上传**：按 512 字节分扇区的原始镜像（raw image），大小必须是
  512 的整数倍，范围 1 扇区 ~ 8 MiB（16384 扇区），流式校验、超限即拒。
- **陆续读盘**：恢复工具每读出一个扇区就上报；读失败的扇区不上报即保持
  缺失，继续往后读。下一轮先拉缺失清单，只补读未救出的扇区。
- **中断安全**：每个扇区独立文件，写入走「临时文件 + fsync + 原子 rename
  + 目录 fsync」。重启时以磁盘扇区文件为准重建状态：已完整保存的扇区仍
  可用，写到一半的 `*.tmp` 在启动对账时删除，仍算缺失。
- **同进度下载**：先创建快照（snapshot），救援镜像与缺失清单共用同一个
  `snapshot_id`，保证两者对应同一份进度；下载期间读盘可以继续，快照内容
  不随后续读盘变化。镜像中缺失位置补零；**实际读到的全零扇区正常保存、
  不算缺失**（与补零位置可通过缺失清单区分）。
- 重复上报幂等；单条非法扇区不拖垮整批，错误在 `rejected` 中逐条返回。

## 目录结构

```
rescue/storage.py     持久化层：样盘、扇区原子写、崩溃对账、快照
rescue/app.py         Flask 路由与 JSON 接口
templates/index.html  救援页（上传 / 进度 / 上报 / 快照下载）
wsgi.py               gunicorn 入口
scripts/sim_reader.py 模拟恢复工具的读盘脚本
tests/                单元测试、端到端集成测试、冒烟测试
Dockerfile, docker-compose.yml
```

数据目录（容器内 `/data`，命名卷 `rescue-data`）：

```
/data
├── job.json            # 状态缓存（真相以 sectors/ 下的文件为准）
├── sample.img          # 样盘
└── sectors/<分片>/<扇区号>.bin
```

## 用 Docker Compose 启动

```bash
docker compose up --build -d
# 打开 http://localhost:8000
docker compose logs -f
docker compose down          # 停止；数据保留在命名卷 rescue-data
docker compose down -v       # 停止并删除救援数据
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 救援页 |
| POST | `/api/job` | multipart 上传 `image` 字段建任务 |
| GET | `/api/job` | 当前进度（saved / missing 列表、SHA-256） |
| GET | `/api/missing` | 当前缺失扇区清单（每轮补读前拉取） |
| DELETE | `/api/job` | 重置任务 |
| POST | `/api/sectors` | 上报读出扇区（JSON 批量或单扇区二进制） |
| GET | `/api/sectors/<N>` | 取回已救出的原始扇区（缺失返回 409） |
| POST | `/api/snapshots` | 生成进度快照，返回 `snapshot_id` |
| GET | `/api/snapshots/<id>/image.bin` | 下载该进度的救援镜像（缺失补零） |
| GET | `/api/snapshots/<id>/missing.json` | 下载同一进度的缺失扇区清单 |

上报扇区两种方式：

```bash
# 1) JSON 批量，data 为 base64
curl -X POST http://localhost:8000/api/sectors \
  -H 'Content-Type: application/json' \
  -d '{"sectors":[{"sector":12,"data":"<base64, 恰好 512 字节解码后>"}]}'

# 2) 单扇区原始二进制
curl -X POST 'http://localhost:8000/api/sectors?sector=12' \
  --data-binary @sector12.bin
```

快照下载：

```bash
SID=$(curl -s -X POST http://localhost:8000/api/snapshots | jq -r .snapshot.snapshot_id)
curl -OJ http://localhost:8000/api/snapshots/$SID/image.bin
curl -OJ http://localhost:8000/api/snapshots/$SID/missing.json
```

## 模拟读盘

```bash
python3 scripts/sim_reader.py --base http://localhost:8000 \
  --image ./sample.img --fail 3,7,1024 --rounds 3 --workers 4
```

每轮开始拉取缺失清单，只补读仍缺失的扇区；`--fail` 列出本轮读不出的扇区，
失败即跳过。

## 测试

```bash
python3 -I tests/run.py               # 38 个单元测试
python3 -I tests/integration_test.py  # 中断重启补读、边读边下载
python3 -I tests/smoke_test.py        # 经 sim_reader 走真实 HTTP 的冒烟
```

## 设计说明与边界

- gunicorn 默认 **1 进程 8 线程**：快照保存在进程内存中，单进程可保证镜像
  与清单严格同进度；所有存储操作由同一把可重入锁串行化，线程安全。
- `job.json` 只是缓存，任何时刻都可以从 `sectors/` 重建，因此崩溃后不会
  出现「状态说有、文件没有」的幻影像。
- 快照保存在内存中，进程重启后旧 `snapshot_id` 失效（重新建快照即可）；
  已救出的扇区数据不受影响。
- 样盘最大 8 MiB 是按题目要求设定的硬限制。
