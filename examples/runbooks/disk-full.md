# Disk full on a checkout-api node

Fictional runbook for the `checkout-api` service at example.com.
Alert: `NodeDiskUsageHigh` on a node in the `shop` namespace.

## Diagnose

1. Confirm which filesystem is full.

```bash
df -h
```

2. Find the pods on the affected node and check for restarts.

```bash
kubectl get pods -n shop -o wide
kubectl describe node worker-07.example.com
```

3. Look for a log flood in the last few minutes.

```bash
kubectl logs -n shop deploy/checkout-api --since=10m | grep -c ERROR
```

4. Check how much space old journal entries are using.

```bash
journalctl --disk-usage
```

## Mitigate

5. Trim the journal to free space quickly.

```bash
sudo journalctl --vacuum-size=500M
```

6. If a single pod is flooding the logs, restart the deployment.

```bash
kubectl rollout restart -n shop deploy/checkout-api
kubectl rollout status -n shop deploy/checkout-api
```

## Verify

7. Confirm disk usage is back under the alert threshold.

```bash
df -h
```
