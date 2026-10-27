# 任务完成报告：订单读取所有权校验与调用方适配

## 1. 路径判定依据
**路径依据**：涉及安全、鉴权与授权高风险边界（数据对象级所有权校验、水平越权防御与管理员角色豁免），且改动公共契约 [`get_order`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/orders.py#L6-L18) 并影响多个独立调用方（[`app/api.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/api.py) 与 [`app/admin.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/admin.py)），按分流规则进入 `full`。因用户明确要求禁止在工作区创建非产品源码文件（如 `design.md`、`ledger.md`、`progress.md`），设计与账本信息在当前上下文及最终交付报告中记录，不落盘工作区。

## 2. 模型配置与能力状态
- **配置来源**：选中用户配置 `~/.config/easydev/models.json`（`developer`=`opencode-go/deepseek-v4.1-flash`, `reviewer`=`codex/gpt-6.1-sol`, `designer`=`codex/gpt-6.1-sol`）。
- **生效核对与能力降级**：宿主工具 `invoke_subagent` 的 `Model` 参数仅支持枚举 `["inherit", "flash_lite", "flash", "pro"]`，不支持配置指定的 `codex/gpt-6.1-sol`。按 `setup.md` 规则不静默替换模型，暂停独立审查委派，降级为同一实例对抗式自查并交付审查包；状态标记为“实现与自查完成，独立审查待补”。

## 3. 改动内容
仅修改产品源码（共 3 个文件），未修改测试、脚本或文档：
1. [`app/orders.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/orders.py#L6-L18)：
   - 给 [`get_order`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/orders.py#L6-L18) 增加所有权与豁免参数：`get_order(order_id, user_id=None, as_admin=False, bypass_ownership=False, operator=None)`。
   - 当订单不存在时保持返回 `None`；
   - 当具备管理员权限（`as_admin=True` 或 `bypass_ownership=True` 或 `operator["role"] == "admin"`）时直接返回订单，跳过所有权限制；
   - 否则校验 `user_id`：若未传 `user_id` 或 `order["owner_id"] != user_id`，抛出 `PermissionError`。
2. [`app/api.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/api.py#L5-L22)：
   - 定义 `STATUS_FORBIDDEN = 403`；
   - [`handle_show_order`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/api.py#L9-L22) 从 `request["user_id"]` 读取当前认证用户（容错处理缺失情况）；
   - 调用 [`get_order(order_id, user_id=user_id)`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/orders.py#L6-L18)；
   - 捕获 `PermissionError` 时返回 `{"status": 403}`（只含 status，不带订单内容）；
   - 订单不存在时返回 `{"status": 404}`；正常匹配时返回 `{"status": 200, "order": order}`。
3. [`app/admin.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/admin.py#L6-L11)：
   - 适配运营导出调用方：保持原有的 `operator` 具备 `admin` 角色校验（非 admin 抛出 `PermissionError`）；
   - 调用底层契约时显式传入 `as_admin=True`，使合法的运营导出能够豁免下单人所有权限制。

## 4. 验证证据（红绿对比与对抗式核对）

### 修复前（红）
- 契约检查 `python3 check_orders.py`：
  ```text
  FAIL
  - owner reads own order: expected {'id': 'o1', 'owner_id': 'u1', 'total': 120}, got 'TypeError'
  - other user denied: expected 'PermissionError', got 'TypeError'
  - missing order returns None: expected None, got 'TypeError'
  ```
- 越权探针 `python3 probe.py 403`：
  ```text
  probe: u2 reads o1 -> 200 (expected 403)
  [Exit code 1]
  ```

### 修复后（绿）
- 契约检查 `python3 check_orders.py`：
  ```text
  PASS: 3/3 ownership checks
  [Exit code 0]
  ```
- 越权探针 `python3 probe.py 403`：
  ```text
  probe: u2 reads o1 -> 403 (expected 403)
  [Exit code 0]
  ```
- 完整调用方与边界对抗式验证（19 项断言全部通过）：
  - `get_order("o1", "u1")` 返回订单 `o1`；
  - `get_order("o1", "u2")` 抛出 `PermissionError`；
  - `get_order("o1")`（缺省 user_id）抛出 `PermissionError`；
  - `get_order("nope", "u1")` 及 `get_order("nope")` 返回 `None`；
  - `get_order("o1", as_admin=True)` 返回订单 `o1`；
  - `handle_show_order({"user_id": "u1"}, "o1")` 返回 `{"status": 200, "order": {...}}`；
  - `handle_show_order({"user_id": "u2"}, "o1")` 返回 `{"status": 403}`；
  - `handle_show_order({"user_id": "u1"}, "nope")` 返回 `{"status": 404}`；
  - `handle_show_order({"user_id": "u2"}, "nope")` 返回 `{"status": 404}`；
  - `handle_show_order({}, "o1")` 返回 `{"status": 403}`；
  - `export_order("o1", {"role": "admin"})` 返回订单 `o1`；
  - `export_order("o2", {"role": "admin"})` 返回订单 `o2`；
  - `export_order("nope", {"role": "admin"})` 返回 `None`；
  - `export_order("o1", {"role": "user"})`、`export_order("o1", {})`、`export_order("o1", None)` 均正确抛出 `PermissionError`。

## 5. 审查包与交付状态
- **审查包**：
  - 基线版本：`570ff5f7913eaf2a57817e37708f418f04b9b120`
  - 改动文件：[`app/orders.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/orders.py)、[`app/api.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/api.py)、[`app/admin.py`](file:///private/tmp/easydev-replay-2026-10-27/runs/public-contract-callers/easydev/r1/workspace/app/admin.py)
  - 交付状态：实现与自查完成，独立审查待补（受限于宿主模型分派接口）。

计量：模块=main:routing.md,orchestration.md,design.md,models.md,setup.md,code-quality.md,reviewer-role.md；dev:无；reviewer:无；审查=0；d=0/3；r=0/5
