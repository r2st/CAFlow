#!/bin/sh
# Ask, every few minutes, whether DoAide Reach is actually working — and say so once
# when the answer changes.
#
# `/health/ready` is the obvious check and not a sufficient one. It answers for
# the API process and the database behind it, which is the half of this
# deployment that fails loudly: Caddy pulls a non-2xx upstream out of the pool
# and the site starts returning 502s, which someone notices. The half that
# fails silently is everything else. A dead Celery worker serves every page
# perfectly and sends no reminders; a beat that stopped queues nothing at all;
# a backup timer that has been failing for a fortnight looks exactly like one
# that has been working. None of those produce a single failed request, and all
# of them are the reason a firm misses a filing date.
#
# So there are five checks, and the last two are the ones worth having.
#
#   HEALTH_URL             the API's readiness probe, on the bridge address
#   PUBLIC_URL             the same app through Caddy — catches TLS, DNS, the vhost
#   UNITS                  systemd units that must be active
#   BACKUP_DIR             where caflow-backup writes
#   MAX_BACKUP_AGE_HOURS   older than this and the nightly backup has stopped
#   RESTORE_STATE_FILE     where caflow-restore-check records a passing drill
#   MAX_RESTORE_AGE_DAYS   older than this and the weekly restore drill has stopped (0 disables)
#   MIN_FREE_MB            free space below which everything starts failing at once
#   FAILURES_BEFORE_ALERT  consecutive failing runs before anyone is told
#   ALERT_REPEAT_HOURS     re-say it this often while it is still broken
#   ALERT_WEBHOOK          optional: a URL the message is POSTed to as JSON
#   ALERT_EMAIL_TO         optional: an address, sent through SendGrid's HTTP API
#   SENDGRID_API_KEY       the key for that (the same one SMTP_PASSWORD holds)
#   ALERT_EMAIL_FROM       must be an address SendGrid will send as
#   STATE_FILE             where the last verdict is remembered
set -eu

HEALTH_URL="${HEALTH_URL:-http://172.18.0.1:3010/health/ready}"
PUBLIC_URL="${PUBLIC_URL:-https://caflow.aiknol.com/health}"
UNITS="${UNITS:-caflow-api caflow-web caflow-worker caflow-beat}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/caflow}"
MAX_BACKUP_AGE_HOURS="${MAX_BACKUP_AGE_HOURS:-36}"
RESTORE_STATE_FILE="${RESTORE_STATE_FILE:-/var/lib/caflow/restore-check.state}"
# Ten days: the drill is weekly, so this tolerates one missed run and a
# generous randomised delay, and complains on the second. 0 turns the check off
# for a box that has deliberately not installed the drill.
MAX_RESTORE_AGE_DAYS="${MAX_RESTORE_AGE_DAYS:-10}"
MIN_FREE_MB="${MIN_FREE_MB:-2048}"
FAILURES_BEFORE_ALERT="${FAILURES_BEFORE_ALERT:-2}"
ALERT_REPEAT_HOURS="${ALERT_REPEAT_HOURS:-6}"
ALERT_WEBHOOK="${ALERT_WEBHOOK-}"
ALERT_EMAIL_TO="${ALERT_EMAIL_TO-}"
SENDGRID_API_KEY="${SENDGRID_API_KEY-}"
ALERT_EMAIL_FROM="${ALERT_EMAIL_FROM:-no-reply@caflow.aiknol.com}"
STATE_FILE="${STATE_FILE:-/var/lib/caflow/monitor.state}"
CURL_TIMEOUT="${CURL_TIMEOUT:-10}"
HOSTNAME_LABEL="${HOSTNAME_LABEL:-$(hostname 2>/dev/null || echo caflow)}"

# A failure is one line: a short key, a space, then the sentence a human reads.
# The key is what decides whether this is the same outage as last time — the
# sentence carries a duration or a percentage and would otherwise make every
# run look like a new problem.
failures=""
note() {
  failures="$failures$1
"
}

# ------------------------------------------------------------------- checks --

# --max-time so a hung upstream cannot hold the timer open until the next one
# fires. -o /dev/null because the body is not the answer here; the status is.
probe() {
  curl -fsS -o /dev/null --max-time "$CURL_TIMEOUT" "$1" 2>/dev/null
}

probe "$HEALTH_URL" \
  || note "ready /health/ready is not answering 2xx on $HEALTH_URL — the API cannot reach its database, or is not running"

# Through Caddy, over TLS, by name. Everything the readiness probe cannot see:
# an expired certificate, a Caddyfile reload that dropped this vhost, DNS. A
# failure here with the check above passing means the app is fine and nobody
# can get to it, which from a client's side is the same outage.
probe "$PUBLIC_URL" \
  || note "public $PUBLIC_URL is not answering — the app is up but the front door is not"

# The two units with no HTTP surface are the whole reason this check exists.
# A stopped worker or beat is invisible to every other check in this file and
# to every visitor: pages render, logins work, and no reminder is ever sent.
for unit in $UNITS; do
  # The key carries the unit name, so a second unit going down afterwards is a
  # different signature and gets said out loud. A bare `unit` key would fold
  # "the worker is down" and "the worker and beat are both down" into one
  # outage — and the second is a different page with a different first move.
  systemctl is-active --quiet "$unit" \
    || note "unit:$unit $unit is not active — $(systemctl is-active "$unit" 2>&1 || true)"
done

# A backup timer that quietly stopped is discovered on restore day. `find
# -mmin` rather than comparing timestamps by hand: it is the same primitive the
# backup script prunes with, and it does not care what date format the box uses.
if [ -d "$BACKUP_DIR" ]; then
  recent=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'caflow-db-*.dump' \
    -mmin "-$((MAX_BACKUP_AGE_HOURS * 60))" -print 2>/dev/null | head -n 1)
  [ -n "$recent" ] \
    || note "backup no database dump in $BACKUP_DIR newer than ${MAX_BACKUP_AGE_HOURS}h — the nightly backup has stopped"
else
  note "backup $BACKUP_DIR does not exist — no backup has ever run here"
fi

# A backup nobody has ever restored is a belief, not a backup. caflow-restore-check
# writes its state file only when a drill passed, so the file's age is the age
# of the last time anyone actually knew the firm could be brought back — and
# that is the number worth watching, because a drill failing every Sunday and a
# drill not running at all look identical from anywhere else.
if [ "$MAX_RESTORE_AGE_DAYS" -gt 0 ] 2>/dev/null; then
  if [ -f "$RESTORE_STATE_FILE" ]; then
    fresh=$(find "$RESTORE_STATE_FILE" -mmin "-$((MAX_RESTORE_AGE_DAYS * 1440))" -print 2>/dev/null)
    [ -n "$fresh" ] \
      || note "restore no successful restore drill in ${MAX_RESTORE_AGE_DAYS} days — the backups have not been proven restorable"
  else
    note "restore no restore drill has ever passed on this box — caflow-restore-check is not installed or has never succeeded"
  fi
fi

# Shared hardware with seven other applications, and this one writes dumps to
# it nightly. A full disk is not one failure; it is Postgres refusing writes,
# uploads failing, the journal truncating and the next backup writing a
# zero-length file, all at once and all at three in the morning.
free_mb=$(df -Pm "$BACKUP_DIR" 2>/dev/null || df -Pm /)
free_mb=$(echo "$free_mb" | awk 'NR==2 {print $4}')
case "$free_mb" in
  '' | *[!0-9]*) note "disk could not read free space for $BACKUP_DIR" ;;
  *)
    [ "$free_mb" -ge "$MIN_FREE_MB" ] \
      || note "disk only ${free_mb}MB free where backups are written (want ${MIN_FREE_MB}MB)"
    ;;
esac

# -------------------------------------------------------------------- state --

# Signature is the keys alone, sorted. Two runs of the same outage produce the
# same signature and are told once; an outage that grows a second cause
# produces a different one and is told again, because that is new information.
signature=$(printf '%s' "$failures" | awk 'NF {print $1}' | sort | tr '\n' ',')

previous_count=0
previous_signature=""
previous_alert=0
if [ -f "$STATE_FILE" ]; then
  previous_count=$(sed -n 1p "$STATE_FILE")
  previous_signature=$(sed -n 2p "$STATE_FILE")
  previous_alert=$(sed -n 3p "$STATE_FILE")
fi
case "$previous_count" in '' | *[!0-9]*) previous_count=0 ;; esac
case "$previous_alert" in '' | *[!0-9]*) previous_alert=0 ;; esac

now=$(date -u +%s)

save_state() {
  mkdir -p "$(dirname "$STATE_FILE")"
  printf '%s\n%s\n%s\n' "$1" "$2" "$3" >"$STATE_FILE"
}

# ------------------------------------------------------------------ alerting --

deliver() {
  subject="$1"
  body="$2"

  # The journal always gets it, whether or not anything else is configured.
  # It is the transport that cannot itself be misconfigured, and it is what
  # `journalctl -u caflow-monitor` shows when someone finally goes looking.
  printf '%s\n%s\n' "$subject" "$body" >&2

  # JSON by hand, so this file needs nothing installed that the box does not
  # already have. Backslashes first — escaping quotes first would then escape
  # the backslashes it just introduced.
  escaped=$(printf '%s\n%s' "$subject" "$body" \
    | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
    | awk '{printf "%s\\n", $0}')

  if [ -n "$ALERT_WEBHOOK" ]; then
    curl -fsS -o /dev/null --max-time "$CURL_TIMEOUT" \
      -H 'Content-Type: application/json' \
      -d "{\"text\":\"$escaped\"}" "$ALERT_WEBHOOK" \
      || echo "caflow-monitor: the webhook itself failed — see the line above for the alert" >&2
  fi

  if [ -n "$ALERT_EMAIL_TO" ] && [ -n "$SENDGRID_API_KEY" ]; then
    curl -fsS -o /dev/null --max-time "$CURL_TIMEOUT" \
      -X POST https://api.sendgrid.com/v3/mail/send \
      -H "Authorization: Bearer $SENDGRID_API_KEY" \
      -H 'Content-Type: application/json' \
      -d "{\"personalizations\":[{\"to\":[{\"email\":\"$ALERT_EMAIL_TO\"}]}],
           \"from\":{\"email\":\"$ALERT_EMAIL_FROM\",\"name\":\"DoAide Reach monitor\"},
           \"subject\":\"$subject\",
           \"content\":[{\"type\":\"text/plain\",\"value\":\"$escaped\"}]}" \
      || echo "caflow-monitor: the alert email did not send — see the line above" >&2
  fi

  # Neither transport configured is a legitimate state — the journal is a real
  # place for this to land on a box someone reads — but it is worth saying so
  # rather than letting an unconfigured monitor look like a quiet one.
  if [ -z "$ALERT_WEBHOOK" ] && [ -z "$ALERT_EMAIL_TO" ]; then
    echo "caflow-monitor: no ALERT_WEBHOOK and no ALERT_EMAIL_TO — this alert went to the journal only" >&2
  fi
}

if [ -z "$signature" ]; then
  if [ -n "$previous_signature" ]; then
    deliver "DoAide Reach recovered on $HOSTNAME_LABEL" \
      "Everything that was failing is answering again. Was failing: $(
        printf '%s' "$previous_signature" | sed -e 's/,$//' -e 's/,/, /g'
      )"
  fi
  save_state 0 "" 0
  echo "caflow-monitor: all checks passed"
  exit 0
fi

count=$((previous_count + 1))
report=$(printf '%s' "$failures" | sed 's/^[^ ]* //')

# Two runs before anyone is told, because a deploy restarts four units and the
# next tick can land in the middle of it. A monitor that pages on a routine
# `systemctl restart` is a monitor that gets muted, and a muted monitor is the
# same as none on the night it would have mattered.
alerted_at="$previous_alert"
if [ "$count" -ge "$FAILURES_BEFORE_ALERT" ]; then
  repeat_due=$((now - previous_alert >= ALERT_REPEAT_HOURS * 3600))
  if [ "$signature" != "$previous_signature" ] || [ "$repeat_due" = 1 ]; then
    deliver "DoAide Reach is unhealthy on $HOSTNAME_LABEL" "$report"
    alerted_at="$now"
  fi
  save_state "$count" "$signature" "$alerted_at"
else
  # Not yet worth telling anyone, so the signature is not recorded as told —
  # otherwise the run that crosses the threshold would compare equal to this
  # one and stay silent for good.
  save_state "$count" "$previous_signature" "$alerted_at"
fi

printf 'caflow-monitor: %s check(s) failing, %s run(s) in a row\n%s\n' \
  "$(printf '%s' "$failures" | grep -c .)" "$count" "$report" >&2

# Non-zero so `systemctl status caflow-monitor` and `systemctl list-timers`
# carry the verdict too. The alert is the thing that reaches a person; this is
# what answers someone who is already logged in and asking.
exit 1
