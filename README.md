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

## 检验结果纠错路径

检验结果录错时病例往往已经确认。纠错流程为：

1. 检验人员（`lab`/`admin`）对`confirmed`病例提交`correct_lab_result`，必填`correction_id`（更正单号）、`reason`（更正依据）、`new_conclusion`（新结论）、`lab_id`（复检单号）。病例回到`investigating`，更正记录挂在病例`data.corrections`下（状态`pending`，内含原确认信息快照`prior`）。
2. 原先的确认记录保留：审计时间线只追加不修改；接触者与病例的关联关系完全不动。
3. 等待结论期间，关联接触者无法`complete_followup`，服务端返回`409 WorkflowBlocked`并指明卡在“完成随访/观察完成”这一步；演示页面同样标注阻塞步骤。
4. 检验人员对同一病例重新下结论（`lab_positive`或`mark_probable`）后继续使用原病例，更正记录变为`resolved`，接触者观察随即可以完成。
5. 同一份更正按`correction_id`幂等：重复提交直接返回当前病例，不产生新版本或新审计记录（结论恢复后的迟到重发也不会再次打开病例）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
