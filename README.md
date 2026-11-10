# 演练资料上报的可核验表单 RPA资料源

本项目保存本地合成资料及对应的来源站行为，供资料接入开发使用。内容取自虚构场景，不代表真实活动、人员、设施或设备参数。来源服务只模拟公开读取与指定的表单行为，不包含资料采集端。

## 环境与运行

使用 Python 3.11 及以上版本，仅依赖标准库。来源只监听本机地址，端口可以通过参数调整。两个终端分别启动不同端口可以表示两个独立来源。

```sh
python -m source_site --port 8090
```

`source_site/source.py` 管理来源响应，`source_site/__main__.py` 是 HTTP 适配层，`sources/` 保存原文与接口约定。状态只存在来源进程内，重启来源会恢复初始样本。`/__control` 和 `/__calls` 属于本地运维入口，资料接入端的正常操作不使用它们。运维端可以通过 POST `/__control` 提供 JSON 对象改变 phase 或更新 state。phase 列表与业务接口见下文，细节见 `sources/contract.json`。

- `POST /login`：URL编码表单account=demo-org,password=local-only，均为公开合成凭据
- `GET /form`：需session Cookie，一次性CSRF
- `POST /submit`：正常表单，允许同自然键重复写入
- `GET /query`：按查询字段过滤回执
- `summary_hash`：UTF-8摘要正文的SHA-256
- `phases`：["base", "expired", "lost-response", "query-delay"]

用 Python 的 HTTP 客户端推进来源状态，例如：

```sh
python -c 'import urllib.request; r=urllib.request.Request("http://127.0.0.1:8090/__control", data=b"{\"phase\":\"base\"}", headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(r).read().decode())'
```

## 开发检查

以下检查覆盖来源协议、异常响应和真实本地 HTTP 启动。检查不会要求访问公网，也不会写入业务状态目录。

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
```

容器环境也可以复现相同检查：

```sh
docker build -t drill-form-rpa-source .
docker run --rm --network none drill-form-rpa-source
```

来源的HTTP异常是合成协议行为，便于资料接入端开发和诊断。样本可扩展；业务数据、原始HTML以及接口响应不应被当作真实部署凭据或真实业务事实。
