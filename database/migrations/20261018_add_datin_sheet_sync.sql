CREATE TABLE datin_sheet_sources (
  spreadsheet_id VARCHAR(128) NOT NULL,
  last_snapshot_at DATETIME(3) NULL,
  PRIMARY KEY (spreadsheet_id)
) ENGINE=InnoDB;

CREATE TABLE datin_customer_master (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  spreadsheet_id VARCHAR(128) NOT NULL,
  sid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  source_row INT UNSIGNED NOT NULL,
  customer_name VARCHAR(255) NOT NULL,
  data_json JSON NOT NULL,
  data_hash CHAR(64) NOT NULL,
  last_synced_at DATETIME(3) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_datin_sheet_sid (spreadsheet_id, sid)
) ENGINE=InnoDB;

CREATE TABLE datin_sheet_sync_runs (
  run_id CHAR(36) NOT NULL,
  spreadsheet_id VARCHAR(128) NOT NULL,
  snapshot_at DATETIME(3) NOT NULL,
  result_json JSON NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id),
  KEY idx_datin_sheet_runs (spreadsheet_id, created_at)
) ENGINE=InnoDB;
