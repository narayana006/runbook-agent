# runbook-agent

> Turn Markdown runbooks into agent-executable steps: read-only diagnostics run automatically, changes require human approval. Ships as an MCP server.

**Status:** 🚧 Early stage — design and roadmap published, implementation in progress. Stars, issues and ideas welcome.

## Why

Runbooks are stale prose that on-call engineers execute by hand at 3am.

## Planned features

- [ ] Parse Markdown runbooks into structured steps
- [ ] Classify steps as read-only vs mutating
- [ ] Auto-run diagnostics, require approval for changes
- [ ] Full audit log of what ran and why
- [ ] MCP server so any agent client can use it

## Intended usage

```
runbook-agent serve ./runbooks
```

_The interface above is the target design and may change._

## Roadmap

1. Core implementation of the first feature set
2. Examples and tests
3. CI and first tagged release
4. Docs and integrations

## Contributing

Contributions are welcome. Open an issue to discuss an idea or pick an unchecked item from the feature list.

## License

[MIT](LICENSE)
