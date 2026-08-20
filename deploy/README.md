# Deploying

Three options, in order of how little there is to maintain.

## Docker Compose (recommended)

```bash
cp .env.example .env            # fill in the credentials
cp profiles.toml.example profiles.toml
docker compose up -d
docker compose logs -f
```

The dashboard is published on `127.0.0.1:8000` only. The database lives in a
named volume (`tracker-data`), so it survives rebuilds.

Back it up with:

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
