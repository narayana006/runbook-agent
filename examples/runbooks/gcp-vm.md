# GCE VM is unreachable

1. Check which project the CLI is pointed at.

```bash
gcloud config list
```

2. List instances in the zone.

```bash
gcloud --project shop-prod compute instances list --zone europe-west1-b
```

3. Check the VM answers on the network.

```bash
ping -c 3 10.0.0.12
```

4. Reset the VM.

```bash
gcloud --project shop-prod compute instances reset web-1 --zone europe-west1-b
```
