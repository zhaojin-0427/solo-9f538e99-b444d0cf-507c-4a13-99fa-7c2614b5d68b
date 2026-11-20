# 冷链温区账本

用于冷库监管核对「已出具月报为何把某批货计入冷藏或冷冻」的网页应用。
出库事实按 UTC 月一次性冻结；温区认定是一份只追加的知识账本，每次整组更正
生成新的知识版号。补登过去生效的区间只影响新版汇总，任何旧版号仍返回它当初
的统计口径。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在名为 `coldchain-data` 的卷中（容器内 `/data/coldchain.sqlite3`），
`docker compose restart` 或重新 `up` 之后旧版号与旧事实均可继续追查。
可通过环境变量改端口/密钥：

```bash
COLDCHAIN_PORT=9000 COLDCHAIN_SECRET='...' docker compose up --build -d
```

## 本地运行（不使用容器）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py                       # http://127.0.0.1:8000
# 或
COLDCHAIN_DB=/tmp/cc.sqlite3 gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 run:app
```

## 使用流程

1. **上传月结**：上传单月出库 CSV。列为 `流水号,SKU,出库时刻,箱数`（也接受常见别名，
   如 `出库流水号 / 商品编码 / 出库时间 / 件数`）。时刻为 ISO-8601，可带 `Z` 或偏移量，
   统一换算为 UTC；文件必须同属一个 UTC 月份，流水号全局唯一，箱数为正整数。
   校验失败时**一行都不会写入**；成功后该月事实永久固定，重复上传返回冲突。
2. **登记温区**：按 SKU 登记 `[生效, 失效)` 区间（起含止不含），失效留空表示持续有效。
   同一版号内同一 SKU 的区间不得重叠；整组提交即整组替换，并生成新知识版号。
   若提交时原基准已不是最新版（他人先提交了更正），会返回冲突并**保留草稿和原基准**，
   修掉重叠后普通重提仍按原基准校验、不会被悄悄套到别人的新版上。确实要沿用自己的
   旧稿时，在温区知识页点「打开核对页」：并排查看**原基准、当前版、草稿**中该 SKU 的
   全部区间，再明确确认整组替换；确认只提交核对页展示的草稿，改了草稿或账本又出新版
   都需重新核对。确认成功才生成新版（版号详情记录草稿原基准与经核对的版号），
   重复确认最多生成一版，失败不留新版或复核记录。旧版号与旧月报查询不受影响。
3. **核对月报**：选择月份和知识版号（留空=最新版），查看各温区箱数、每笔对应流水
   及其依据区间，以及未归类流水。空档期间的出库自动列为未归类。

示例文件：`examples/2026-03.csv`（可用 `python examples/make_example.py` 重新生成）。

## 关键规则如何落实

| 要求 | 实现 |
| --- | --- |
| 上传成功后固定该月事实 | `months` 与 `shipments` 在同一 `BEGIN IMMEDIATE` 事务写入；数据库触发器禁止 `UPDATE/DELETE`；同月重复上传返回 409 |
| 区间起含止不含 | 所有时点存为 UTC 微秒整数，判定 `valid_from_us <= t < valid_to_us` |
| 同版同 SKU 不重叠 | 应用层排序校验 + SQLite 触发器 `ranges_no_overlap` 双重保护 |
| 空档出库未归类 | `classify()` 返回空即为未归类，与已归类箱数之和恒等于月总箱数 |
| 每次整组更正生成新版号 | 新版本 = 其他 SKU 区间原样带入 + 目标 SKU 整组替换；版本与区间同事务写入 |
| 补登过去只改新版 | 版本只追加、不修改；查询按选定版号读取该版区间快照 |
| 更正/查询交错不混用新旧 | 写用 `BEGIN IMMEDIATE` 串行化；查询在一个读事务里同时取月份事实、版号与区间 |
| 非法/失败不产生半版 | 校验先于写入；任何异常整体回滚；版本号和区间在同一事务提交 |
| 重启后旧版可追查 | 全部状态在 SQLite（WAL，`synchronous=FULL`），容器卷持久化 |
| 核对确认后才覆盖他人更正 | 签名回执绑定草稿与核对时版号；`version_reviews.token_id` 唯一且与版本同事务提交，重复确认不会生成第二版 |

并发更正采用「基准版号」乐观并发控制：提交时必须声明所依据的版本号，若已不是
最新版则返回 409 要求刷新重试，因此不会把两次不同更正悄悄合并或覆盖。核对确认是
唯一例外，且必须带着核对页签发的回执：回执绑定该草稿与当时的最新版号，由服务端用
`COLDCHAIN_SECRET` 签名，篡改草稿或版号变化都会使回执失效。复核记录
（`version_reviews`）与新版本在同一 `BEGIN IMMEDIATE` 事务写入，失败则一起回滚。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/months` | 已固定月份列表 |
| POST | `/api/months` | multipart 上传 `file`（可选表单字段 `month` 做校验） |
| GET | `/api/versions` | 知识版号列表 |
| POST | `/api/versions` | `{"sku","intervals":[{"zone","valid_from","valid_to"}],"reason","base_version"}` |
| GET | `/api/versions/<n>` | 某版全部 SKU 区间；`review` 字段给出该版由哪次核对确认生成（草稿原基准 / 核对时当前版） |
| GET | `/api/report?month=YYYY-MM&version=<n>` | 月报（省略 `version` 用最新版） |
| GET | `/knowledge/review?token=<回执>` | 并排核对原基准、当前版与草稿 |
| POST | `/knowledge/review` | 由草稿表单打开核对页 |
| POST | `/knowledge/review/confirm` | 带核对回执明确确认整组替换 |
| GET | `/api/audit` | 最近 200 条审计记录 |
| GET | `/healthz` | 健康检查 |

`base_version` 为 `null` 仅允许用于空账本创建 v1；否则必须是当前最新版号。
`POST /api/versions` 不接受核对回执，因此 API 普通重提永远按基准版号校验；
核对确认只经由页面 `/knowledge/review/confirm` 提交。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：月结冻结与重复拒绝、CSV 各类非法输入、起含止不含边界、空档未归类、
重叠/倒序区间拒绝、补登不改旧版、基准版号冲突、并发更正串行化、查询快照一致、
重启后旧版可追查、数据库层不可变触发器等；以及并发更正冲突后草稿与原基准保留、
修掉重叠仍按原基准校验、核对页三组并排、改草稿/账本出新版后须重新核对、
重复确认只生成一版、确认失败不留新版或复核记录、旧月报照常查询（`tests/test_review.py`）。

## 目录

```
coldchain/
  app.py        Flask 路由与页面/API
  facts.py      月结 CSV 校验与冻结
  knowledge.py  温区版本、区间校验、分类与报表
  db.py         SQLite schema、触发器、事务
  timeutil.py   UTC 解析与规范化
  templates/    页面
tests/          单元与并发测试
examples/       示例月结 CSV
Dockerfile, docker-compose.yml
```
