# 发布说明（面向本仓库的维护者）

本文档服务于**本仓库**（BG4JTS 永久自有的分叉）的维护者，说明仓库的来历、后续怎么改、发布时注意什么。它**不是给使用者看的**：安装、配置、更新等面向最终用户的内容都在 README，那里才是用户入口。

## 一、上游来源与分叉说明

本仓库是 krijingle-create 的 `maibot-jev-timing-gate`（一个 MaiBot 插件）的衍生仓库：

- 上游仓库：<https://github.com/krijingle-create/maibot-jev-timing-gate>
- 本仓库：`origin` 为 <https://github.com/BG4JTS/maibot-jev-timing-gate>（走 `https://gh.927223.xyz/` 镜像前缀，见 `git remote -v`）

**永久独立、不跟踪上游**：本仓库自建立起即为独立维护、独立发布的永久自有分叉，不跟随上游更新，也**不配置 `upstream` remote（以后也不会加）**。

由此必然产生的后果：`gate_core.py` 会与上游逐步分叉。**自 v1.1.0 起，本仓库手写维护 `gate_core.py`**——它不再是"由仓库外生成器产出的生成物"（这条旧说法已废弃）。改逻辑直接改本文件，改完跑下面的离线用例。

对上游的归属予以保留：`LICENSE` 与 `_manifest.json` 中的作者署名、README 中的致谢均保留对 krijingle-create 的署名。上游项目自己维护了一条"发布到官方插件中心"的流水线（见其仓库），**与本仓库无关**，这里不沿用它。

## 二、自有仓库的维护约定

维护者改代码 / 改配置时遵守下面几条：

1. **改配置字段结构** → 同时递增 `config.py` 里的 `CONFIG_SCHEMA_VERSION` 与 `_manifest.json` 里的 `version`（版本号是三段式 semver，见下节）。只改逻辑、不动配置结构时，可只递增 `version`。
2. **`_manifest.json` 是 `extra="forbid"` 严格模式**：用到的字段必须在 SDK 的 manifest 模型里声明过，用到的能力必须写进 `capabilities`（宿主按能力令牌授权，未声明会被拒）。注意：**本仓库故意不添加 `changelog` 字段**——虽然 SDK 文档把它列为可选字段，但 manifest 文档禁止未声明字段，为避免校验失败，此处不用它。
3. **提交前必跑离线用例**：`python tests/test_gate_core.py` 与 `python tests/test_plugin_gate.py`（纯 python，无 pytest，无第三方依赖）。
4. **`config.toml` 与 `jev_config.json` 永不入库**（已在 `.gitignore`）：前者是运行时生成 / 用户填写的配置，后者可能含密钥。
5. **每个改动一个 conventional commit**：一次提交只做一件事，message 用 `type(scope): description` 格式（如 `fix(gate): ...`、`docs(...): ...`）。

## 三、本仓库自身的版本

- 插件 ID：`bg4jts.jev-timing-gate`
- 版本号：三段式 semver，记录在 `_manifest.json` 的 `version`。
- 变更历史不单列文件（不新增 `CHANGELOG.md`），靠 conventional commit 的 message 承载。

改动后的完整工作流：改 `config.py` + `_manifest.json` 版本 → 跑两个测试文件 → `git add` 相关文件（不含 `config.toml` / `jev_config.json`）→ 一个 conventional commit → push。
