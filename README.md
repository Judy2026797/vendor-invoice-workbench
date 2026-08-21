# 供应商发票工作台（防重复付款）

独立的供应商发票登记 / 查重 / 录入工具。面向多员工每月录入发票并关联 BPM 付款申请单号，核心目标是 **防止同一张发票被重复付款**。

## 特性

- **硬阻断去重**：相同 `(发票号, 供应商)` 不允许重复录入，系统直接拒绝，不接受"强制覆盖"。
- **角色权限**：
  - `admin` — 全量读写（增删改 + 标记付款）
  - `staff` — 可录入 / 编辑发票，不可删除或改付款状态
  - 其他 — 只读
- **BPM 付款单号**：每张发票可登记关联的 BPM 付款申请单号。
- **批量导入**：`scripts/import_vendor_xlsx.py` 解析 `Vendor Payment Data.xlsx` 并通过 API 导入（同样遵守去重）。
- **CSV 导入 / 导出**：前端一键操作。

## 部署

```bash
# 1. (可选) 创建虚拟环境并安装依赖
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. 设置 admin 密码（务必使用环境变量，不要写死）
export FIN_ADMIN_USER=admin
export FIN_ADMIN_PASS='你的强密码'

# 3. 启动（默认端口 8137）
python server.py 8137
```

首次访问 `http://127.0.0.1:8137/` 会被引导到登录页，用上面设置的 admin 账号登录。
数据库 `workbench.db` 在首次启动时自动创建（`vendor_invoices` + `users` 两张表）。

## 目录结构

```
server.py                      # 后端：鉴权 + 供应商发票 API + 静态服务
login.html                     # 登录页
index.html                    # 前端主页面（仅供应商发票模块）
assets/css/style.css
assets/js/app.js              # 前端逻辑：列表 / 录入 / 去重拦截 / CSV
scripts/import_vendor_xlsx.py # xlsx 批量导入器
docs/                         # 相关文档（如有）
```

## API 速览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录，返回 `Set-Cookie` |
| POST | `/api/logout` | 退出 |
| GET  | `/api/me` | 当前用户 |
| GET  | `/api/vendor-invoices` | 列表（支持 `vendor/period/pay_status/q` 过滤） |
| GET  | `/api/vendor-invoice-check` | 查重（`invoice_no` + `vendor`） |
| POST | `/api/vendor-invoice` | 新增/编辑；重复返回 `{ok:false, duplicate:true}` |
| POST | `/api/vendor-invoice-delete` | 删除（admin） |
| POST | `/api/vendor-invoice-pay` | 标记付款（admin） |
| POST | `/api/vendor-invoice-import` | 批量导入（遵守去重） |

## 安全说明

- 仓库 **不包含任何真实数据或凭据**：`workbench.db`、`*.db`、`sonal_service_account.json`、`data/`、`backups/`、`.env` 均被 `.gitignore` 排除。
- admin 密码只从环境变量 `FIN_ADMIN_PASS` 读取；未设置时随机生成并打印到启动日志（请尽快修改）。
- 会话 token 保存在内存，重启需重新登录。
