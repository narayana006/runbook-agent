# runbook-agent

Turn Markdown runbooks into agent-executable steps: read-only diagnostics run automatically, changes require human approval. Ships as an MCP server.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A MCP server and command-line tool.

**The problem it solves:** Runbooks are stale prose that on-call engineers execute by hand at 3am.

## Who it is for

On-call teams whose runbooks are Markdown documents that people follow by hand.

## How it will work

1. The tool parses each runbook into individual steps.
2. It marks every step as read-only (safe to run) or as making a change.
3. An agent runs the read-only checks automatically and stops for human approval before any change; everything is logged.

## Planned features

Unticked items are not built yet.

- [ ] Parse Markdown runbooks into structured steps
- [ ] Classify steps as read-only vs mutating
- [ ] Auto-run diagnostics, require approval for changes
- [ ] Full audit log of what ran and why
- [ ] MCP server so any agent client can use it

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
runbook-agent serve ./runbooks
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## License

[MIT](LICENSE)
