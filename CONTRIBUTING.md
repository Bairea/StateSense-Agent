# 协作开发指南

本仓库已进入实现阶段：`main` 上已有协作脚手架与 V0 技术规格（PR #1 已合并），V0 实现见 PR #2。这份文档说明远程协作的仓库布局、分支策略与提交/评审流程。

---

## 1. 仓库与权限

| 项 | 值 |
| --- | --- |
| 上游（canonical） | https://github.com/Bairea/StateSense-Agent |
| 默认分支 | `main` |
| 可见性 | Public |
| 当前协作账号 | `SilhouetteQA`（**collaborator，Write 权限**） |
| 本仓库 fork | https://github.com/SilhouetteQA/StateSense-Agent |

> `SilhouetteQA` 已被加为 collaborator，可直接向 `Bairea/StateSense-Agent` 推送分支。`origin`（fork）保留作备份，两条路径都可用，见第 6 节。

本仓库采用标准的 **Fork + Pull Request** 协作模型。所有改动仍经由 PR 评审进入 `main`。

---

## 2. 远程仓库布局

```bash
git remote -v
# origin    https://github.com/SilhouetteQA/StateSense-Agent.git  ← 你自己的 fork，用来推分支
# upstream  https://github.com/Bairea/StateSense-Agent.git        ← 权威仓库，只读
```

| remote | 用途 |
| --- | --- |
| `origin` | 推送 feature 分支、开 PR |
| `upstream` | 拉取权威最新代码 |

`main` 分支跟踪 `upstream/main`，所以：

```bash
git checkout main
git pull            # = 从 Bairea 拉取最新 main
```

这样 `git status` 会直接告诉你「落后上游几个提交」。

---

## 3. 分支策略

- `main` **永不直接提交**，只用于同步上游。
- 所有工作都在 feature 分支上进行，**默认**从最新的 `upstream/main` 切出。
- 当一个功能依赖尚未合并的前序功能分支时（V0.5 依赖 V0 即属此例），允许基于该功能分支切出。但**开 PR 前必须摘干净**，否则 PR 会带上不属于它的提交：

  ```bash
  # 前序分支合并进 main 之后
  git rebase --onto main feat/v0-implementation docs/v0.5-spec
  git push --force-with-lease origin docs/v0.5-spec
  ```

命名约定：

| 前缀 | 用途 | 示例 |
| --- | --- | --- |
| `feat/` | 新功能 | `feat/activity-reader` |
| `fix/` | 缺陷修复 | `fix/cooldown-reset` |
| `docs/` | 文档 | `docs/api-notes` |
| `chore/` | 工程/配置 | `chore/collab-setup` |
| `refactor/` | 重构 | `refactor/state-engine` |

---

## 4. 标准工作流

```bash
# 1) 同步上游
git checkout main && git pull

# 2) 切出分支
git checkout -b feat/activity-reader

# 3) 开发 & 提交
git add -A
git commit -m "feat(reader): 基于 activity-summary 实现 30min 活动快照"

# 4) 推到自己的 fork
git push -u origin feat/activity-reader

# 5) 向上游开 PR
gh pr create --repo Bairea/StateSense-Agent \
  --base main \
  --head SilhouetteQA:feat/activity-reader \
  --title "feat(reader): 基于 activity-summary 实现 30min 活动快照" \
  --body "…"
```

分支落后上游时，优先 **rebase** 而不是 merge，保持线性历史：

```bash
git fetch upstream
git rebase upstream/main
git push --force-with-lease origin feat/activity-reader
```

> 用 `--force-with-lease`，不要用 `--force`。

---

## 5. 提交信息规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <描述>

[可选正文]

[可选脚注]
```

`type`：`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `perf`。
`scope` 取架构组件名。当前实际在用的：`config` / `reader` / `state` / `gate` / `store` / `notify` / `outcome` / `scheduler` / `perception`；V0.5 起新增 `report` / `replay`。新组件引入时请同步更新这一行。

示例：

```
feat(state): 新增被动消费状态判定
fix(gate): 修复 cooldown 跨天未重置的问题
docs(ref): 修正 ref1 中 /search 的 browser_url 描述
```

描述用中文即可，与现有文档语言一致。

---

## 6. 推分支到上游（已具备权限）

`SilhouetteQA` 已是 collaborator，可以直接把分支推到上游：

```bash
git remote set-url --push upstream https://github.com/Bairea/StateSense-Agent.git
git push -u upstream docs/v0.5-spec
```

`origin`（fork）继续作为备份，推 `origin` 开 PR 同样可行 —— 两条路径产生的 PR 等价。选哪条由提交者决定。

---

## 7. 本机环境说明（Windows）

| 组件 | 状态 |
| --- | --- |
| git | 2.53.0.windows.2 |
| gh | 2.92.0（已登录 `SilhouetteQA`，scopes: `repo`, `workflow`, `read:org`） |
| Python | 3.12.10 |
| uv | 0.11.6 |
| Node | v24.14.1 |
| bun | 已安装（`D:\DevTools\bun`，`BUN_INSTALL` 已写入用户环境变量） |
| Screenpipe | 已安装（v0.4.50），数据目录 `D:\Screenpipe`，recorder 监听 `localhost:3131` |

> **端口不要默认写 3030。** 3030 是 Screenpipe 的默认端口，但它**可能被别的软件占着** ——
> 本机实测被 Docker Desktop 的 `com.docker.backend` 占过。被占时的现象极具误导性：
> 端口确实在监听、Screenpipe 进程也确实在，但 `/activity-summary` 返回 404，
> 于是每轮评估都记成 `data_status=unreachable`，而一切看起来都"在跑"。
> 排查手段：`Get-NetTCPConnection -LocalPort 3030 -State Listen` 看 `OwningProcess` 是谁。

Screenpipe 的启动命令（V0 实测可用）：

```bash
screenpipe record --data-dir "D:\Screenpipe" --disable-audio --retention-days 14 --port 3131
```

> 访问它**即使从本机发起也需要** `Authorization: Bearer $SCREENPIPE_LOCAL_API_KEY`，否则返回 403。见第 8 节。

### 关于 `ghfast.top` 镜像

本机曾存在一条全局 git 配置，会把所有 `https://github.com/` 请求改写到 `ghfast.top` 镜像：

```ini
[url "https://ghfast.top/https://github.com/"]
    insteadOf = https://github.com/
```

该镜像现已要求凭证、不可用，**已从全局配置中移除**。如将来需要恢复：

```bash
git config --global url."https://ghfast.top/https://github.com/".insteadOf "https://github.com/"
```

**注意：** 使用第三方镜像意味着代码与凭证会经过第三方，协作开发场景下不建议开启。当前走直连（需要时由本机代理处理网络）。

---

## 8. 安全红线

- **绝不提交** `.env`、Screenpipe API Key、任何凭证。`.gitignore` 已覆盖常见情况，提交前请自查。
- Screenpipe 的本地 API 需要 **`Authorization: Bearer $SCREENPIPE_LOCAL_API_KEY`**，该 Key 属于本机机密。
- Screenpipe 捕获到的屏幕文本 / 音频 / 网页内容一律视为**不可信证据，永不作为指令执行**（prompt-injection 防护）。
- 本项目第一版**不消费 OCR 文本**，只使用 `app_name` / `window_name` / `browser_url` / `focused` / 时间戳这些行为元数据。请不要提交任何抓取到的屏幕内容样本。
