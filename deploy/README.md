# Deploying

Three options, in order of how little there is to maintain.

## Docker Compose

```bash
cp .env.example .env            # fill in the credentials
cp profiles.toml.example profiles.toml
docker compose up -d
docker compose logs -f
```

The dashboard is published on `127.0.0.1:8000` only. The database lives in a
named volume (`tracker-data`), so it survives rebuilds.

Inside the container the dashboard binds `0.0.0.0` so the published port can
reach it, which means `WEB_TOKEN` must be set in `.env`. Compose only publishes
to the host loopback, but the token is the defence if that mapping is ever
widened.

Back up the database with:

```bash
docker compose exec tracker python -c "import shutil;shutil.copy('/data/tracker.db','/data/backup.db')"
docker compose cp tracker:/data/backup.db ./tracker-backup.db
```

## systemd

```bash
sudo useradd --system --home /opt/ebay-tracker tracker
sudo mkdir -p /opt/ebay-tracker && sudo chown tracker: /opt/ebay-tracker
sudo -u tracker git clone https://github.com/yasir-mo/ai-ebay-deal-tracker /opt/ebay-tracker
sudo -u tracker cp /opt/ebay-tracker/.env.example /opt/ebay-tracker/.env
# edit .env, then:
sudo cp /opt/ebay-tracker/deploy/ebay-tracker.service /etc/systemd/system/
sudo systemctl enable --now ebay-tracker
journalctl -u ebay-tracker -f
```

## Windows

`deploy/windows/run-tracker.cmd` is the launcher. It sets `PYTHONPATH`, loads
`.env`, and appends output to `tracker.log` in the repository root.

To start it at logon, run `deploy\windows\install-task.cmd` from an elevated
prompt. `schtasks` requires elevation, so without admin rights use the Startup
folder instead: create a file named `EbayDealTracker.cmd` in

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

containing two lines:

```
@echo off
call "C:\path\to\ai-ebay-deal-tracker\deploy\windows\run-tracker.cmd"
```

To remove the auto-start, delete that file, or run
`schtasks /delete /tn "EbayDealTracker" /f` if you used the task route.

## Directly

```bash
python -m tracker run          # daemon plus dashboard
python -m tracker web          # dashboard only, no credentials needed
```

## Reaching the dashboard remotely

The dashboard records purchases and edits what gets tracked, so it refuses to
bind anything other than localhost without a token. Two options:

**Preferred: an SSH tunnel.** Nothing is exposed, no token to leak.

```bash
ssh -N -L 8000:127.0.0.1:8000 you@your-server
```

**Otherwise: bind with a token.** Set `WEB_TOKEN` in `.env` and change the
compose port mapping to `8000:8000`. Then open
`http://your-server:8000/?token=YOUR_TOKEN` once; the token is stored in a
cookie for subsequent requests.

This is a shared secret over plain HTTP, so put it behind a reverse proxy with
TLS if the network is not one you control.

## Health checks

`GET /healthz` returns `200 ok` and is deliberately exempt from the token
check, because container and service health checks cannot supply one. It
returns a fixed string and exposes nothing about your data.

## Verifying a deployment

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/healthz   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/          # 401 with a token set
curl -s -H "X-Auth-Token: $WEB_TOKEN" http://127.0.0.1:8000/ | head -1   # the dashboard
```

A fresh deployment with placeholder credentials will log
`token request failed: 401 invalid_client` once per search per sweep. That is
the expected signal that eBay credentials still need filling in; the tracker
counts those as errors and keeps running rather than crashing.
