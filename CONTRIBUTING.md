# 协作开发指南

本仓库已进入实现阶段：`main` 上已有协作脚手架与 V0 技术规格（PR #1 已合并），V0 实现见 PR #2。这份文档说明远程协作的仓库布局、分支策略与提交/评审流程。

> **2026-09-22 按事实修订。** 本节的旧版写着「origin 是 `SilhouetteQA` 的 fork，
> `upstream` 只读」，与当前 clone 不符：本机 `origin` 就是
> `Bairea/StateSense-Agent`（canonical），登录账号是 `Bairea`。V0/V0.5 那批提交
> 确实是由协作账号经 fork 提 PR 完成的，那是历史；现在的流程见下面各节。

---

## 1. 仓库与权限

| 项 | 值 |
| --- | --- |
| 仓库（canonical） | https://github.com/Bairea/StateSense-Agent |
| 默认分支 | `main` |
| 可见性 | Public |
| 当前账号 | `Bairea`（**owner**） |
| remote | 只有 `origin`，指向上面这个 canonical 仓库 |

本机没有 fork、也不需要：分支直接推到 `origin`，用 PR 合入 `main`。
**`main` 仍不接受直接提交** —— 走 feature 分支加 PR 的理由不是权限，是可追溯：
每个改动的验证结果与决定都留在 PR 里，回看时能对上。

---

## 2. 远程仓库布局

```bash
git remote -v
# origin  https://github.com/Bairea/StateSense-Agent.git (fetch)
# origin  https://github.com/Bairea/StateSense-Agent.git (push)
```

| remote | 用途 |
| --- | --- |
| `origin` | 拉取最新 `main`、推送 feature 分支、开 PR |

`main` 跟踪 `origin/main`，所以：

```bash
git checkout main
git pull            # 拉取最新 main
```

这样 `git status` 会直接告诉你「落后远端几个提交」。

---

## 3. 分支策略

- `main` **永不直接提交**，只用于同步远端。
- 所有工作都在 feature 分支上进行，从最新的 `origin/main` 切出。
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
# 1) 同步远端
git checkout main && git pull

# 2) 切出分支
git checkout -b feat/activity-reader

# 3) 开发 & 提交（用显式路径，别用 git add -A —— .gitignore 里那条血的教训）
git add src/statesense/activity tests/test_activity_reader.py
git commit -m "feat(reader): 基于 activity-summary 实现 30min 活动快照"

# 4) 推分支（同仓，不经过 fork）
git push -u origin feat/activity-reader

# 5) 开 PR，base main
gh pr create --repo Bairea/StateSense-Agent \
  --base main \
  --head feat/activity-reader \
  --title "feat(reader): 基于 activity-summary 实现 30min 活动快照" \
  --body "…"
```

分支落后 `main` 时，优先 **rebase** 而不是 merge，保持线性历史：

```bash
git fetch origin
git rebase origin/main
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
`scope` 取架构组件名。当前实际在用的：`config` / `reader` / `state` / `intervention` / `store` / `notify` / `outcome` / `scheduler` / `perception` / `cli`（入口层 `__main__.py`）；V0.5 起新增 `report` / `replay`；阶段 1.1 起新增 `rulebook`（判定规则版本）。`docs` / `chore` 类提交往往不对应单一组件，用宽泛 scope（如 `spec` / `plan` / `gitignore`）或省略不写均可。新组件引入时请同步更新这一行。

示例：

```
feat(state): 新增被动消费状态判定
fix(gate): 修复 cooldown 跨天未重置的问题
docs(ref): 修正 ref1 中 /search 的 browser_url 描述
```

描述用中文即可，与现有文档语言一致。

---

## 6. 推送分支

同仓直推，没有 fork 与 upstream 两套 remote 要区分：

```bash
git push -u origin feat/activity-reader
```

首次推送后 `gh pr create` 会自动用这个分支作 head；之后的提交再推一次即可
自动更新 PR。**不要推 `main`，也不要对 `main` 用 `--force`。**

---

## 7. 本机环境说明（Windows）

| 组件 | 状态 |
| --- | --- |
| git | 2.36.1.windows.1 |
| gh | 2.92.0（已登录 `Bairea`，scopes: `repo`, `workflow`, `read:org`, `gist`） |
| Python | `uv run` 下的解释器是 3.13.12（`pyproject.toml` 要求 >=3.12） |
| uv | 0.11.16 |
| Node | v24.14.1 |
| bun | 已安装（`D:\DevTools\bun`，`BUN_INSTALL` 已写入用户环境变量） |
| Screenpipe | 桌面版装过：数据目录是**默认的** `C:\Users\<用户>\.screenpipe`（2026-09-22 实测 211 MB，最后活动 2026-09-15）。`D:\Screenpipe` **不存在**，`screenpipe` CLI 不在 PATH 上 |

> **⚠️ 2026-09-22 实测修正。** 上面这一行原写作「已安装 v0.4.50，数据目录 `D:\Screenpipe`，
> recorder 监听 `localhost:3131`」——那是另一台机器/另一套部署的状态，在本机不成立。
> 照旧文执行下面的启动命令会**新建一个空的 `D:\Screenpipe`**、把历史记录留在原地，
> 而且现象很像「一直在跑、只是没数据」。
> **先确认数据目录**（看已存在的 `.screenpipe` 在哪，或 `screenpipe --help` 的默认值），
> 再决定 `--data-dir`；不确定就**不要**加这个参数，用默认值。

> **端口不要默认写 3030。** 3030 是 Screenpipe 的默认端口，但它**可能被别的软件占着** ——
> 本机实测被 Docker Desktop 的 `com.docker.backend` 占过。被占时的现象极具误导性：
> 端口确实在监听、Screenpipe 进程也确实在，但 `/activity-summary` 返回 404，
> 于是每轮评估都记成 `data_status=unreachable`，而一切看起来都"在跑"。
> 排查手段：`Get-NetTCPConnection -LocalPort 3030 -State Listen` 看 `OwningProcess` 是谁。

Screenpipe 的启动命令（V0 实测可用，**`--data-dir` 必须换成实际在用的那个目录**）：

```bash
screenpipe record --data-dir "<实际数据目录>" --disable-audio --retention-days 14 --port 3131
```

recorder 起来后先用一条命令确认「端口在听、且归属是 Screenpipe」：

```bash
Get-NetTCPConnection -LocalPort 3131 -State Listen | Select-Object OwningProcess
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
