# Restart a service

Parameters: `namespace` (required), `deployment` (required), `lines` (optional, default 20).

1. Check the deployment.

```bash
kubectl get deploy {{deployment}} -n {{namespace}}
```

2. Read recent logs.

```bash
kubectl logs deploy/{{deployment}} -n {{namespace}} --tail={{lines=20}}
```

3. Restart it.

```bash
kubectl rollout restart deploy/{{deployment}} -n {{namespace}}
```
