# Hello runbook

A harmless runbook you can execute for real to see how approval works.
It only uses `echo`, `date` and `uname`.

## Checks

1. Print the kernel name.

```bash
uname -s
```

2. Pipes between read-only commands are fine.

```bash
echo "three two one" | tr " " "\n" | sort
```

## Change

3. Write a marker file (redirection writes to disk, so this needs approval).

```bash
echo "runbook-agent was here" > runbook-agent-demo.txt
```

## Verify

4. Read the marker file back.

```bash
cat runbook-agent-demo.txt
```
