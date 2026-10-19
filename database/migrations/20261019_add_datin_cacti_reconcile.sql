-- Back up the database first. Apply once, after all previous DATIN migrations.
ALTER TABLE datin_sheet_sources
  ADD COLUMN device_inventory_json JSON NULL,
  ADD COLUMN device_inventory_snapshot_at DATETIME(3) NULL;
ALTER TABLE datin_customer_master
  ADD COLUMN source_status VARCHAR(16) NOT NULL DEFAULT 'OK',
  ADD COLUMN source_status_snapshot_at DATETIME(3) NULL;
ALTER TABLE datin_cacti_devices
  ADD COLUMN sync_state ENUM('ACTIVE','DELETED') NOT NULL DEFAULT 'ACTIVE',
  ADD COLUMN last_applied_json JSON NULL,
  ADD COLUMN last_source_snapshot_at DATETIME(3) NULL,
  ADD COLUMN deleted_at DATETIME(3) NULL,
  ADD COLUMN last_sync_job_id CHAR(36) NULL;

-- Seed from existing VERIFIED receipts only; never infer ownership from a name.
UPDATE datin_cacti_devices d JOIN datin_cacti_pilot_jobs j
  ON j.spreadsheet_id = d.spreadsheet_id AND j.sid = d.sid
  AND j.server = d.server AND j.host_id = d.host_id AND j.header_id = d.header_id
  SET d.last_applied_json = j.desired_json
  WHERE j.job_status = 'VERIFIED' AND d.ownership_status = 'VERIFIED';

CREATE TABLE datin_cacti_sync_runs (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  spreadsheet_id VARCHAR(128) NOT NULL,
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  run_status ENUM('RUNNING','DONE') NOT NULL DEFAULT 'RUNNING',
  active TINYINT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  heartbeat_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  completed_at DATETIME(3) NULL,
  last_error VARCHAR(512) NULL,
  last_error_at DATETIME(3) NULL,
  summary_json JSON NULL,
  UNIQUE KEY uq_datin_sync_active (spreadsheet_id, active)
) ENGINE=InnoDB;

CREATE TABLE datin_cacti_sync_jobs (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  spreadsheet_id VARCHAR(128) NOT NULL,
  resource_key VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  sid VARCHAR(128) NULL,
  operation ENUM('HEADER','CREATE','UPDATE','DELETE','CLEANUP','VERIFY') NOT NULL,
  job_status ENUM('PREPARED','ATTEMPTED','VERIFIED','CANCELLED') NOT NULL DEFAULT 'PREPARED',
  request_hash CHAR(64) NOT NULL,
  request_json JSON NOT NULL,
  expected_json JSON NOT NULL,
  source_snapshot_at DATETIME(3) NOT NULL,
  master_backup_json JSON NULL,
  receipt_json JSON NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  attempted_at DATETIME(3) NULL,
  cancelled_at DATETIME(3) NULL,
  verified_at DATETIME(3) NULL,
  KEY idx_datin_sync_job_run (run_id, job_status),
  KEY idx_datin_sync_job_sid (spreadsheet_id, sid)
) ENGINE=InnoDB;

CREATE TABLE datin_customer_archives (
  job_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  spreadsheet_id VARCHAR(128) NOT NULL,
  sid VARCHAR(128) NOT NULL,
  reason VARCHAR(32) NOT NULL,
  data_json JSON NOT NULL,
  snapshot_at DATETIME(3) NOT NULL,
  archived_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_datin_archive_sid (spreadsheet_id, sid)
) ENGINE=InnoDB;
