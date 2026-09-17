const pool = require("../config/database");
const { recordCollectorHealth } = require("../services/cacti-scheduler.service");

async function receiveCacti(req, res, next) {
  try {
    const {
      source = "cacti",
      customer_type,
      collector,
      scans = [],
      devices = [],
      health_report = false,
    } = req.body;

    if (!customer_type) {
      return res.status(400).json({
        success: false,
        message: "customer_type wajib diisi"
      });
    }

    if (health_report) {
      await recordCollectorHealth({ collector, scans });
      return res.json({
        success: true,
        collector,
        summary: { scans: scans.length },
      });
    }

    const result = {
      new: [],
      existing: [],
      recovered: [],
      skipped_servers: []
    };

    for (const scan of scans) {
      const server = scan.server;

      /*
       * Sangat penting:
       * server gagal scan TIDAK BOLEH dipakai
       * untuk menentukan recovered.
       */
      if (scan.status !== "success") {
        result.skipped_servers.push({
          server,
          reason: scan.error || "scan_failed"
        });

        continue;
      }

      /*
       * Device hasil scan khusus server ini.
       */
      const serverDevices = devices.filter(
        (device) => device.server === server
      );

      /*
       * Ambil ACTIVE alarm yang sebelumnya tersimpan
       * untuk customer + server ini.
       */
      const [activeRows] = await pool.query(
        `
        SELECT *
        FROM cacti_alarms
        WHERE customer_type = ?
          AND server = ?
          AND alarm_state = 'ACTIVE'
        `,
        [customer_type, server]
      );

      const currentNames = new Set(
        serverDevices.map((device) => device.name)
      );

      /*
       * ===================================================
       * NEW / EXISTING
       * ===================================================
       */

      for (const device of serverDevices) {
        const [rows] = await pool.query(
          `
          SELECT *
          FROM cacti_alarms
          WHERE customer_type = ?
            AND server = ?
            AND device_name = ?
          LIMIT 1
          `,
          [
            customer_type,
            server,
            device.name
          ]
        );

        const existing = rows[0];

        /*
         * Belum pernah ada sama sekali
         */
        if (!existing) {
          await pool.query(
            `
            INSERT INTO cacti_alarms (
              source,
              customer_type,
              server,
              device_name,
              group_name,
              olt_ip,
              slot_port,
              alarm_status,
              last_cacti_check,
              alarm_state,
              first_seen_at,
              last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', NOW(), NOW())
            `,
            [
              source,
              customer_type,
              server,
              device.name,
              device.group || null,
              device.olt_ip || null,
              device.slot_port || null,
              device.status || null,
              device.last_cek_cacty || null,
            ]
          );

          result.new.push(device);

          continue;
        }

        /*
         * Pernah ada, tetapi sebelumnya recovered.
         * Artinya down lagi.
         */
        if (existing.alarm_state === "RECOVERED") {
          await pool.query(
            `
            UPDATE cacti_alarms
            SET
              group_name = ?,
              olt_ip = ?,
              slot_port = ?,
              alarm_status = ?,
              last_cacti_check = ?,
              alarm_state = 'ACTIVE',
              first_seen_at = NOW(),
              last_seen_at = NOW(),
              recovered_at = NULL
            WHERE id = ?
            `,
            [
              device.group || null,
              device.olt_ip || null,
              device.slot_port || null,
              device.status || null,
              device.last_cek_cacty || null,
              existing.id
            ]
          );

          result.new.push(device);

          continue;
        }

        /*
         * Masih alarm yang sama.
         */
        await pool.query(
          `
          UPDATE cacti_alarms
          SET
            group_name = ?,
            olt_ip = ?,
            slot_port = ?,
            alarm_status = ?,
            last_cacti_check = ?,
            last_seen_at = NOW()
          WHERE id = ?
          `,
          [
            device.group || null,
            device.olt_ip || null,
            device.slot_port || null,
            device.status || null,
            device.last_cek_cacty || null,
            existing.id
          ]
        );

        result.existing.push(device);
      }

      /*
       * ===================================================
       * RECOVERED
       * ===================================================
       *
       * ACTIVE sebelumnya tetapi sekarang tidak ada
       * di hasil scan.
       */

      for (const previous of activeRows) {
        if (!currentNames.has(previous.device_name)) {
          await pool.query(
            `
            UPDATE cacti_alarms
            SET
              alarm_state = 'RECOVERED',
              recovered_at = NOW()
            WHERE id = ?
            `,
            [previous.id]
          );

          result.recovered.push({
            name: previous.device_name,
            group: previous.group_name,
            olt_ip: previous.olt_ip,
            slot_port: previous.slot_port,
            status: previous.alarm_status,
            customer_type,
            server
          });
        }
      }
    }

    console.log("CACTI RESULT:", {
      customer_type,
      collector,
      new: result.new.length,
      existing: result.existing.length,
      recovered: result.recovered.length,
      skipped_servers: result.skipped_servers,
    });

    return res.json({
      success: true,
      customer_type,
      collector,

      summary: {
        received: devices.length,
        new: result.new.length,
        existing: result.existing.length,
        recovered: result.recovered.length,
        skipped_servers: result.skipped_servers.length
      },

      result,
    });

  } catch (error) {
    next(error);
  }
}

module.exports = {
  receiveCacti
};
