# TagLedger

<p align="center"><img src="assets/tagledger-icon.svg" alt="TagLedger 项目图标" width="112"></p>

图标为本项目原创 SVG；素材来源见 [assets/README.md](assets/README.md)。

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB) ![Git 2.30+](https://img.shields.io/badge/Git-2.30%2B-F05032) ![MIT](https://img.shields.io/badge/License-MIT-green) ![无 Python 运行时依赖](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen)

TagLedger 为依赖仓库保存一份明确、可审阅的 Git tag 历史基线，之后检查这些 tag 名称是否仍指向同一 Git 对象。适合希望把 tag 历史记录提交到自身依赖配置旁边的维护者。

这是本地命令行工具。远端检查只读取 tag 引用；`verify` 不会 checkout、改写或推送 tag，也不会更新基线。

**它能发现漂移，但不能判断哪个版本才是真实可信的版本。** 首次保存基线前，请通过可信渠道核对来源和版本。

English version：[README.en.md](README.en.md) · [项目规格](PROJECT_SPEC.md) · [技术机制](TECHNICAL_INNOVATION.md) · [架构](ARCHITECTURE.md)

## 技术栈与支持范围

| 项目 | 当前声明 |
|---|---|
| 实现语言 | Python 3.11+；纯 Python，不含 C/C++ 扩展 |
| Git | 最低版本声明为 2.30；CI 尚未固定并验证这个最低版本 |
| 运行时 | Python 标准库与系统 Git；无第三方 Python 运行时依赖 |
| 构建 | setuptools 77.0.3+（仅构建时需要） |
| CI 目标 | GitHub Actions 的 Ubuntu/macOS/Windows 最新镜像 × Python 3.11、3.12、3.13、3.14；首次托管运行完成前不代表矩阵已通过 |
| 包版本 | 0.1.0 开发快照；尚无 PyPI 发布 |
| 许可证 | MIT；见 [LICENSE](LICENSE) |

本机验证：Windows build 26200、Git 2.54.0；源码测试在 Python 3.11.0 与 3.14.2 通过，wheel/sdist 构建、Python 3.14.2 隔离安装和 CLI 入口检查通过。Python 3.12–3.13、macOS/Linux 与声明的 Git 2.30 最低版本仍未验证；GitHub Actions 矩阵也尚未运行。

## 安装

需要 Python 3.11+ 和 Git 2.30+。

```powershell
python -m pip install .
tagledger --version
```

在源码 checkout 中不安装也可运行 CLI 帮助：

```powershell
$env:PYTHONPATH = "src"
python -m tagledger.cli --help
```

## 使用

先核实上游地址和 tag，然后记录基线：

```powershell
tagledger snapshot https://github.com/OWNER/REPOSITORY.git `
  --output .tagledger/repository.json `
  --mutable v1 --mutable v2 --mutable stable
```

如果确认仓库本来就没有 tag，可显式加 `--allow-empty`；默认空结果会被视为来源地址或筛选条件可能错误。

把 `.tagledger/repository.json` 与依赖变更一起提交。以后生成 HTML 与 SARIF 对比报告：

```powershell
tagledger verify .tagledger/repository.json `
  --html .tagledger/latest-report.html `
  --sarif .tagledger/latest-report.sarif
```

精确版本名（如 `v2.4.1`）默认按固定版本候选处理；`v2`、`stable` 这类可移动别名只有在生成基线时通过 `--mutable` 明确标记，才按可移动引用分类。名称规则只是启发式判断，请核对报告。

对本机 Git 仓库，工具通过 `for-each-ref` 读取 tag；对 URL 使用 `git ls-remote --tags`。如果仓库需要认证，则由本机已配置的 Git credential helper 处理。

远端来源只接受 HTTP、HTTPS、SSH、Git 协议和 SSH scp 写法（例如 `git@host:owner/repository.git`）。自定义 remote-helper 协议与 `file://` URL 会被拒绝；远端 Git 调用会设置明确的传输协议白名单。本地仓库请直接传文件系统路径，路径名可以包含 `#`。

`git://` 没有传输加密或服务器身份认证，建议优先使用 HTTPS 或 SSH；仅在可信网络上使用 `git://`。

不要把 token 或密码写进 source URL。TagLedger 会拒绝带内嵌用户名/密码的 HTTP(S) URL，并按常见凭据字段名启发式拒绝查询参数；这不是通用凭据扫描器，不能识别所有厂商自定义字段。SSH/scp 用户名会原样保存在 source 标识中，校验器不会把它识别为秘密，因此也不要把 token 或密码放在用户名字段。请改用 Git Credential Manager、其他 credential helper，或 SSH agent/key。

## 报告变化类型

- `target-moved`：同名 tag 指向了不同的最终目标对象。
- `tag-object-changed`：annotated tag 对象发生变化，但最终目标不变。
- `deleted`：基线中的 tag 当前不可见。
- `added`：基线之后出现的新 tag。
- 固定版本候选发生变化会让默认检查返回失败；可移动别名和未分类 tag 会标为待复核。

报告只包含 tag 名、源标识、时间戳和对象 ID，不输出 Git 错误详情或凭据 helper 输出。HTML 是独立文件，并对 tag 名与 source 做 HTML 转义。

基线会保存 source 标识、tag 名与 OID，以及 `--include`/`--mutable` 的自定义 glob；报告也可能包含 source 和变化的 tag 名/OID。这些内容可能暴露 Windows 用户名、目录结构、私有仓库名或内部命名习惯。相对本地路径相对于运行命令时的当前目录解析。提交基线或分享报告前，请逐项检查这些字段。不要把凭据放入 source URL。

退出码：`0` 没有错误级或警告级发现（可包含仅提示级的新 tag）；`1` 存在固定版本漂移或需要复核的警告；`2` 输入、Git 或报告写入错误。

## 更新基线

`verify` 不会写基线；如果报告路径指向基线、多个报告路径冲突或目标位于 Git 管理目录，程序会拒绝写入。基线和报告以同目录临时文件原子替换；阅读报告后，可把新基线按正常代码评审流程提交；也可以明确执行：

```powershell
tagledger advance .tagledger/repository.json --yes --accept-changes
```

每次替换都必须提供 `--yes`。若固定版本 tag 移动或消失，还必须提供 `--accept-changes`。

## 边界

- Git tag 名称本身没有不可变保证；基线只记录观察结果，不能证明首次记录可信。
- 为限制异常或超大输入的内存和运行时间，Git 输出最多读取 16 MiB，基线 JSON 不超过 64 MiB，tag 列表最多 100,000 项；查询 60 秒超时后会终止 Git 进程树。超过限制会安全失败；当前不适合超大 tag 仓库。source 中的控制字符会被拒绝。
- 对象 ID 不验证 release 附件、attestation、签名身份、构建来源或可复现构建。
- 工具不会自动扫描 GitHub Actions 的 `uses:`；一次检查只针对一个远端仓库。
- 网络错误会作为错误返回，不会被误报为所有 tag 已删除。

## 开发与测试

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

测试使用临时本地 bare Git 仓库和模拟的远端输出，不访问公共主机。更多细节见 [架构说明](ARCHITECTURE.md)、[贡献指南](CONTRIBUTING.md) 与 [性能记录](PERFORMANCE_NOTES.md)。

## 许可证

本项目采用 MIT 许可证，完整文本见 [LICENSE](LICENSE)。已知限制和当前支持范围以本 README、项目规格和架构说明为准。
