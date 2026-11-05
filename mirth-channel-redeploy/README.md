# mirth-channel-redeploy

Automates the manual post-deployment step in Mirth Connect (4.5.x):

1. Set **Poll Once on Start = Yes** on the channel's source connector and save
2. Redeploy, so the channel polls immediately (Idle -> Reading -> Idle)
3. Wait until the source connector goes **Reading -> Idle** (no time limit)
4. Only then set **Poll Once on Start = No** again and save
5. Email the result to techsvc@agilonhealth.com

This project is separate from `editable-mirth-automation`: it has its own config,
Jenkins job and credential, and shares no files with that project.

## Files

| File | Purpose |
|---|---|
| `mirth_redeploy.py` | The automation script |
| `config.json` | Mirth server per environment + email settings (no passwords) |
| `requirements.txt` | Python dependencies |
| `Jenkinsfile` | Jenkins pipeline |

## Run locally

```bash
pip install -r requirements.txt
export MIRTH_USERNAME=youruser MIRTH_PASSWORD=yourpass

# Dry run (default): shows the plan, changes nothing, sends no email
python3 mirth_redeploy.py --env DEV --channel "CHANNEL_NAME"

# Real run
python3 mirth_redeploy.py --env DEV --channel "CHANNEL_NAME" --execute
```

Options:

| Option | Meaning |
|---|---|
| `--base-url URL` | Use this server instead of config.json |
| `--wait 120` | Seconds to wait for the channel to reach STARTED |
| `--poll-timeout 0` | Optional safety stop in seconds (0 = no limit, default). Reaching it is a FAILURE and Poll Once on Start stays Yes |
| `--check-interval 30` | Seconds between checks once Reading is seen (1 s before that) |
| `--retry-window 600` | Keep retrying this long if Mirth is unreachable during the wait |
| `--no-email` | Do not send the result email |
| `--insecure` | Skip TLS check (also settable per environment: `"insecure": true`) |

## When is the poll "finished"?

Only when the source connector was seen **Reading** (Dashboard Connection column,
Mirth connection log, or new messages received) **and** is back to **Idle**.
Time passing never counts as finished. If that can't be confirmed, Poll Once on Start
is left as Yes and the email reports a failure so someone can check.

## Email

Set `smtp_host`, `smtp_port`, `starttls` and `from` in the `email` section of
config.json. If the mail server needs a login, set `SMTP_USERNAME` / `SMTP_PASSWORD`
as environment variables (Jenkins credential), never in config.json.
While `smtp_host` is empty, the script prints the email to the console instead of sending it.

The email contains only channel names, statuses, timings and counts, never message content.

## Audit

Each run writes an audit record to `audit/` (git-ignored).

## Jenkins

- New Pipeline job, Pipeline script from SCM, this repo
- **Script Path:** `mirth-channel-redeploy/Jenkinsfile`
- Credential (Username with password): `mirth-redeploy-dev-creds`
- First run with **EXECUTE** unticked (dry run)
