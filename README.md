# amoo-status

轻量本地常驻服务。读取 Codex 本地会话状态，把汇总结果写入 `status.json`，
再更新 GitHub 的同名文件，供背屏工坊读取。Python 标准库实现，无 pip 依赖。

## 启动

环境：Python 3.10+、GitHub CLI。当前实机格式为 Codex CLI 0.162.1。
`gh` 必须登录具有仓库写入权限的账号。服务复用 `gh` 的凭据存储。

```powershell
# 单次采集，不上传
python statusd.py once

# 前台常驻：5 秒采集，60 秒合并上传变化，300 秒无变化心跳
python statusd.py run --publish

# Windows：当前用户登录启动，失败后每分钟重启，无控制台窗口
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/service.ps1 -Action Install
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/service.ps1 -Action Status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/service.ps1 -Action Restart
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/service.ps1 -Action Stop
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/service.ps1 -Action Uninstall
```

任务名为 `AmooStatus`，使用当前用户权限。登录触发器负责启动，每分钟触发器负责
检查；已有实例运行时忽略重复启动。`Stop` 会禁用任务，`Start` 会重新启用。
退出登录后不运行，重新登录后恢复。
休眠期间不采集，唤醒后继续。不保存 Windows 密码。
日志为 `.runtime/statusd.log`，轮转上限约 4 MiB。运行信息为 `.runtime/service.json`。
进程锁阻止同一运行目录内的重复实例。启用前停止旧的 JSON 写入程序。

支持 `--codex-home`、`--output`、`--runtime-dir`、`--repo owner/name`、`--branch`、
`--poll`、`--publish-interval`、`--heartbeat`、`--stale-after`。时间单位为秒。
默认读取 `CODEX_HOME` 或 `~/.codex`，默认上传 `ra1nzzz/amoo-status` 的 `main` 分支。

GitHub 上传使用 Contents API，仅更新远端 `status.json`。它不会暂存本地改动、
创建本地提交、切换分支或重置开发文件。SHA 冲突时刷新后重试一次。
网络失败时继续本地采集，下个上传周期重试。发现其他写入程序后记录错误，停止覆盖。

## JSON 兼容字段

```json
{
  "active": 3,
  "waiting": 1,
  "status": "waiting",
  "detail": "运行 2 / 等待 1 / 空闲 4",
  "updated": "2026-10-18T22:00:00+08:00"
}
```

| 字段 | 含义 |
| --- | --- |
| `active` | 主会话中运行、等待人工、状态过期的数量之和；包含 `waiting` |
| `waiting` | 有明确、尚未回复的结构化问题或持久化审批请求的主会话数量 |
| `status` | 有等待时为 `waiting`；否则有进行中会话时为 `developing`；否则为 `idle` |
| `running` | 最新回合进行中，且最近 300 秒有活动的主会话数量 |
| `idle` | 运行器仍加载、最新回合结束、没有检测到待回复问题的主会话数量 |
| `stale` | 活动过期、首次追赶日志或缺少回合数据的主会话数量 |
| `total` | 当前运行器加载的主会话总数；不包括历史记录或子代理 |
| `health` | `ok` / `degraded`，表示采集质量，不保证任务结果正确 |
| `providers.codex.subagents` | 单独统计子代理，不增加背屏的主会话数量 |
| `providers.codex.source.observed_at` | 最近成功采集时间；来源失败时保留该时间和旧数量 |
| `providers.codex.quota` | 脱敏的额度比例、窗口长度和重置时间 |

本地 JSON 用原子替换写入。采集失败时保留最近数量，并将 `health` 标为
`degraded`、来源标为 `unavailable`、`detail` 显示采集降级。
`updated` 是快照生成时间，来源的 `observed_at` 才是数据采集时间。

## 识别规则与限制

- 读取 `state_5.sqlite`、`thread_history_1.sqlite`、会话 JSONL。数据库以只读方式打开。
- 探测 `thread-writer-locks` 的操作系统锁。仅存在锁文件不算当前会话。
  运行器可以继续加载已结束的会话。`total` 不等于侧栏历史条目数或打开的标签数。
- 最新回合来自按会话索引的数据库查询。活动时间同时参考数据库和日志，兼容
  Windows 写入句柄长期打开导致文件修改时间延后的情况。
- 等待人工只匹配 `request_user_input`、`request_user_input_async` 及明确的审批事件。
  异步问题的 `accepted: true` 不表示用户回复。按问题编号消除等待。
  普通回复中的“请确认”不会猜成等待。
- **等待数量是可观察下限。** 部分桌面审批请求只存在内存中，本地文件无法完整观察。
  服务启动时最多读取最近两个回合的末尾 8 MiB，更早的未回复问题无法恢复。
  单条超过 8 MiB 的记录分段跳过。启动后持续增量读取。
- 进行中会话超过 300 秒无活动时标为 `stale`，不会猜成空闲。
  长时间工具执行也会触发此标记，可以提高 `--stale-after`。
- 本地格式不是稳定公共接口。模式不兼容时明确降级，不修改 Codex 数据。
- 公开 JSON 不包含会话 ID、标题、提示词、目录、账号标识、凭据或积分余额。
  它会公开会话数量、工作状态和额度比例。

## 额度方案

1. **默认本地快照：** 读取 `token_count.rate_limits`，输出使用比例、剩余比例、
   窗口长度、重置时间和观测时间。超过 15 分钟或窗口已重置时标为 `stale`。
   没有快照时为 `unavailable`，不会用 token 数量估算订阅额度。
2. **官方只读探测：** `python statusd.py probe-quota`。独立启动 App Server，
   只调用初始化与 `account/rateLimits/read`，随后退出。不启动模型回合。
   返回数据取决于本机登录方式及权限。服务默认不轮询此接口。
   本机于 2026-10-18 实测成功：返回周窗口使用率和重置时间。
3. **后续官方事件接入：** 同一运行器提供 `thread/loaded/list` 和
   `thread/status/changed` 时，可以读取精确运行状态与等待标记。
   独立启动的 App Server 无法代表桌面端进程。本机共享控制套接字返回 Windows
   错误 10050，当前使用本地文件。

接口说明：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。
MCP 的存在不等于它提供桌面全部会话的监控接口。本服务不依赖模型主动上报。

## Claude 与其他 Agent

`sources.Provider` 定义入口：`name` 和 `collect(now) -> Snapshot`。
新增提供器读取对应 Agent 的事件或本地文件，返回四类数量、子代理数量、
来源质量及脱敏额度。把它加入 `statusd.serve()` 的 `providers` 列表即可参与汇总。
当前只实现 Codex。Claude 的本地事件格式和额度授权尚未验证。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m py_compile statusd.py sources.py
```

测试覆盖锁、主会话与子代理分离、活动过期、Windows 文件时间延后、结构化问题与
异步答复、未完整写入的行、超大记录、来源失败、额度脱敏、原子写入、重复实例、
GitHub SHA 冲突和其他写入程序检测。实机记录见 `docs/verification.md`。
