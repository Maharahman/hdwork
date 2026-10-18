-- Setelah 20261019_add_datin_cacti_devices.sql.
-- Reservasi tidak kedaluwarsa otomatis: timeout create harus diperiksa, bukan diulang.
CREATE TABLE datin_cacti_pilot_jobs (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  spreadsheet_id VARCHAR(128) NOT NULL,
  sid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  server ENUM('SBS', 'SBU') NOT NULL,
  tree_id INT UNSIGNED NOT NULL,
  header_id INT UNSIGNED NOT NULL,
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  desired_json JSON NOT NULL,
  job_status ENUM('RESERVED', 'VERIFIED') NOT NULL DEFAULT 'RESERVED',
  host_id INT UNSIGNED NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  verified_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_datin_pilot_sid (spreadsheet_id, sid),
  UNIQUE KEY uq_datin_pilot_host (server, host_id)
) ENGINE=InnoDB;
