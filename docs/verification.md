# Verification — 2026-10-18

Environment: Windows, Python 3.12, Codex CLI 0.162.1. Repository:
`D:\Code\amo-status`. Runtime files are ignored by Git.

## Completed checks

- `python -m unittest discover -s tests -v`: 20 tests passed.
- `python -m py_compile statusd.py sources.py`: passed.
- PowerShell parser and live `scripts/service.ps1 -Action Install`: passed.
- Actual collection reads the user's live held thread locks and SQLite records.
  Root and subagent counts are separate. After initial bounded log catch-up,
  public collection health reached `ok`.
- GitHub Contents API successfully updated only `status.json` on `main`.
  Subsequent uploads appeared roughly one minute apart. Local collection continued
  between uploads; a blocked-upload test also verified that collection proceeds.
- `python statusd.py probe-quota`: successful at 22:30:12 Asia/Shanghai.
  Official `account/rateLimits/read` returned a 10080-minute window, 33% used,
  67% remaining, reset at 2026-10-25 08:40:38 +08:00. No model turn was started.
- Source output includes no raw transcript, account ID, credentials or credit balance.
- Windows task `AmooStatus` uses current-user interactive logon, no execution time
  limit, battery operation enabled, IgnoreNew, and a one-minute watchdog trigger.
- Forced termination at 22:39:04: old PID 29068 exited. The watchdog launched PID
  58580 at 22:40:02 without a manual start. Collection health returned to `ok`
  and publication succeeded. A sample working set was 27.5 MiB.

## Faults found and addressed

- Windows file modification time lagged behind database events while Codex kept a
  write handle open. Freshness now also uses indexed item timestamps.
- A live JSONL record exceeded 8 MiB. Oversized records are skipped incrementally
  instead of permanently blocking the reader. This limits waiting detection.
- Compacted logs contained obsolete database byte offsets beyond the current file
  size. Startup clamps the scan to a bounded valid tail.
- A forced process termination returned task result `0xFFFFFFFF` and did not restart
  with RestartOnFailure alone. A native repeating trigger now supplies recovery.

## Remaining validation

- Actual Windows logout/relogin or reboot was not performed. The logon trigger was
  inspected; this is configuration verification, not a reboot test.
- Memory-only approval prompts cannot be fully observed from local files.
- Claude integration is a documented provider boundary, not an implemented provider.
