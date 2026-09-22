# 参与 ForgeAgent 开发

感谢你愿意改进 ForgeAgent。本项目仍处于 Alpha 阶段，优先接受范围明确、可测试、不会削弱安全边界的改动。

## 开始之前

1. 先阅读 [README.md](./README.md)、[SECURITY.md](./SECURITY.md) 和 [AGENTS.md](./AGENTS.md)。
2. 在 Issue 中确认问题尚未被报告；较大的功能建议先讨论设计和验收标准。
3. 不要提交 API Key、真实会话、目标仓库私有内容、`.env`、`.forge/`、轨迹、虚拟环境或构建产物。
4. 安全漏洞不要创建公开 Issue，请使用 [GitHub Private Vulnerability Reporting](https://github.com/jensen-Zeng/ForgeAgent/security/advisories/new)。

## 本地开发

需要 Python 3.12+ 与 Git。完整的开发与 RLM 测试环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -c requirements\constraints.txt -e ".[dev,rlm]"
```

提交前运行：

```powershell
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m pytest --cov=forge_agent --cov-report=term-missing --cov-fail-under=80 -q
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

只运行受影响测试适合开发中快速反馈，但不能代替提交前完整回归。

## 改动原则

- 只修改解决当前问题所需的模块，避免顺手重构无关代码。
- 公共 Schema、CLI、环境变量、错误码或安全策略变化必须附带测试和文档。
- Provider 适配保持厂商对象隔离，Runtime 只依赖 ForgeAgent 自己的领域模型。
- 文件目标必须通过 `Workspace` 校验；命令风险由宿主策略决定，不能信任模型声明。
- Docker 不可用时，高风险命令必须失败关闭，不能自动降级到本地执行。
- 可信 RLM Kernel 拥有当前用户权限，不得把它描述为沙箱。
- Fake Provider、模拟 HTTP、参考评测和模拟 Docker 不能作为真实服务或真实隔离证据。

## Pull Request

PR 请保持主题单一，并说明：

- 问题和解决方案；
- 用户可见行为与兼容性影响；
- 安全边界是否变化；
- 实际运行的验证命令和结果；
- 尚未验证的外部环境或真实 Provider 条件。

项目使用 MIT License。提交代码即表示你有权贡献该内容，并同意按仓库许可证发布。
