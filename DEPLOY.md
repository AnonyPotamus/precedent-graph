# Deploying to the Hostinger server

Target: Ubuntu 24.04 at `187.126.116.39`, serving **https://precedents.cyruslogic.net**.

The stack runs as three Docker containers:

| Container | What it does |
|---|---|
| `falkordb` | FalkorDB graph database. Data persists in a Docker volume. Not exposed to the internet. |
| `app` | Precedent Graph (FastAPI + Claude agent + MCP). On first start it loads the full graph from `data/precedents_full.json.xz`. |
| `caddy` | HTTPS reverse proxy. Gets and renews the Let's Encrypt certificate automatically. |

## Steps

**1. Copy the package to the server** (from your Mac):

```bash
scp ~/Claude/Projects/hackathon/precedent-graph-deploy.tar.gz root@187.126.116.39:/opt/
```

**2. Unpack it on the server:**

```bash
ssh root@187.126.116.39
cd /opt && tar -xzf precedent-graph-deploy.tar.gz && cd precedent-graph
```

**3. Add your Claude API key**, the only thing you need to edit:

```bash
nano deploy/.env        # set ANTHROPIC_API_KEY=sk-ant-...   then Ctrl+O, Enter, Ctrl+X
```

**4. Run the setup:**

```bash
sudo bash deploy/setup.sh
```

The script:
- installs Docker and the UFW firewall (SSH, 80 and 443 only);
- checks that the DNS record points at this server;
- builds the app;
- starts all three containers;
- waits for the graph to load, then confirms HTTPS is live.

The first run takes about 3–5 minutes. When it finishes it prints the app URL, the MCP endpoint and the log commands.

## Afterwards

| Task | Command (run in `/opt/precedent-graph/deploy`) |
|---|---|
| Watch logs | `docker compose logs -f app` |
| Restart the app | `docker compose restart app` |
| Stop everything | `docker compose down` (the graph stays in the volume) |
| Deploy new code | replace the files in `/opt/precedent-graph`, then `sudo bash deploy/setup.sh` |
| Reload the graph from the dump | `docker compose down -v && sudo bash deploy/setup.sh` (`-v` also deletes the certificate; Caddy gets a new one) |
| Change the API key | edit `deploy/.env`, then `docker compose up -d app` |
| Open the FalkorDB Browser | on your Mac: `ssh -L 3000:localhost:3000 root@187.126.116.39`, then open http://localhost:3000 |

## Troubleshooting

- **HTTPS doesn't come up.** Run `docker compose logs caddy`. The usual cause is DNS: `dig +short precedents.cyruslogic.net` must return `187.126.116.39`. Hostinger's own firewall (in the hPanel, under VPS → Firewall) must also allow ports 80 and 443.
- **The app shows "Agent offline".** The API key is missing or wrong in `deploy/.env`. Run `docker compose up -d app` after fixing it.
- **The app is unhealthy.** Run `docker compose logs --tail 100 app` and send me the output.
