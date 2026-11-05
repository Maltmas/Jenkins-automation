#!/usr/bin/env python3
"""Post-deployment Mirth step automation (written for Mirth 4.5.x).

Replaces the manual steps:
  open channel -> Source tab -> "Poll Once on Start" = Yes -> Save -> Redeploy
  -> channel goes Idle -> Reading -> Idle

Flow (per channel):
  1. Log in to the Mirth REST API and resolve the channel ID from its NAME
  2. Set Poll Once on Start = Yes on the source connector and save (skipped if already Yes)
  3. Redeploy the channel and wait until it is STARTED
  4. Wait until the source connector has gone Reading -> Idle (no time limit by default)
  5. Only then: turn Poll Once on Start back to No and save (verified)
  6. Email a summary of the run (to techsvc@agilonhealth.com by default)

Standalone project: uses its OWN config.json (next to this script).
Dry run by default; add --execute to make changes and send the email.
Mirth credentials: MIRTH_USERNAME / MIRTH_PASSWORD.
SMTP credentials (only if the mail server needs a login): SMTP_USERNAME / SMTP_PASSWORD.
No message content (possible PHI) is read, printed, logged or emailed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
import urllib3

HERE = Path(__file__).resolve().parent
HDR = {"X-Requested-With": "OpenAPI"}
XML_ACCEPT = {**HDR, "Accept": "application/xml"}
TIMEOUT = (10, 60)
ERRORS = (requests.RequestException, ET.ParseError)


class MirthError(Exception):
    pass


ERRORS = (MirthError, *ERRORS)


# --------------------------------------------------------------------------- Mirth API
class Mirth:
    def __init__(self, base_url: str, verify: bool = True) -> None:
        self.base = base_url.rstrip("/") + "/api"
        self.s = requests.Session()
        self.s.verify = verify

    # ---- session ----
    def login(self, user: str, pwd: str) -> None:
        r = self.s.post(f"{self.base}/users/_login",
                        data={"username": user, "password": pwd},
                        headers=HDR, timeout=TIMEOUT)
        r.raise_for_status()
        if "SUCCESS" not in r.text.upper():
            raise MirthError("Mirth login was not successful")
        self._creds = (user, pwd)

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        """API call that logs in again once if the session has expired (HTTP 401),
        e.g. during a multi-hour wait or after a Mirth restart."""
        r = self.s.request(method, url, **kw)
        if r.status_code == 401 and getattr(self, "_creds", None):
            print("    Mirth session expired - logging in again")
            self.login(*self._creds)
            r = self.s.request(method, url, **kw)
        return r

    def logout(self) -> None:
        try:
            self.s.post(f"{self.base}/users/_logout", headers=HDR, timeout=TIMEOUT)
        except requests.RequestException:
            pass

    # ---- lookups ----
    def channels(self) -> dict[str, str]:
        """Return {channel name: channel id}."""
        r = self._req("GET", f"{self.base}/channels/idsAndNames", headers=XML_ACCEPT, timeout=TIMEOUT)
        r.raise_for_status()
        out: dict[str, str] = {}
        for entry in ET.fromstring(r.content).iter("entry"):
            vals = [x.text for x in entry.findall("string")]
            if len(vals) == 2 and vals[0] and vals[1]:
                out[vals[1]] = vals[0]
        return out

    def channel_xml(self, channel_id: str) -> str:
        r = self._req("GET", f"{self.base}/channels/{channel_id}", headers=XML_ACCEPT, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    def state(self, channel_id: str) -> str | None:
        """Deployed state: STARTED / STOPPED / PAUSED / DEPLOYING ... or None if not deployed."""
        r = self._req("GET", f"{self.base}/channels/{channel_id}/status", headers=XML_ACCEPT, timeout=TIMEOUT)
        if r.status_code != 200 or not r.content.strip():
            return None
        return ET.fromstring(r.content).findtext("state")

    def received(self, channel_id: str) -> int | None:
        r = self._req("GET", f"{self.base}/channels/{channel_id}/statistics", headers=XML_ACCEPT, timeout=TIMEOUT)
        if r.status_code != 200 or not r.content.strip():
            return None
        el = ET.fromstring(r.content).find(".//received")
        return int(el.text) if el is not None and el.text and el.text.strip().isdigit() else None

    def source_connection_state(self, channel_id: str) -> str | None:
        """Text of the Dashboard 'Connection' column for the source connector (Idle, Reading ...).

        Returns None if the dashboard-status extension can't be read (caller falls back
        to watching statistics), or "" if this channel has no entry yet.
        """
        url = f"{self.base}/extensions/dashboardstatus/connectorStates"
        try:
            r = self._req("GET", url, headers=XML_ACCEPT, timeout=TIMEOUT)
            if r.status_code == 406:
                r = self._req("GET", url, headers={**HDR, "Accept": "*/*"}, timeout=TIMEOUT)
            if r.status_code != 200 or not r.content.strip():
                return None
        except requests.RequestException:
            return None
        keys = {f"{channel_id}_0", channel_id}
        text = r.text.strip()
        try:
            if text[:1] in "{[":
                found = _state_from_json(json.loads(text), keys)
                return "" if found is None else found
            root = ET.fromstring(r.content)
        except (ValueError, ET.ParseError):
            return None
        for entry in root.iter("entry"):
            kids = list(entry)
            if kids and (kids[0].text or "").strip() in keys:
                vals = [e.text.strip() for e in entry.iter("string") if e.text and e.text.strip()][1:]
                return vals[-1] if vals else ""
        return ""

    def source_events(self, channel_id: str) -> list[tuple[int, str]] | None:
        """Recent Dashboard connection-log events of the SOURCE connector as [(logId, event)].

        Used so a very fast Reading -> Idle (shorter than one status check) is not missed.
        Only the event name is read; the log's 'information' text is never stored or printed.
        Returns None if the log can't be read.
        """
        url = f"{self.base}/extensions/dashboardstatus/connectionLogs/{channel_id}"
        try:
            r = self._req("GET", url, params={"fetchSize": 100}, headers=XML_ACCEPT, timeout=TIMEOUT)
            if r.status_code == 406:
                r = self._req("GET", url, params={"fetchSize": 100}, headers={**HDR, "Accept": "*/*"}, timeout=TIMEOUT)
            if r.status_code != 200:
                return None
            if not r.content.strip():
                return []
            text = r.text.strip()
            items: list[dict] = []
            if text[:1] in "{[":
                def walk(o):
                    if isinstance(o, dict):
                        if "event" in o:
                            items.append(o)
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)
                walk(json.loads(text))
            else:
                for el in ET.fromstring(r.content).iter():
                    if el.find("event") is not None:
                        items.append({c.tag: c.text for c in el})
        except (requests.RequestException, ValueError, ET.ParseError):
            return None
        out = []
        for it in items:
            try:
                log_id = int(str(it.get("logId")).strip())
            except ValueError:
                continue
            meta = str(it.get("metadataId", "0")).strip()
            if meta in ("0", "None", ""):
                out.append((log_id, str(it.get("event") or "").strip()))
        return out

    def server_logs(self, after_id: int = 0) -> list[dict] | None:
        """Recent Mirth server-log entries newer than after_id as
        [{"id", "level", "thread", "message"}]. None if the log can't be read.
        Used only in memory to recognise this channel's own lines; log text is
        never printed, stored or emailed (other channels' lines could hold PHI)."""
        url = f"{self.base}/extensions/serverlog"
        params = {"fetchSize": 500}
        if after_id:
            params["lastLogId"] = after_id
        try:
            r = self._req("GET", url, params=params, headers=XML_ACCEPT, timeout=TIMEOUT)
            if r.status_code == 406:
                r = self._req("GET", url, params=params, headers={**HDR, "Accept": "*/*"}, timeout=TIMEOUT)
            if r.status_code != 200:
                return None
            if not r.content.strip():
                return []
            text = r.text.strip()
            raw: list[dict] = []
            if text[:1] in "{[":
                def walk(o):
                    if isinstance(o, dict):
                        if "message" in o and "id" in o:
                            raw.append(o)
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)
                walk(json.loads(text))
            else:
                for el in ET.fromstring(r.content).iter():
                    if el.find("message") is not None and el.find("id") is not None:
                        raw.append({c.tag: (c.text or "") for c in el})
        except (requests.RequestException, ValueError, ET.ParseError):
            return None
        out = []
        for it in raw:
            try:
                lid = int(str(it.get("id")).strip())
            except ValueError:
                continue
            if lid > after_id:
                out.append({"id": lid, "level": str(it.get("level") or "").upper(),
                            "thread": str(it.get("threadName") or ""), "message": str(it.get("message") or "")})
        return out

    # ---- actions ----
    def save_channel(self, channel_id: str, xml: str) -> None:
        # No XML Accept header here: on Mirth 4.5 this endpoint answers 406 if we
        # insist on XML for its true/false reply. Let Mirth pick the reply format.
        r = self._req("PUT", f"{self.base}/channels/{channel_id}",
                       params={"override": "true"},
                       data=xml.encode("utf-8"),
                       headers={**HDR, "Content-Type": "application/xml", "Accept": "*/*"},
                       timeout=TIMEOUT)
        r.raise_for_status()
        reply = r.text.strip().lower()
        if reply in ("false", "<boolean>false</boolean>") or '"boolean":false' in reply.replace(" ", ""):
            raise MirthError("Mirth refused to save the channel")

    def deploy(self, channel_id: str) -> None:
        r = self._req("POST", f"{self.base}/channels/{channel_id}/_deploy",
                        params={"returnErrors": "true"},
                        headers=HDR, timeout=(10, 180))
        r.raise_for_status()


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def _state_from_json(data, keys: set[str]) -> str | None:
    """Find the connection-state text for one of `keys` in a JSON connectorStates reply."""
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys:
                s = [x for x in _strings(v) if x.strip()]
                return s[-1] if s else ""
        children = list(data.values())
    elif isinstance(data, list):
        children = data
    else:
        return None
    for c in children:
        found = _state_from_json(c, keys)
        if found is not None:
            return found
    if isinstance(data, dict):
        s = [x for x in _strings(data) if x.strip()]
        if len(s) >= 2 and s[0] in keys:
            return s[-1]
    return None


# --------------------------------------------------------------------------- channel XML
def set_poll_on_start(xml: str, value: bool) -> tuple[str, str]:
    """Set <pollOnStart> inside <sourceConnector> only.

    Text edit (not XML re-serialise) so scripts/transformers are saved back unchanged.
    Returns (new_xml, 'changed' | 'already' | 'missing').
    """
    want, other = ("true", "false") if value else ("false", "true")
    start, end = xml.find("<sourceConnector"), xml.find("</sourceConnector>")
    if start == -1 or end == -1:
        return xml, "missing"
    src = xml[start:end]
    if re.search(rf"<pollOnStart>\s*{want}\s*</pollOnStart>", src):
        return xml, "already"
    new_src, n = re.subn(rf"<pollOnStart>\s*{other}\s*</pollOnStart>",
                         f"<pollOnStart>{want}</pollOnStart>", src, count=1)
    if n == 0:
        return xml, "missing"
    return xml[:start] + new_src + xml[end:], "changed"


def save_poll_on_start(mirth: Mirth, cid: str, value: bool) -> str:
    """Set the value, save, and read the channel back to confirm. Returns 'changed' or 'already'."""
    xml, status = set_poll_on_start(mirth.channel_xml(cid), value)
    if status == "missing":
        raise MirthError("Source connector has no 'Poll Once on Start' setting (not a polling connector?)")
    if status == "changed":
        mirth.save_channel(cid, xml)
        _, check = set_poll_on_start(mirth.channel_xml(cid), value)
        if check != "already":
            raise MirthError(f"Save returned OK but Poll Once on Start is not {'Yes' if value else 'No'} in Mirth")
    return status


# --------------------------------------------------------------------------- waiting
def wait_started(mirth: Mirth, cid: str, timeout: int) -> None:
    state, deadline = None, time.time() + timeout
    while time.time() < deadline:
        state = mirth.state(cid)
        if state == "STARTED":
            return
        time.sleep(3)
    raise MirthError(f"Channel not STARTED after {timeout}s (last state: {state})")


def is_reading(state: str) -> bool:
    """True for the 'busy' connection states (Reading, Polling, Receiving ...).
    Idle, Disconnected, Connected etc. are NOT counted as reading."""
    st = state.lower()
    return any(w in st for w in ("read", "poll", "receiv", "process"))


class LogWatch:
    """Watches the Mirth server log for lines written by ONE channel after the redeploy.

    A line belongs to the channel if the channel name appears (as a whole name) in the
    log message or in the polling thread's name. Any such line proves a poll ran, even
    one that ended in milliseconds (e.g. "Skipping this cycle" when the lock is held).
    """

    def __init__(self, mirth: Mirth, name: str) -> None:
        self.mirth, self.available = mirth, True
        self.rx = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
        self.seen = self.skipped = False
        self.errors = 0
        logs = mirth.server_logs(0)
        if logs is None:
            self.available = False
            self.last_id = 0
        else:
            self.last_id = max((x["id"] for x in logs), default=0)

    def update(self) -> None:
        if not self.available:
            return
        try:
            logs = self.mirth.server_logs(self.last_id)
        except ERRORS:
            logs = None
        if not logs:
            return
        self.last_id = max(self.last_id, max(x["id"] for x in logs))
        for x in logs:
            if self.rx.search(x["message"]) or self.rx.search(x["thread"]):
                self.seen = True
                if "skipping this cycle" in x["message"].lower():
                    self.skipped = True
                if x["level"] in ("ERROR", "FATAL"):
                    self.errors += 1


def wait_idle_before_redeploy(mirth: Mirth, cid: str, args) -> None:
    """Never redeploy in the middle of a read: interrupting it could leave the channel's
    lock held (all later cycles would then skip). If Reading, wait until Idle first."""
    st = mirth.source_connection_state(cid)
    if st is None:
        raise MirthError("Cannot read the channel's Connection state, so it is not safe to redeploy")
    if not is_reading(st):
        return
    print(f"Channel is currently {st} - waiting for the current cycle to finish before redeploying...")
    t0 = time.time()
    while True:
        time.sleep(args.check_interval)
        try:
            st = mirth.source_connection_state(cid)
        except ERRORS:
            st = None
        if st and not is_reading(st):
            print(f"    current cycle finished ({int((time.time() - t0) / 60)} min) - continuing")
            return
        if time.time() - t0 >= 300 and int(time.time() - t0) % 300 < args.check_interval:
            print(f"    still waiting for the current cycle ({int((time.time() - t0) / 60)} min)")


def wait_poll_finished(mirth: Mirth, cid: str, before: int | None, log_baseline: int,
                       logw: LogWatch, args) -> dict:
    """Wait until the source connector has been Reading and is back to Idle.

    The poll is ONLY treated as finished when both are true:
      - the poll was seen to run: state Reading in the Dashboard, a line from this
        channel in the Mirth server log, a Reading event in the connection log,
        or new messages received, and
      - the source connector's state is now Idle.
    Time passing never counts as finished. --poll-timeout (0 = no limit, default)
    only stops waiting and reports a failure; Poll Once on Start is then left as Yes.
    """
    t0 = time.time()
    seen_reading, last, last_note = False, None, t0
    ever_read_state, failing_since = False, None
    while True:
        elapsed = time.time() - t0
        if args.poll_timeout and elapsed >= args.poll_timeout:
            raise MirthError(f"Stopped waiting after {args.poll_timeout}s: channel did not go "
                             f"Reading -> Idle (last state: {last})")

        try:
            st = mirth.source_connection_state(cid)
        except ERRORS:
            st = None
        if st is None:
            if not ever_read_state:
                raise MirthError("Cannot read the source connector's Connection state (Idle/Reading) "
                                 "from Mirth, so the end of the poll cannot be confirmed")
            # Mirth / network temporarily unreachable: keep trying for up to --retry-window
            failing_since = failing_since or time.time()
            down = int(time.time() - failing_since)
            if down >= args.retry_window:
                raise MirthError(f"Lost contact with Mirth for {down}s while waiting for the poll to finish")
            if down == 0 or down % 60 < 5:
                print(f"    cannot reach Mirth right now - retrying ({down}s)")
            time.sleep(5)
            continue
        ever_read_state, failing_since = True, None
        if st and st != last:
            print(f"    source connector: {st}")
            last = st
        newly_seen = False  # run proven by log/messages AFTER this state sample was taken
        if st and is_reading(st):
            seen_reading = True

        logw.update()
        if logw.seen and not seen_reading:
            seen_reading = newly_seen = True
            print("    server log: channel ran" + (" (cycle SKIPPED - lock held)" if logw.skipped else ""))

        if not seen_reading:
            try:
                events = mirth.source_events(cid) or []
            except ERRORS:
                events = []
            if any(lid > log_baseline and is_reading(ev) for lid, ev in events):
                seen_reading = newly_seen = True
                print("    connection log: Reading recorded")
            else:
                try:
                    rec = mirth.received(cid)
                except ERRORS:
                    rec = None
                if before is not None and rec is not None and rec > before:
                    seen_reading = newly_seen = True
                    print("    new message received (channel has read)")

        # Finish only on an Idle state read AFTER the run was proven (never a stale sample)
        if seen_reading and not newly_seen and st and st.lower() == "idle":
            secs = int(time.time() - t0)
            print(f"Polling finished: Reading -> Idle (~{secs}s)")
            return {"poll_seen_reading": True, "poll_seconds": secs}

        if time.time() - last_note >= 300:
            print(f"    still waiting ({int((time.time() - t0) / 60)} min) - state: {last}")
            last_note = time.time()
        # Check every second until Reading is seen (so a fast poll is not missed),
        # then every --check-interval seconds to keep load on Mirth negligible.
        time.sleep(args.check_interval if seen_reading and not newly_seen else 1)


# --------------------------------------------------------------------------- one channel
def process(mirth: Mirth, server: str, name: str, cid: str, args, rows: list) -> None:
    row = {"server": server, "channel_name": name, "channel_id": cid}
    rows.append(row)
    print(f"\n--- {name} on {server}")

    _, status = set_poll_on_start(mirth.channel_xml(cid), True)
    if status == "missing":
        row["result"] = "FAILED_NO_POLL_SETTING"
        raise MirthError("Source connector has no 'Poll Once on Start' setting (not a polling connector?)")
    row["poll_on_start_before"] = "Yes" if status == "already" else "No"
    print(f"Poll Once on Start currently: {row['poll_on_start_before']}")
    print(f"Deployed state: {mirth.state(cid)}")

    if not args.execute:
        steps = ["set Poll Once on Start = Yes and save"] if status == "changed" else []
        if is_reading(mirth.source_connection_state(cid) or ""):
            steps.insert(0, "wait for the current Reading cycle to finish")
        steps += ["redeploy", "wait until the source goes Reading -> Idle",
                  "set Poll Once on Start = No and save"]
        print("DRY RUN: would " + ", then ".join(steps) + ". Add --execute to run.")
        row["result"] = "DRY_RUN_OK"
        return

    err: Exception | None = None
    poll_done = False
    logw = None
    try:
        wait_idle_before_redeploy(mirth, cid, args)
        if save_poll_on_start(mirth, cid, True) == "changed":
            print("Saved: Poll Once on Start = Yes (verified)")
        before = mirth.received(cid)
        events = mirth.source_events(cid) or []
        log_baseline = max((lid for lid, _ in events), default=0)
        logw = LogWatch(mirth, name)
        if not logw.available:
            print("    note: Mirth server log not readable - a very fast/skipped cycle may not be detected")
        mirth.deploy(cid)
        print("Redeploy requested, waiting for STARTED...")
        wait_started(mirth, cid, args.wait)
        row["state_after"] = "STARTED"
        print("STARTED - waiting for polling to finish...")
        row.update(wait_poll_finished(mirth, cid, before, log_baseline, logw, args))
        poll_done = True
        try:
            after = mirth.received(cid)
        except ERRORS:
            after = None
        row["received"] = [before, after]
        if before is not None and after is not None:
            row["received_during_poll"] = after - before
            print(f"Received: {before} -> {after} (+{after - before})")
        logw.update()
        warnings = []
        if logw.skipped:
            warnings.append("The channel SKIPPED this cycle (lock held by another instance/server). "
                            "No data was processed by this run - please check the channel's lock.")
        if logw.errors:
            warnings.append(f"{logw.errors} ERROR line(s) from this channel in the Mirth server log "
                            "during the poll - please check the server log.")
        if row.get("received_during_poll") == 0 and not logw.skipped:
            warnings.append("0 messages were received by the poll - please confirm this is expected "
                            "(no new files) and not a silent error.")
        if warnings:
            row["warnings"] = warnings
            for w in warnings:
                print(f"WARNING: {w}")
    except ERRORS as e:
        err = e

    # Turn Poll Once on Start off ONLY after the poll was seen to finish (Reading -> Idle)
    if poll_done:
        try:
            save_poll_on_start(mirth, cid, False)
            row["poll_on_start_after"] = "No"
            print("Poll Once on Start turned off (No) - verified")
        except ERRORS as e:
            row["poll_on_start_after"] = "UNKNOWN - turning off failed, please check"
            err = MirthError(f"Could not turn off Poll Once on Start: {e}")
    else:
        row["poll_on_start_after"] = "Yes - NOT turned off (poll not confirmed finished), please check"
        print("Poll Once on Start left as Yes: the poll was not confirmed finished")

    if err is not None:
        row["error"] = str(err)
        row.setdefault("result", "FAILED")
        raise err
    row["result"] = "COMPLETED_WITH_WARNINGS" if row.get("warnings") else "COMPLETED"
    print(f"{name}: completed" + (" (with warnings)" if row.get("warnings") else ""))


# --------------------------------------------------------------------------- config / email / audit
def load_config(args) -> dict:
    p = Path(args.config)
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        if args.base_url:
            return {}
        raise MirthError(f"Config not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_server(args, cfg: dict) -> tuple[str, bool]:
    """Return (server URL, skip TLS check). 'insecure' can be set per env in config.json."""
    # MIRTH_URL is deliberately NOT read, so a value set for another
    # Mirth automation on the same Jenkins agent can't redirect this one.
    if args.base_url:
        return args.base_url, args.insecure
    envs = {k: v for k, v in cfg.items() if k != "email" and isinstance(v, dict)}
    if not args.env:
        raise MirthError(f"--env or --base-url is required (environments: {', '.join(envs)})")
    env = envs.get(args.env)
    if not env:
        raise MirthError(f"Environment '{args.env}' not in config (available: {', '.join(envs)})")
    if not env.get("server"):
        raise MirthError(f"No 'server' for '{args.env}' in config")
    return env["server"], args.insecure or bool(env.get("insecure", False))


def build_email(audit: dict) -> tuple[str, str]:
    ok = audit["result"] == "OK"
    warn = ok and any(r.get("warnings") for r in audit["rows"])
    status = "FAILED" if not ok else ("WARNING" if warn else "SUCCESS")
    label = audit.get("env") or audit.get("server") or "Mirth"
    names = ", ".join(audit["channels"])
    subject = f"[Mirth redeploy] {label} - {status} - {names}"
    lines = [
        "Mirth channel poll-on-start redeploy",
        "",
        f"Environment : {label}",
        f"Server      : {audit.get('server', '-')}",
        f"Started     : {audit['started_local']}",
        f"Finished    : {audit['finished_local']}",
        f"Result      : {status}",
    ]
    if audit.get("setup_error"):
        lines.append(f"Error       : {audit['setup_error']}")
    for r in audit["rows"]:
        lines += ["", f"Channel: {r.get('channel_name')}"]
        lines.append(f"  Result                        : {r.get('result', '-')}")
        if "poll_on_start_before" in r:
            lines.append(f"  Poll Once on Start before run : {r['poll_on_start_before']}")
        if "state_after" in r:
            lines.append(f"  Redeployed                    : yes, channel {r['state_after']}")
        if "poll_seconds" in r:
            lines.append(f"  Polling (Reading -> Idle)     : finished after ~{r['poll_seconds']} s")
        if "received_during_poll" in r:
            lines.append(f"  Messages received by the poll : {r['received_during_poll']}")
        if "poll_on_start_after" in r:
            lines.append(f"  Poll Once on Start after run  : {r['poll_on_start_after']}")
        for w in r.get("warnings", []):
            lines.append(f"  WARNING                       : {w}")
        if r.get("error"):
            lines.append(f"  Error                         : {r['error']}")
    lines += ["", "This message was sent automatically by mirth-channel-redeploy. "
              "It contains no message content."]
    return subject, "\n".join(lines)


def send_email(email_cfg: dict, subject: str, body: str) -> str:
    to = email_cfg.get("to") or []
    host = (email_cfg.get("smtp_host") or "").strip()
    if not host or not to:
        print("\nEmail NOT sent: smtp_host / to not set in config.json. Preview:")
        print(f"  To: {', '.join(to) or '-'}\n  Subject: {subject}\n")
        print("  " + body.replace("\n", "\n  "))
        return "NOT_CONFIGURED"
    user, pwd = os.getenv("SMTP_USERNAME", "").strip(), os.getenv("SMTP_PASSWORD", "").strip()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from") or user or to[0]
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP(host, int(email_cfg.get("smtp_port", 25)), timeout=30) as smtp:
        if email_cfg.get("starttls"):
            smtp.starttls(context=ssl.create_default_context())
        if user and pwd:
            smtp.login(user, pwd)
        smtp.send_message(msg)
    print(f"\nEmail sent to {', '.join(to)}")
    return "SENT"


def write_audit(record: dict) -> Path:
    audit_dir = HERE / "audit"
    audit_dir.mkdir(exist_ok=True)
    path = audit_dir / datetime.now(timezone.utc).strftime("redeploy-%Y%m%dT%H%M%S%fZ.json")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def now_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def check_api(mirth: Mirth, name: str, cid: str | None) -> None:
    """Report which status sources work for this channel. Prints no log or message content."""
    print(f"\n--- API check for {name}")
    if not cid:
        print("  channel                : NOT FOUND")
        return
    print(f"  channel id             : {cid}")
    print(f"  deployed state         : {mirth.state(cid)}")
    print(f"  connection state       : {mirth.source_connection_state(cid)!r}  (None = not readable)")
    ev = mirth.source_events(cid)
    print(f"  connection log         : {'not readable' if ev is None else f'{len(ev)} source event(s)'}")
    logs = mirth.server_logs(0)
    if logs is None:
        print("  server log             : not readable")
    else:
        rx = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
        mine = sum(1 for x in logs if rx.search(x["message"]) or rx.search(x["thread"]))
        print(f"  server log             : {len(logs)} recent line(s), {mine} from this channel")
    print(f"  received count         : {mirth.received(cid)}")


# --------------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser(description="Mirth poll-on-start redeploy automation")
    p.add_argument("--channel", action="append", required=True,
                   help="Exact channel name. Repeat or comma-separate for several.")
    p.add_argument("--env", help="Environment key in config.json, e.g. DEV")
    p.add_argument("--config", default="config.json")
    p.add_argument("--base-url", help="Single server, overrides config (e.g. https://10.15.10.102:9091)")
    p.add_argument("--execute", action="store_true", help="Make the changes and send the email (default is dry run)")
    p.add_argument("--wait", type=int, default=120, help="Seconds to wait for STARTED (default 120)")
    p.add_argument("--poll-timeout", type=int, default=0,
                   help="Optional safety stop in seconds (default 0 = wait until Reading -> Idle). "
                        "Reaching it is reported as a FAILURE and Poll Once on Start stays Yes.")
    p.add_argument("--check-interval", type=int, default=30,
                   help="Seconds between status checks once Reading has been seen (default 30)")
    p.add_argument("--retry-window", type=int, default=600,
                   help="Keep retrying this many seconds if Mirth is unreachable while waiting (default 600)")
    p.add_argument("--check-api", action="store_true",
                   help="Only check which Mirth status APIs are readable for the channel (changes nothing)")
    p.add_argument("--no-email", action="store_true", help="Do not send the result email")
    p.add_argument("--insecure", action="store_true", help="Skip TLS check (HTTPS dev servers only)")
    args = p.parse_args()

    names = [c.strip() for raw in args.channel for c in raw.split(",") if c.strip()]
    audit = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "started_local": now_local(),
             "env": args.env, "mode": "execute" if args.execute else "dry-run",
             "channels": names, "rows": []}
    failures: list[str] = []
    cfg: dict = {}

    try:
        cfg = load_config(args)
        server, insecure = resolve_server(args, cfg)
        audit["server"] = server
        user = os.getenv("MIRTH_USERNAME", "").strip()
        pwd = os.getenv("MIRTH_PASSWORD", "").strip()
        if not user or not pwd:
            raise MirthError("Set MIRTH_USERNAME and MIRTH_PASSWORD environment variables")
        if insecure:
            urllib3.disable_warnings()
        mirth = Mirth(server, verify=not insecure)
        try:
            mirth.login(user, pwd)
            ids = mirth.channels()
            if args.check_api:
                for name in names:
                    check_api(mirth, name, ids.get(name))
                return 0
            for name in names:
                cid = ids.get(name)
                if not cid:
                    print(f"\nERROR: channel '{name}' not found on {server}", file=sys.stderr)
                    audit["rows"].append({"server": server, "channel_name": name, "result": "NOT_FOUND",
                                          "error": "channel not found"})
                    failures.append(name)
                    continue
                try:
                    process(mirth, server, name, cid, args, audit["rows"])
                except ERRORS as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    failures.append(name)
        finally:
            mirth.logout()
    except (*ERRORS, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        audit["setup_error"] = str(e)
        failures.append("setup")

    audit["finished_local"] = now_local()
    audit["result"] = "FAILED" if failures else "OK"

    email_failed = False
    email_cfg = cfg.get("email", {}) if isinstance(cfg, dict) else {}
    if not args.execute:
        to = ", ".join(email_cfg.get("to") or []) or "(no recipient set)"
        print(f"\nDRY RUN: would email the result to {to}")
    elif args.no_email:
        audit["email"] = "SKIPPED"
    else:
        try:
            audit["email"] = send_email(email_cfg, *build_email(audit))
        except (smtplib.SMTPException, OSError) as e:
            print(f"\nERROR: email could not be sent: {e}", file=sys.stderr)
            audit["email"] = f"FAILED: {e}"
            email_failed = True

    print(f"\nAudit written: {write_audit(audit).name}")
    if failures:
        print(f"Failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    if email_failed:
        return 1
    print("All done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
