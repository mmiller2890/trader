# Unattended Operations Runbook

This runbook covers multi-day operation with durable Telegram alerts, a
72-hour live operating lease, supervised tasks, and guarded halt recovery.
It complements `docs/live-runbook.md`; nothing here overrides the global
rule: **never clear the kill switch before the named authoritative check
passes.**

## Alert levels and first response

| Level | Meaning | First response |
|---|---|---|
| URGENT | Trading is halted/failed, accounting or safety invariant broke, lease expired, resume rejected | Open the matching playbook below within minutes |
| WARNING | Degraded dependency, reconnects, disk >80%, backlog, lease <24h | Inspect dashboard; plan intervention if it persists |
| INFO | Start, safe auto-resume, recovery completed, daily summary | No action |

Every urgent alert names its category in the message; each category maps to
one playbook section below.

## Shared procedure for every playbook

1. Open the dashboard (`ssh -L 8000:127.0.0.1:8000 host`, then
   `http://127.0.0.1:8000`). Confirm the runtime badge shows `HALTED` /
   `FAILED` and read the kill-switch reason.
2. Preserve evidence before any restart:
   - `data/snapshots/state.json` (latched kill switch + positions)
   - `data/journal/events.jsonl` (recent rotated files included)
   - `data/bot.sqlite3` tables: `operational_incidents`,
     `notification_outbox`, archived rows.
   ```bash
   cp -a data/snapshots data/journal /safe/place/
   sqlite3 data/bot.sqlite3 ".backup /safe/place/incident-$(date +%s).sqlite3"
   ```
3. Verify on the exchange (authoritative), not the dashboard: open orders,
   positions, fills for the affected market/token via the CLOB web UI or API.
4. Cancel anything unsafe manually only after step 3 confirms what remains
   open. The bot already attempted cancellation during halting.
5. Follow the playbook's "resolution" check.
6. Clear the latch only through the dashboard **Clear halt** action with
   exact confirmation `CLEAR HALT <suffix>`. It re-verifies preflight,
   reconciliation, persistence, disk, open orders, and position safety, then
   resolves exactly that incident. It never starts trading and never
   restores the revoked lease.
7. Re-authorize trading: run **Preflight**, send **Telegram test** (must be
   delivered within five minutes), then issue a new lease through
   **START LIVE** with the exact confirmation.

## Playbooks

### 1. Authentication, signature, geoblock

Alert fields: component `preflight|clob`, reason
`authentication|signature|geoblock`. Checks: API credentials valid on
Polymarket (re-derive L2 creds), system clock synced (`timedatectl`),
region not blocked (`https://polymarket.com/api/geoblock`), wallet funded
for gas-free proxy ops. Resolution: a fresh dashboard **Preflight** passes
all credential/geoblock checks. Rotate any exposed key before continuing.

### 2. Confirmed reconciliation/accounting divergence

Alert fields: category `account_divergence|accounting`, second confirmation
or invariant name. Checks: exchange orders/fills vs
`data/snapshots/state.json` checkpoints; identify the diverging order key.
Resolution: exchange truth matches local confirmed state after manual
verification of every fill delta for that identity. If exchange shows fills
the bot never applied, record them manually and verify totals before
clearing.

### 3. Unprotected position / exhausted exits

Alert fields: category `exit_safety`, market end time in message. Checks:
is the position still open at the exchange? Did the market resolve?
Resolution: position closed (manually or by resolution) OR a verified exit
path exists with fresh data. Never clear while an unprotected position can
still be traded.

### 4. Cancellation failure

Alert fields: reason `cancel_failed:<type>`. Checks: list open orders at
the exchange; cancel stragglers manually. Resolution: zero open orders for
this bot's wallet.

### 5. Critical task restart budget exhausted

Alert fields: category `task_crash`, task name, four crashes in ten
minutes. Checks: journal tail around crash times
(`grep '"event_type": "runtime_' data/journal/events*.jsonl`), memory/disk
pressure, recent deploys. Resolution: root cause identified and fixed;
supervisor reports the task healthy on a fresh start.

### 6. Persistence/disk failure

Alert fields: category `persistence|disk`, `disk_halt` at ≥95%. Checks:
`df -h` on the data volume, filesystem errors, SQLite integrity
(`PRAGMA integrity_check;`). Resolution: free space restored below the
warning threshold (80%) and a probe write succeeds; corrupt stores are
restored from backups, never hand-edited.

### 7. Automatic-resume rejection

Alert fields: reason like `lease_missing_or_revoked`, `config_mismatch`,
`kill_switch_active`, `preflight_failed:<names>`. This alert means the
process restarted but stayed stopped — by design. Resolution: fix the named
gate, then continue with the shared procedure (step 6 onward) only when the
original safety incident is also resolved.

### 8. Lease expiration

Alert fields: `lease_expired`. The bot stopped entries, exited, cancelled,
and latched safely. Checks: confirm no open orders/positions at the
exchange. Resolution: operator decision to continue trading → shared
procedure steps 6–8 (new lease required; the bot never extends its own).

### 9. Telegram backlog

WARNING `telegram_backlog`. Checks: bot token/chat id valid, network egress
to `api.telegram.org`. Delivery retries indefinitely at capped intervals;
no trading action needed. Resolution: oldest pending age drops to zero.

## Standard operations

### Start (first time or after clean stop)

```bash
sudo systemctl start polymarket-bot   # or: docker compose up -d
journalctl -u polymarket-bot -f       # watch startup reconciliation
```

Dry run stays manual: press **Start** in the dashboard.

### Ordinary host restart

No special action: systemd/Compose restart the dashboard, which runs the
full auto-resume gate against the persisted lease. A valid lease plus fresh
preflight/reconciliation resumes live trading automatically and emits an
INFO notice; anything else stays stopped and sends `AUTO_RESUME_REJECTED`.

### Emergency halt

Dashboard **Emergency halt** with confirmation `HALT` — latches instantly,
cancels orders, persists, alerts. Process shutdown alone never revokes the
lease; explicit stop/halt/config change does.

### Rollback a bad deploy

```bash
sudo systemctl stop polymarket-bot
git -C /opt/polymarket-bot checkout <previous-tag>
pip install -e /opt/polymarket-bot
sudo systemctl start polymarket-bot
```

The persisted kill switch and lease survive rollback; re-check the resume
gates afterwards.
