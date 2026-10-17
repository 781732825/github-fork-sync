# github-fork-sync

每周自动把一个 GitHub 账号下**所有 fork** 与其上游对齐的定时任务。

只做 fast-forward，不会重写历史；上游被删库或被清空时自动跳过，绝不把空仓库同步进来。

## 它做什么

```
列出账号下所有仓库
   └─ 非 fork（自建仓库）────────────────────→ 跳过
   └─ fork
        ├─ 上游不可访问（404 / 转私有）──────→ 跳过
        ├─ 上游没有 fork 跟踪的那个分支 ────→ 跳过
        ├─ 上游内容疑似被清空 / 替换 ───────→ 跳过
        ├─ 与上游一致 ─────────────────────→ 跳过（已最新）
        ├─ fork 领先上游 ──────────────────→ 跳过（上游没有新东西）
        ├─ 历史分叉（既有领先又有落后）────→ 跳过（fast-forward 会丢提交）
        └─ 只落后于上游 ───────────────────→ fast-forward 同步
```

每周一 03:17 UTC 执行一次，也可以在 Actions 页面手动触发。

## 安全护栏

同步动作指向的是你自己的仓库，但输入来自你无法控制的上游，所以每一步都先验证再动手。

| 护栏 | 触发条件 | 行为 |
| --- | --- | --- |
| 上游不存在 | `GET /repos/{upstream}` 返回 404 | 跳过。上游已删除或转为私有。 |
| 跟踪分支消失 | 上游没有 fork 默认分支的同名分支 | 跳过。上游把分支改名或删了（例如 `master` → `rm`），盲目同步只会失败或引入无关内容。 |
| 上游被清空 | 上游条目数 ≤ 1 | 跳过。这是"整个仓库被替换成一个 README"的典型形态。 |
| 上游大幅缩水 | fork 条目数 ≥ 20，且上游条目数 < fork 条目数 × 0.5 | 跳过。仓库被裁剪或替换，而不是正常演进。 |
| 无法读取上游文件树 | 目录树接口失败 | 跳过。凭据或权限异常时不盲动。 |
| 历史分叉 | compare 结果 `ahead > 0` | 跳过。fast-forward 会丢掉 fork 上的本地提交。 |
| 无共同祖先 | compare 返回 404 | 跳过。上游重写过历史，fast-forward 从原理上不可行。 |

条目数统计的是分支下所有文件与目录的条目总数。判断缩水时**只有 fork 自身条目数 ≥ 20 才做比例比较**，否则小仓库（3 个文件的脚本仓库）会被比例规则误伤。

### 关于"开发语言是否匹配"

脚本会读取双方的主语言（`GET /repos/{repo}/languages`，按字节数取最大的那个），两者不同时在该仓库的报告行尾追加 `[check: primary language differs, fork X vs upstream Y]` 作为提示。

它**不作为拦截条件**，原因有两点：GitHub 的主语言是按字节数启发式推断的，加一个 CI 配置文件就可能改变；项目本身也会迁移语言（上游从 Go 转 Rust、从 Python 转 TypeScript 都属正常演进）。据此拦截会拦住合法的同步。真正需要拦截的"上游已经不是同一个项目"，由上面的条目数护栏覆盖，那个信号噪声更低、更直接。

## 使用

### 1. 准备令牌

需要一个能读写你所有待同步仓库的令牌：

- **经典 PAT**：勾选 `repo`（公开+私有仓库的读写）和 `workflow`（同步涉及 `.github/workflows` 文件时必须，否则 push 会被拒）。
- **细粒度 PAT**：`Repository access` 选中所有待同步仓库，权限给 `Contents: Read and write` 与 `Workflows: Read and write`。

> 用 Actions 自带的 `GITHUB_TOKEN` 不行：它只对当前仓库有效，看不到你的其他仓库。

### 2. 放到你自己的仓库

把这个仓库 fork 或复制到你账号下，然后在 `Settings → Secrets and variables → Actions` 新建仓库密钥：

```
Name:   SYNC_TOKEN
Secret: <上一步的令牌>
```

### 3. 运行

- 自动：每周一 03:17 UTC。
- 手动：`Actions → Fork sync → Run workflow`。勾选 `dry_run` 可以只出报告不同步。

每次运行的完整报告写入 job summary，在 Actions 运行页面直接可读。

### 4. 本地运行

```bash
export GH_SYNC_TOKEN=ghp_xxxxxxxx
export GH_SYNC_DRY_RUN=true     # 先干跑
python3 sync_forks.py

unset GH_SYNC_DRY_RUN           # 确认无误后真正同步
python3 sync_forks.py
```

无需第三方依赖，Python 3.9+ 标准库即可。

## 配置

全部通过环境变量，不需要改代码：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GH_SYNC_TOKEN` | 必填 | 执行同步用的令牌，未设置时回退到 `GITHUB_TOKEN`。 |
| `GH_SYNC_OWNER` | 令牌所属账号 | 要同步哪个账号下的仓库。 |
| `GH_SYNC_WORKERS` | `6` | 并发请求数。仓库很多且被限流时调小。 |
| `GH_SYNC_DRY_RUN` | `false` | `true` 时只报告不同步。 |
| `REPORT_PATH` | 无 | 追加写 Markdown 报告的文件路径。 |

调度周期在 `.github/workflows/sync.yml` 的 `cron` 一行修改：

```yaml
on:
  schedule:
    - cron: '17 3 * * 1'   # 每周一 03:17 UTC
```

注意 GitHub 的 `schedule` 只在**默认分支**上生效，且高负载时可能延迟几分钟到几十分钟。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 全部成功（包括"已最新"和"跳过"） |
| 1 | 至少一个仓库同步失败，失败明细写到 stderr |
| 2 | 配置或凭据错误，例如令牌无效、无法列出仓库 |

失败会写入 job summary，需要重试时重新手动触发即可：脚本是幂等的，已经同步过的仓库下次会判定为"已最新"。

## 已知限制

- **只支持 fast-forward**。上游改过历史（强制推送、清空重建）时 fork 无法自动对齐，需要人工 `git reset --hard upstream/<branch>` 后强推。脚本会把这些仓库标成 `skipped` 并说明原因，不会静默忽略。
- **只处理默认分支**。fork 上的其他分支（例如上游的 `dev`、`release/*`）不会被同步。
- **不创建、不删除仓库**。护栏只影响"是否同步"，脚本本身没有删除仓库或分支的能力。
- **`skipped` 不等于出错**。上游已删除、已清空、fork 领先上游，都是预期内的跳过，不影响退出码。

## 许可

MIT，见 [LICENSE](LICENSE)。
