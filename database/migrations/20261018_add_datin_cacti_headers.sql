-- Jalankan setelah 20261018_add_datin_sheet_sync.sql.
-- Menandai baris yang benar-benar lolos snapshot terakhir, bukan data lama yang ditahan.
ALTER TABLE datin_customer_master ADD COLUMN valid_snapshot_at DATETIME(3) NULL;

CREATE TABLE datin_cacti_headers (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  spreadsheet_id VARCHAR(128) NOT NULL,
  server VARCHAR(3) NOT NULL,
  tree_id INT UNSIGNED NOT NULL,
  institution_name VARCHAR(255) NOT NULL,
  normalized_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  header_id INT UNSIGNED NOT NULL,
  last_verified_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_datin_cacti_header_name (spreadsheet_id, server, tree_id, normalized_name)
) ENGINE=InnoDB;
