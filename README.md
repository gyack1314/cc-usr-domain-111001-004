# 演练资料上报的可核验表单 RPA

把多场社区演练资料自动填入既有门户表单。门户**没有批量接口、没有幂等键、页面令牌一次一换、
提交后偶尔断线**，本工具围绕“提交结果不明确时绝不盲目重报”设计，并为工作人员保留可接手的
完整证据。全部内容为虚构合成数据，不涉及真实账号、人员或设施。

## 设计约束（硬性纪律）

- RPA 只通过门户的四个业务页面工作：`POST /login`、`GET /form`、`POST /submit`、`GET /query`；
  路径在客户端用白名单（`rpa/portal.py: ALLOWED_PATHS`）限制，运维入口 `/__control`、`/__calls`
  与模拟器内部函数一律不导入、不调用，也不读写门户的数据文件。
- 自然键 = `组织代码|活动编号`，门户允许同一自然键存在多条提交，因此自然键**不具备幂等性**。
- 内容判同用两道哈希：随表提交的 `summary_hash`（摘要正文 SHA-256，门户协议字段）和本地
  `content_hash`（五个业务字段的规范化 SHA-256）。
- 提交请求超时、连接中断、响应截断 → 一律记为 `SUBMIT_UNKNOWN`，**只能通过查询页核对，禁止自动重提**。
- 查询核对规则：
  - 恰好 **1 条内容相同**且无其他回执 → `CONFIRMED`；
  - 多条内容相同，或存在内容不同的回执 → `CONFLICT`（转人工）；
  - 暂时查不到 → 保持 `SUBMIT_UNKNOWN`，等待下一轮，**不触发第二次提交**；
  - 只有门户明确返回未写入的拒绝（403 令牌失效 / 422 业务校验，`written=false`）才记
    `REJECTED`，由人工修正资料后用 `reset` 放行重试。
- 人工补录的回执号也必须回门户查询页验证“本账号可见 + 组织归属 + 内容一致”，任意字符串不能当成功。
- 日志与证据文件对**口令和 session cookie 统一脱敏**（`rpa/security.py`），口令通过运行配置
  的 `password_env` 从环境变量读取，不写死在仓库里。

## 状态机

```
PENDING ──claim_submit──▶ SUBMITTING ──完整回执──▶ CONFIRMED
                              │
            断线/超时/截断     ├─▶ SUBMIT_UNKNOWN ──查询:唯一同内容──▶ CONFIRMED
                              │        │           查询:多条/异内容──▶ CONFLICT
                              │        │           查询:暂无线索────▶ 保持 SUBMIT_UNKNOWN
                              │
            403/422(未写入)   └─▶ REJECTED ──人工修正 + reset──▶ PENDING
```

- `SUBMITTING`/`SUBMIT_UNKNOWN` 的任务永远不会被 `claim_submit` 再次领取；租约过期后只能被
  `claim_reconcile` 接管（且 `SUBMITTING` 只会升级为 `SUBMIT_UNKNOWN`，绝不退回 `PENDING`）。
- 存储为单文件 SQLite（WAL + `BEGIN IMMEDIATE`），多进程共享时领取是原子的；状态迁移带
  `lease_owner` 属主校验，陈旧工作进程无法覆盖新状态。

## 目录

```
rpa/                RPA 实现（仅标准库）
  model.py          资料模型、自然键、summary/content 哈希、校验
  portal.py         门户 HTTP 客户端（路径白名单、异常按是否写入分类、401 有限自愈）
  store.py          SQLite 任务存储：领取、租约、重启恢复、防重
  engine.py         提交→待核实→查询核对状态机、人工补录验证
  security.py       脱敏日志过滤器、证据落盘
  config.py         运行配置（账号经 password_env 注入）
  cli.py            命令行入口
source_site/        本地门户模拟器（四个业务页 + 两个仅测试用运维入口）
sources/            合成资料与接口约定
tests/              45 项检查，全部走真实环回 HTTP
run_rpa.py          RPA 启动器（支持 python -I 隔离模式）
run_source.py       门户模拟器启动器
run_tests.py        测试启动器
configs/            示例运行配置
```

## 运行

仅依赖 Python 3.11+ 标准库。

```sh
# 终端 1：启动本地门户模拟器
python -I run_source.py --port 8090

# 终端 2：预览（零 HTTP，不调用提交页）
RPA_PASSWORD_ORG_A=local-only RPA_PASSWORD_ORG_B=local-only-2 \
  python -I run_rpa.py --config configs/demo.config.json preview sources/submissions.json

# 不带 --confirm：只登记任务，不提交
python -I run_rpa.py --config configs/demo.config.json run sources/submissions.json

# 确认批次：登录→取一次性 CSRF→提交→（必要时）查询核对
python -I run_rpa.py --config configs/demo.config.json run --confirm sources/submissions.json

# 逐项状态与门户回执；加自然键查看该任务的全部尝试与证据索引
python -I run_rpa.py --config configs/demo.config.json status
python -I run_rpa.py --config configs/demo.config.json status 'ORG-A|DR-1'

# 对待核实任务执行查询页核对
python -I run_rpa.py --config configs/demo.config.json reconcile

# 工作人员补录回执：回门户验证组织与内容后才确认
python -I run_rpa.py --config configs/demo.config.json verify-receipt 'ORG-A|DR-1' R1

# 对门户明确拒绝（REJECTED）的任务，修正资料后放行重试
python -I run_rpa.py --config configs/demo.config.json reset 'ORG-A|DR-1'
```

证据写入配置中的 `evidence_dir`，每个关键动作一个脱敏 JSON；运行状态在 `db_path`（SQLite）。
`state/` 已在 `.gitignore` 中，不会入库。

## 门户模拟器的故障阶段（仅测试/演练用）

通过 `POST /__control` 切换（RPA 自身不使用该入口）：

- `base`：正常；
- `expired`：会话代次轮换，旧会话返回 401，重新登录获得的新会话可用（模拟真实重登）；
- `lost-response`：提交记录**已写入**，但响应被截断并断连，用于验证“结果不明→查询核对、不重报”；
- `query-delay`：查询页暂时返回空列表，用于验证“查不到也不重提”。

合成账号：`demo-org/local-only`（组织 ORG-A）、`demo-org2/local-only-2`（组织 ORG-B），
查询页按账号组织隔离回执，可验证跨组织错误回执会被拒绝。

## 开发检查

```sh
python -I run_tests.py                      # 隔离模式跑全部 45 项
python -m unittest discover -s tests        # 常规模式等价
python -m compileall -q .
```

检查覆盖：四个业务页协议与异常分类、CSRF 一次性、断线不重报、查询延迟、会话过期自愈、
多/异内容冲突、人工补录与跨组织拒绝、多进程并发领取只产生一张回执、重启恢复、脱敏与证据留存、
预览零 HTTP。容器内等价检查：

```sh
docker build -t drill-form-rpa .
docker run --rm --network none drill-form-rpa
```

模拟器的 HTTP 异常是合成协议行为，便于接入端开发与诊断；业务数据、HTML 与响应均不应被当作
真实部署凭据或真实业务事实。
