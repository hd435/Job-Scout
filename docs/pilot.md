# Running Job-Scout as an independent tester

This is the briefing for the tester pilot. It assumes you are a different person from the
maintainer, with your own job search, your own machine, and no interest in how the code was built.

The point of the pilot is **not** to prove the software is finished. It is to find out whether
someone who did not build it can get to a useful result unaided, and what breaks when they try.

## If you were invited to the hosted bot

You need nothing installed — no Python, no configuration file, no scheduler. The service already
runs; you are giving it your own job search.

1. Open **`@AwesomeJobScoutBot`** in Telegram and press **Start** (or send `/start`).
2. Answer five short screens, all buttons:
   kinds of role → early-career types → location → employers → confirm.
   About a minute, and `/settings` changes any of it later.
3. Stop there. It now monitors in the background and messages you when a genuinely new matching
   role appears.

The commands you may want: `/status` (is monitoring working?), `/digest` (roles that already match
now), `/settings`, `/pause`, `/resume`, `/delete` (erases your data and stops monitoring), `/help`.

What to expect, so silence does not read as breakage:

- **You are told about roles that appear after you set up.** Roles that were already open do not
  produce an alert — ask for those with `/digest` at any time.
- **Nothing for a few days is normal.** These employers post a handful of early-career roles a
  month between them.
- `/status` tells you whether monitoring is running and how many employers are being scanned.
- Alerts arrive as ordinary Telegram messages with the role, employer, location and a link to the
  employer's own page.

**Your data, on a hosted bot.** The operator holds your Telegram user id, your chosen preferences,
which employers you follow, and one row per alert sent to you — nothing else: no name, no email, no
CV, no application tracking. `/delete` removes all of it and stops monitoring immediately.

**Which employers you follow is yours.** The service offers 12 employers grouped into packs; picking
a pack means the service reads those employers' public job boards on your behalf. If you would
rather follow fewer, follow fewer. The operator chose that list (D-094), not you — if it matters to
you which boards are read, ask before you accept the invitation.

What to report: anything confusing, anything that arrived that should not have, anything that
looked like it should have arrived and did not, and whether you would keep it switched on. If
something looks wrong, the `/status` output and roughly when it happened is enough. Note that
`/status` answering at all proves the bot is alive; it does not prove that alerts will arrive.


## If you are self-hosting the CLI

The rest of this document is for that path.

---

## What is yours, and what stays with the maintainer

| Yours | The maintainer's |
|---|---|
| Your Telegram bot, token and chat id | Their bot and chat, which you never use |
| Your database (`data/jobscout.db`, created on your first scan) | Their database and its history |
| Your `config.toml`: sources, preferences, locations | Their personal configuration |
| Your decision about **which employers to scan** | Their personal-use risk decision, which does not transfer to you |

That last row matters. The repository ships a catalogue of employers, and the maintainer has
accepted the personal-use risk of polling them from their own machine. That is their decision for
their deployment. **You choose your own source list**, and enabling an employer means your machine
sends requests to that employer's public job board. `config.example.toml` starts you with six and
explains how to narrow further. If you would rather start with two, do that — it is the better
pilot behaviour anyway, because fewer sources make a match easier to attribute.

## The journey to try

Follow the README's "Set up your own deployment". In order, and note where you hesitate:

1. **Install** — `pip install -e ".[dev]"` from a clone.
2. **Your own bot** — `@BotFather`, token, chat id, into `.env`.
3. **Prove delivery** — `jobscout notify-test`. This should give you one message that says it is
   a delivery check.
4. **Choose your scope** — copy `config.example.toml` to `config.toml`, edit the roles, seniority
   and locations you actually care about, then `jobscout config validate` and `jobscout config show`.
5. **Prove the whole journey** — `jobscout rehearsal`. One synthetic alert, clearly labelled.
6. **Baseline** — `jobscout schedule --once` (or `scan --source <id> --dry-run` per source).
7. **See what is already open** — `jobscout summary --current`, then `jobscout digest --dry-run`
   and `jobscout digest`.
8. **Run it on a schedule** — below.
9. **Wait for a genuine new role** — this is the only step that cannot be forced.

## Running it on a schedule

Each pass scans only the sources whose 30-minute cadence has elapsed, delivers any queued alerts,
and exits. Nothing has to stay resident.

**One short script.** Whatever the platform, keep the command in a file so the scheduler never has
to deal with quoting.

```bat
@echo off
rem Job-Scout: one short pass. Machine-specific; keep it with your config.
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m jobscout schedule --once --config config.toml >> schedule.log 2>&1
exit /b %ERRORLEVEL%
```

(Save that as `run-once.cmd` next to your `config.toml` on Windows; on macOS/Linux the equivalent
is a two-line shell script, or just the cron line below.)

**Windows** — register it with the native cmdlets, which handle paths with spaces correctly
(`schtasks /TR` does not):

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\full\path\to\run-once.cmd"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
             -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "Job-Scout scan" -Action $action -Trigger $trigger -Force

# Is it alive, and what did the last pass do?
schtasks /Query /TN "Job-Scout scan" /FO LIST /V | Select-String "Status|Last Run|Last Result"
Get-Content schedule.log -Tail 20

# Stop it (keeps the definition) / start it again / remove it
Disable-ScheduledTask -TaskName "Job-Scout scan"
Enable-ScheduledTask  -TaskName "Job-Scout scan"
Unregister-ScheduledTask -TaskName "Job-Scout scan" -Confirm:$false
```

**macOS / Linux** — a cron entry every five minutes:

```bash
crontab -e
# then add one line, replacing the paths with yours:
*/5 * * * * cd /path/to/Job-Scout && .venv/bin/python -m jobscout schedule --once --config config.toml >> schedule.log 2>&1
```

`systemd` timers work too and log more usefully (`Type=oneshot`, `OnUnitActiveSec=5min`), but cron
is enough for a pilot.

Either way, check on it with:

```bash
python -m jobscout status --brief --config config.toml
```

## What to expect, so silence does not read as breakage

- **The first scan is silent by design.** It records what is already open. Alerts start from the
  next genuinely new matching role. `jobscout summary --current` and `jobscout digest` are how you
  see the current picture.
- **Zero matches is a real possible answer.** If your scope matches nothing in the current
  catalogue, `jobscout summary --current` now prints the most common reasons your roles were
  excluded. That is the product telling you about your scope, not failing.
- **A message naming several employers at once** is the shared-connectivity notice: several
  sources failed because the connection never opened, which usually means your machine's network.
  It is one message on purpose.
- **Partial scans are not failures.** A large sitemap board may report `partial` because a
  per-scan page cap was reached; the next pass continues.

## What to report, and how

Keep a short log as you go. For anything that looks wrong, these four commands say almost
everything:

```bash
python -m jobscout status --brief --config config.toml
python -m jobscout config validate --config config.toml
python -m jobscout report --days 1 --config config.toml
python -m jobscout notify-test
```

Before pasting output anywhere, check it for your bot token and chat id — `notify-test` redacts
them, but your shell history and log files will not.

The four questions worth answering at the end:

1. Could you install and configure it **without asking the maintainer anything**? If not, at which
   step did you get stuck, and what did the output say?
2. Did you trust what it told you? Which parts were unclear — status, summary, report, alerts?
3. Did anything arrive, and was it relevant? If nothing arrived, did you understand why?
4. What would have to change for you to keep using it?

## What this pilot deliberately does not include

No installer, no hosted service, no accounts, no dashboard, no licence decision beyond "private
during the beta" (D-071). Those belong to a self-host release, not to a two-person pilot. If you
find yourself wanting one of them, that is itself a useful finding — write it down rather than
building it.
