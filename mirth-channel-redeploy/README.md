# mirth-channel-redeploy

Automates the manual post-deployment step in Mirth Connect (4.5.x):

1. Open the channel, then on the Source tab set **Poll Once on Start = Yes**
2. Save
3. Redeploy, after which the channel goes Idle -> Reading -> Idle

This project is separate from `editable-mirth-automation`: it has its own config,
Jenkins job and credential, and shares no files with that project.

## Files

| File | Purpose |
|---|---|
| `mirth_redeploy.py` | The automation script |
| `config.json` | Mirth server per environment (no passwords) |
| `requirements.txt` | Python dependencies |
| `Jenkinsfile` | Jenkins pipeline |

## Run locally

```bash
pip install -r requirements.txt
export MIRTH_USERNAME=youruser MIRTH_PASSWORD=yourpass

# Dry run (default): shows the plan, changes nothing
python3 mirth_redeploy.py --env CHS --channel "CHANNEL_NAME"

# Real run
python3 mirth_redeploy.py --env CHS --channel "CHANNEL_NAME" --execute
```

Options: `--base-url URL` (override config), `--wait 120` (seconds to wait for STARTED),
`--settle 10` (seconds to wait for the first poll), `--insecure` (HTTPS with self-signed certificate).

Each run writes an audit record to `audit/` (git-ignored).

## Jenkins

- New Pipeline job, Pipeline script from SCM, this repo
- **Script Path:** `mirth-channel-redeploy/Jenkinsfile`
- Credential (Username with password): `mirth-redeploy-dev-creds`
- First run with **EXECUTE** unticked (dry run)
