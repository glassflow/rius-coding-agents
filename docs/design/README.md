# Design records

How this plugin was designed and built, kept because the reasoning and the
defects found along the way explain choices the code alone doesn't.

These are historical documents. They describe the plugin as it was being
built, against a staging backend and under an earlier plugin name
(`rius-claude-code`). For current behaviour, hosts and commands, read the
[README](../../README.md) and [Getting started](../getting-started.md).

| Record | What it is |
|---|---|
| [Design spec](specs/2026-09-22-rius-claude-code-design.md) | The original design: span model, OTLP encoding, config ladder, privacy defaults |
| [Implementation plan](plans/2026-09-22-rius-claude-code.md) | The task-by-task plan the first version was built from |
| [Build log](build-log.md) | Rulings made during the build, every defect found and how it was caught |
| [Windows support](windows-support-handoff.md) | How the platform differences were isolated, and the residual risk |
