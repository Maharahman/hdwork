-- Struktur mapping saja. Tidak mengadopsi device lama atau membuat device Cacti.
CREATE TABLE datin_cacti_devices (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  spreadsheet_id VARCHAR(128) NOT NULL,
  sid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  server ENUM('SBS', 'SBU') NOT NULL,
  tree_id INT UNSIGNED NOT NULL,
  host_id INT UNSIGNED NOT NULL,
  header_id INT UNSIGNED NOT NULL,
  ownership_status ENUM('REVIEW_REQUIRED', 'VERIFIED') NOT NULL DEFAULT 'REVIEW_REQUIRED',
  verified_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_datin_device_sid (spreadsheet_id, sid),
  UNIQUE KEY uq_datin_device_host (server, host_id)
) ENGINE=InnoDB;
