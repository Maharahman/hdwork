-- Setelah tabel devices dan pilot_jobs. Reservasi tidak kedaluwarsa otomatis.
CREATE TABLE datin_cacti_batch_runs (
  batch_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  spreadsheet_id VARCHAR(128) NOT NULL,
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  manifest_json JSON NOT NULL,
  run_status ENUM('RESERVED', 'VERIFIED') NOT NULL DEFAULT 'RESERVED',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  verified_at DATETIME NULL,
  PRIMARY KEY (batch_id)
) ENGINE=InnoDB;

CREATE TABLE datin_cacti_batch_headers (
  batch_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  spreadsheet_id VARCHAR(128) NOT NULL,
  server ENUM('SBS', 'SBU') NOT NULL,
  institution VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  tree_id INT UNSIGNED NOT NULL,
  header_id INT UNSIGNED NULL,
  header_status ENUM('RESERVED', 'VERIFIED') NOT NULL DEFAULT 'RESERVED',
  PRIMARY KEY (batch_id, server, institution),
  UNIQUE KEY uq_datin_batch_header (spreadsheet_id, server, institution)
) ENGINE=InnoDB;
