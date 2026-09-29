# Editable Mirth automation proof of concept

This version runs locally without Jenkins or Mirth access. It lets you edit and
demonstrate the workflow now, while real network calls remain disabled.

## Run it now

From the project folder:

```powershell
python automation.py --client CHS --message-id EMAIL-MSG-1001
```

Expected result:

```text
MODE: DEMO (no Mirth request will be made)
Client: CHS
Email/business Message ID: EMAIL-MSG-1001
Resolved internal Mirth ID: 12345
Current status: ERROR
Destination: PCP_Outbound
DRY RUN: no reprocessing was performed.
```

Test a blocked message:

```powershell
python automation.py --client LABFLY --message-id EMAIL-MSG-2001
```

It is blocked because the example status is `SENT`, not `ERROR`.

Simulate the reprocessing path without contacting Mirth:

```powershell
python automation.py --client CHS --message-id EMAIL-MSG-1001 --execute
```

## Files to edit

| File | What you change |
|---|---|
| `demo_messages.json` | Add fake email IDs, clients, statuses, and internal Mirth IDs for your demonstration |
| `config.example.json` | Keep placeholders during the demo; later copy to `config.json` and enter approved values |
| `automation.py` | Add email parsing, SQL lookup, validation rules, status polling, and notifications |
| `Jenkinsfile.sample` | Change agent label and Jenkins credential ID after access is provided |

## Required real values

Every production value is marked `CHANGE_REQUIRED`:

```text
Mirth URL/IP and HTTPS port
Mirth channel UUID
PCP_Outbound destination metadata/connector ID
Jenkins internal agent label
Jenkins Mirth credential ID
Mirth API username and password (Jenkins credentials only)
Email Message ID to internal Mirth ID mapping rule
```

## Switch to real mode later

1. Copy `config.example.json` to `config.json`.
2. Set `mode` to `real`.
3. Replace every `CHANGE_REQUIRED` value.
4. Install dependencies: `python -m pip install -r requirements.txt`.
5. Set `MIRTH_USERNAME` and `MIRTH_PASSWORD` through Jenkins credentials.
6. Run without `--execute` first.
7. Validate in non-production before any reprocessing test.

Do not commit `config.json`, credentials, PHI, production messages, or real logs.

