locals {
  # The workspace's ingestion cap, held here rather than inline on the resource
  # only so the alert that watches it reads the same number. A threshold that
  # drifted from the cap would warn late or never.
  #
  # Measured ingestion is ~0.0004 GB/day, so this sits around 300x above
  # normal: the alert exists for a runaway — a crash-looping replica logging at
  # speed — not for ordinary growth. Reaching the cap stops ingestion for the
  # rest of the day, which is the one failure that blinds everything else here.
  log_daily_quota_gb = 0.15

  # Fired at, not on: warning while there is still a day's headroom left is the
  # only useful moment, since hitting the cap is what stops the logs that would
  # tell you about it.
  log_quota_alert_gb = local.log_daily_quota_gb * 0.8
}
