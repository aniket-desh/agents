# Native conversation integrations

These adapters keep research in the ordinary agent conversation. They neither
start another coding agent nor call a model API. SessionStart context supplies
the exact native conversation ID and workspace; the research skill binds an
explicitly requested investigation to that identity. The controller rejects a
different conversation or workspace, and an investigation has one active driver.

Use `research-compute session driver ID native` when a native Goal or scheduled
task owns continuation. Installed Stop hooks then return no continuation, so
they cannot revive a paused Goal. Use `hook` for the installed hook/extension
driver. The agent makes this choice as part of the user's ordinary request.

| Agent | Integration and limits |
|---|---|
| Codex | SessionStart and Stop command hooks. Review their trust in native `/hooks`; the installer does not bypass it. Prefer a native Goal for persistent work when available, selecting `native` first. |
| Claude Code | SessionStart and Stop command hooks. The native consecutive-Stop limit is currently eight. Longer unattended work needs an available in-session scheduled task or task-completion wakeup; select `native` for scheduled continuation. |
| Pi | An extension under the agent directory's `extensions/`, requiring the current `agent_before_settle` API. It continues at that boundary or polls admitted jobs and sends a follow-up only while the exact conversation remains idle. Use driver `hook`. |

Stop hooks do not wake idle Codex/Claude conversations after a job finishes.
The agent must wait in its existing tool/task flow or arrange a supported native
wakeup before yielding. Hooks fail open for conversation stopping when the
controller is unavailable, the binding is absent, or the controller refuses
continuation; independent GPU leases and the watchdog still govern compute.

The Python hook tests exercise exact binding, refused/waiting decisions,
unrelated lifecycle events, and missing-service behavior. The Pi test executes
the actual extension source with a mock of its documented API, checking native
continuation, waiting, pause, interruption, switching, and queued user input.
These are local mock/process checks, not live vendor-session or GPU validation.

Sources checked 2026-09-29: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex Goals](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex),
[Claude hooks](https://code.claude.com/docs/en/hooks),
[Pi extension API](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/extensions/types.ts),
and [Pi configuration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/configuration.md).
