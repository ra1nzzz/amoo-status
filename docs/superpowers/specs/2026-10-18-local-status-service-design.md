# Local status service

Approved design: Python standard library; poll Codex local files every five seconds;
preserve the back-screen JSON contract; count root sessions separately from subagents;
recognize human waiting only from explicit events; batch publication every minute;
run through a Windows logon scheduled task; leave a provider boundary for Claude.

## Data and behavior

- Open Codex SQLite databases in read-only mode. Query only loaded sessions identified
  by held thread writer locks; an existing but unlocked file is not a live session.
- Read the latest turn through its indexed SQLite query. Completed/interrupted turns
  are idle. An unfinished turn with no recent activity is stale, not proven healthy.
- Incrementally parse the current turn's JSONL events. Match structured question
  requests and replies by call ID. Never infer waiting from natural-language prose.
- Read quota windows from `token_count.rate_limits`. Preserve observation time and
  mark old samples stale. Export percentages and reset times, not credentials,
  account IDs, credits, prompts, session IDs, titles, or local paths.
- Aggregate `active` (running + waiting + stale), `waiting`, `status`, `detail`, and
  `updated`; add `running`, `idle`, `stale`, `total`, and provider health. Waiting
  takes display priority, then developing, then idle. Failures retain the last
  successful values and explicitly mark the result stale/degraded.
- Publish only `status.json` with GitHub's Contents API through the user's existing
  `gh` authentication. Use SHA compare-and-swap and bounded retries. Publication
  failure must not stop local collection. Do not manipulate the developer's index.
- Use a process lock, atomic JSON replacement, rotating local logs, and Windows
  logon plus one-minute watchdog triggers with IgnoreNew. RestartOnFailure alone
  did not recover a forcibly terminated process in the live test. Stop disables
  the task so the watchdog cannot undo an intentional stop. Network requests run
  off the collection loop and have deadlines. Runtime files are ignored.

## Limits and extensions

The supported current local format is Codex 0.162.1. The local storage schema is
internal and can change. Unsupported schemas fail visibly. Runtime-only approval
requests are not reliably persisted; local waiting is a lower bound. A provider
implements `collect(now)` and returns aggregate session state and sanitized quota.
Claude can later implement the same boundary using its own events or hooks.

Official App Server supports `thread/status/changed`, `thread/loaded/list`, and
`account/rateLimits/read`: https://learn.chatgpt.com/docs/app-server . An independent
app-server process cannot observe another process's loaded threads. This machine's
shared control socket failed with Windows error 10050, so local files are primary.
An opt-in read-only quota probe will test the official account method without
starting a model turn. No inference or account mutation is required.

## Implementation and validation

1. Implement the local provider and structured question parser.
2. Implement aggregation, atomic output, singleton operation, and publication.
3. Add the Windows task manager and operational documentation.
4. Test question matching, stale activity, locks, source failures, publication
   conflicts, atomic output, and privacy using synthetic data.
5. Check real local collection, official quota access, remote JSON publication,
   task restart, and continued background operation.

The user confirmed this design and confirmed stopping the previous writer.
