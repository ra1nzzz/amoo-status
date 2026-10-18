# amoo-status

背屏工坊读取的 Codex 状态源（由 `statusd.py` 定时写入）。

```json
{
  "active": 1,
  "waiting": 0,
  "status": "developing",
  "detail": "背屏工坊开发",
  "updated": "2026-10-18T19:46:35+08:00"
}
```

- `active`：进行中会话数量
- `waiting`：其中等待人工回答的数量
- `status`：`developing` 开发中 / `waiting` 等待人工回答 / `idle` 摸鱼中
