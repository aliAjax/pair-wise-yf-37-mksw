# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 检验更正

检验结果录错且病例已确诊时，检验人员（`lab`角色）可对`confirmed`病例提交`correct_lab_result`动作，请求数据需包含`correction_id`（更正单号）、`reason`（更正依据）和`corrected_result`（新结论）：

- 病例回到`investigating`，原确认数据与审计记录、接触者的关联关系全部保留，更正前的确认信息快照存入`data.correction.previous_confirmation`。
- 更正待结论期间，关联接触者的`complete_followup`会被拒绝（HTTP 409），错误信息说明卡在哪一步、等待哪个病例的哪份更正。
- 病例经`lab_positive`或`mark_probable`恢复结论后仍在原病例上继续，更正记录标记为`resolved`，接触者随即可以完成医学观察。
- 同一`correction_id`重复提交只处理一次：重复请求直接返回当前病例，不产生新版本和重复审计。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
