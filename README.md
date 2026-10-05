# JABS Local Sync Agent

A standalone, cron-driven rsync agent for mirroring directories between
local/NFS-mounted paths on the same LAN host (e.g. two NAS mounts on the same
box, or between two locally-mounted shares). It follows the same conventions
as the other JABS agents (`snapshot_agent`, `nas_sync_agent`, etc.) and
reports every run to the [JABS Dashboard](../dashboard).

Unlike `nas_sync_agent` (built for syncing over Tailscale to a remote host,
with bandwidth limiting and a stop-time deadline), this agent assumes both
sides are reachable as ordinary local filesystem paths on a trusted LAN — no
bandwidth limiting, no compression, and no remote-shell/security concerns
apply. Also, unlike nas_sync_agent, this agent is one way.

## Quick Start

```bash
./jabs-agent.sh setup
# edit .env (JABS_DASHBOARD_URL, JABS_AGENT_KEY, SMTP creds)
# edit config/global.yaml
# edit/add config/jobs/*.yaml (see config/templates/job.yaml)
./jabs-agent.sh check           # sanity-check config/jobs before the first real run
```

Then add a cron entry for the scheduler (printed at the end of `setup`):

```
*/15 * * * * /path/to/local_sync_agent/venv/bin/python /path/to/local_sync_agent/scheduler.py > /dev/null 2>&1
```

## How it works

1. `scheduler.py` runs on a cron cadence (e.g. every 15 minutes) and checks
   every `config/jobs/*.yaml`'s `schedules[]` list for a due cron expression.
2. When a job is due, `backup.py`'s `run_job()` is called in-process (no
   extra subprocess spawn) for that job.
3. Each job may list multiple `paths` (source/destination pairs). Every pair
   is synced with `rsync` and reported to the dashboard **independently** as
   its own tracked job (`job_name:pair_name`) — a failure in one pair never
   stops the others in the same job.
4. After a successful (or partial) sync, a cheap **quick verify** runs
   automatically (unless disabled) — see below.
5. Every stage (start, running, verify, complete/error) is reported to the
   dashboard via `monitoring_client.py`, and immediate email alerts are sent
   per `config/global.yaml`'s `email.notify_on` settings.

## Job configuration

Each `config/jobs/<name>.yaml` (see `config/templates/job.yaml`):

```yaml
job_name: "example"
paths:
  - name: "documents"
    source: "/mnt/nas1/documents"
    destination: "/mnt/nas2/documents"
schedules:
  - cron: "0 2 * * *"
    enabled: true
exclude:
  - "Downloads/"
```

Global defaults (mirror behavior, timeouts, email) live in
`config/global.yaml`; per-job settings override them. See
`config/global-example.yaml` for the full list of options and comments.

## Integrity checks

- **Automatic, low-cost quick verify** — runs after every successful/partial
  sync (`verify_after_sync: true`, the default): `rsync --dry-run
  --itemize-changes`. This only compares file size/mtime metadata — no file
  content is read — so it's cheap enough to run on every job.
- **Manual, thorough deep verify** — reads and compares actual file content
  on both sides. Slow and I/O-heavy, so it's never run automatically:

  ```bash
  ./jabs-agent.sh check           # quick verify, all jobs
  ./jabs-agent.sh check --job example
  ./jabs-agent.sh check-deep      # deep verify, all jobs (slow)
  ./jabs-agent.sh check-deep --job example
  ```

Both are also available directly through `backup.py --check` /
`--full-check [--job NAME]`.

## rsync tuning for local/NFS paths

- `--whole-file` — skips rsync's delta-transfer (rolling checksum) algorithm.
  On a LAN/NFS mount, bandwidth isn't the bottleneck, so a straight whole-file
  copy is typically faster and lighter on CPU/I/O than computing block
  checksums on both sides.
- `--numeric-ids` — avoids NFS uid/gid → name resolution round trips.
- Ownership (`--owner`/`--group`) is **not** preserved by default — two NFS
  clients rarely share identical uid/gid mappings, so copying ownership
  across hosts usually just produces wrong owners. Enable
  `preserve_owner`/`preserve_group` only if this host and its mounts share a
  common identity source (LDAP/NIS/etc).
- `--partial --partial-dir=.rsync-partial --sparse --force` for resumable,
  reliable transfers.
- `--timeout` (`io_timeout` in config) detects a stalled NFS mount instead of
  hanging forever.
- No compression (`-z`) and no `--bwlimit` — this agent assumes LAN-speed,
  same-host filesystem paths, so compression only adds CPU overhead and
  bandwidth limiting isn't needed.
- rsync exit codes 23/24 (partial transfer — some files skipped/vanished) are
  treated as a warning, not a failure, matching `nas_sync_agent`'s convention.

## JABS agent monitoring

This agent posts to the dashboard's `/api/monitoring/events` endpoint (see
[../dashboard/AGENTS_API_GUIDE.md](../dashboard/AGENTS_API_GUIDE.md)). It must
be pre-registered on the dashboard's Agents page and its API key set as
`JABS_AGENT_KEY` in `.env`. Agent type: `Local Sync`.

While a sync is running, rsync's `--info=progress2` output is parsed for live
percent-complete and transfer rate, posted to the dashboard every ~5s
(throttled independently of the log, which only records every 10% crossed) so
the Agent Detail page can show a live progress bar.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Could not start job: ...` | Another run of the same job is still holding its lock file under `locks/` |
| `Source directory not accessible` | An NFS mount dropped, or the path in the job config is wrong |
| Job reports `partial` warnings | rsync hit vanished/unreadable files mid-run (exit 23/24) — check `sync.log` |
| Quick verify keeps finding mismatches | Something outside this agent is writing to the destination, or `delete` settings differ between the sync and verify pass |
| `rsync binary not found` | Install rsync, or set `RSYNC_BIN` in `.env` |

## Security

No encryption or authentication is used for the rsync transfer itself — both
source and destination are assumed to be local/NFS-mounted paths on a
trusted LAN, per this agent's design. The dashboard API key (`JABS_AGENT_KEY`)
and SMTP credentials are still handled like any other secret (kept in
`.env`, never committed).

## License

MIT — see [LICENSE.md](./LICENSE.md).
