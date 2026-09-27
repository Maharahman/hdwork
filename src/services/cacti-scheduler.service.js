const pool = require("../config/database");
const telegramRepository = require("../repositories/telegram.repository");
const telegramService = require("./telegram.service");
const {
  formatAlarmMessage,
  formatCollectorHealthMessage,
  formatCollectorRecoveredMessage,
} = require("./cacti-alarm.service");

const TELEGRAM_MESSAGE_LIMIT = 3900;
let schedulerStarted = false;
let schedulerRunning = false;

function safeJson(value, fallback) {
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function splitAlarmDevices(customerType, devices) {
  const batches = [];
  let batch = [];

  for (const device of devices) {
    const candidate = [...batch, device];
    if (batch.length > 0 && formatAlarmMessage(customerType, candidate).length > TELEGRAM_MESSAGE_LIMIT) {
      batches.push(batch);
      batch = [device];
    } else {
      batch = candidate;
    }
  }

  if (batch.length > 0) batches.push(batch);
  return batches;
}

async function recordCollectorHealth({ collector, scans = [] }) {
  const failedScans = scans.filter((scan) => scan.status !== "success" && scan.failure_kind !== "login");
  const scanSummary = scans.map((scan) => ({
    server: scan.server,
    status: scan.status,
    error: scan.error || null,
    failure_kind: scan.failure_kind || null,
  }));

  await pool.query(
    `
    INSERT INTO cacti_collector_health (
      collector, customer_type, last_received_at,
      last_scan_failed_count, last_scan_summary
    ) VALUES (?, 'GLOBAL', NOW(), ?, ?)
    ON DUPLICATE KEY UPDATE
      last_received_at = NOW(),
      last_scan_failed_count = VALUES(last_scan_failed_count),
      last_scan_summary = VALUES(last_scan_summary)
    `,
    [
      collector || "UNKNOWN_COLLECTOR",
      failedScans.length,
      JSON.stringify(scanSummary),
    ],
  );
}

async function sendActiveAlarms() {
  const [rows] = await pool.query(
    `SELECT * FROM cacti_alarms WHERE alarm_state = 'ACTIVE' ORDER BY customer_type, server, group_name, device_name`,
  );
  const devicesByCustomerType = new Map();
  for (const device of rows) {
    const key = String(device.customer_type || "").toLowerCase();
    if (!devicesByCustomerType.has(key)) devicesByCustomerType.set(key, []);
    devicesByCustomerType.get(key).push(device);
  }

  for (const [customerType, devices] of devicesByCustomerType) {
    const destination = await telegramRepository.getAlarmDestination(customerType);
    const batches = splitAlarmDevices(customerType, devices);

    for (const batch of batches) {
      const message = formatAlarmMessage(customerType, batch);
      if (destination) {
        const delivery = await telegramService.sendMessage(
          destination.telegram_chat_id,
          message,
          destination.telegram_thread_id,
        );
        if (!delivery.delivered) console.error(`Alarm Cacti ${customerType} gagal dikirim: ${delivery.error}`);
      }
    }

    const personalDevices = await personalAlarmDevices(customerType, devices);
    if (!destination && !personalDevices.size) {
      console.warn(`Tujuan alarm Telegram Cacti belum terdaftar: ${customerType}`);
      continue;
    }

    for (const { subscriber, devices: subscriberDevices } of personalDevices.values()) {
      for (const batch of splitAlarmDevices(customerType, subscriberDevices)) {
        const message = formatAlarmMessage(customerType, batch);
        const delivery = await telegramService.sendPersonalAlarmMessage(subscriber.telegram_chat_id, message);
        if (delivery.delivered) continue;

        console.error(`Alarm personal Cacti ${customerType} ke ${subscriber.telegram_user_id} gagal dikirim: ${delivery.error}`);
        if (delivery.error_code === 403) {
          await telegramRepository.deactivateAlarmSubscriber(customerType, subscriber.telegram_user_id);
        }
      }
    }
  }
}

async function personalAlarmDevices(customerType, devices) {
  const recipients = new Map();
  if (customerType === "datin") {
    const subscribers = await telegramRepository.getDatinSubscribersByGroupNames(devices.map((device) => device.group_name || device.group));
    const subscribersByGroup = new Map();
    for (const subscriber of subscribers) {
      const key = subscriber.normalized_name;
      if (!subscribersByGroup.has(key)) subscribersByGroup.set(key, []);
      subscribersByGroup.get(key).push(subscriber);
    }
    for (const device of devices) {
      const group = telegramRepository.normalizeDatinGroupName(device.group_name || device.group);
      for (const subscriber of subscribersByGroup.get(group) || []) {
        const key = String(subscriber.telegram_user_id);
        if (!recipients.has(key)) recipients.set(key, { subscriber, devices: [] });
        recipients.get(key).devices.push(device);
      }
    }
    return recipients;
  }

  for (const subscriber of await telegramRepository.getAlarmSubscribers(customerType)) {
    recipients.set(String(subscriber.telegram_user_id), { subscriber, devices: [...devices] });
  }
  return recipients;
}

function healthProblems(row) {
  const scans = safeJson(row.last_scan_summary, []);
  if (!scans.length || scans.some((scan) => scan.status === "success")) return [];

  const connectionFailures = scans.filter((scan) => scan.failure_kind === "connection");
  if (connectionFailures.length !== scans.length) return [];

  return [
    `Semua jalur Cacti tidak dapat diakses: ${connectionFailures.map((scan) => scan.server).join(", ")}.`,
  ];
}

async function sendHealthToAlarmGroups(text) {
  for (const customerType of ["nodeb", "datin"]) {
    const destination = await telegramRepository.getAlarmDestination(customerType);
    if (!destination) continue;
    const delivery = await telegramService.sendMessage(destination.telegram_chat_id, text, destination.telegram_thread_id);
    if (!delivery.delivered) console.error(`Health collector ${customerType} gagal dikirim: ${delivery.error}`);
  }
}

async function sendCollectorHealth() {
  const [rows] = await pool.query(
    `
    SELECT *
    FROM cacti_collector_health
    WHERE customer_type = 'GLOBAL'
    `,
  );

  let vpnDisconnected = false;
  for (const row of rows) {
    const problems = healthProblems(row);
    const unhealthy = problems.length > 0;
    if (unhealthy) vpnDisconnected = true;

    if (unhealthy) {
      await sendHealthToAlarmGroups(formatCollectorHealthMessage({ collector: row.collector, problems }));
    }
    if (!unhealthy && row.last_alert_state === "UNHEALTHY") {
      await sendHealthToAlarmGroups(formatCollectorRecoveredMessage(row.collector));
    }

    await pool.query(
      "UPDATE cacti_collector_health SET last_alert_state = ?, last_health_checked_at = NOW() WHERE id = ?",
      [unhealthy ? "UNHEALTHY" : "HEALTHY", row.id],
    );
  }
  return vpnDisconnected;
}

async function runScheduledNotifications() {
  if (schedulerRunning) return;
  schedulerRunning = true;
  try {
    const vpnDisconnected = await sendCollectorHealth();
    if (!vpnDisconnected) await sendActiveAlarms();
  } catch (error) {
    console.error("Penjadwalan alarm Cacti gagal:", error.message);
  } finally {
    schedulerRunning = false;
  }
}

function millisecondsUntilNextTenMinuteMark(now = new Date()) {
  const next = new Date(now);
  next.setSeconds(0, 0);
  const minutesUntilNextMark = 10 - (next.getMinutes() % 10);
  next.setMinutes(next.getMinutes() + minutesUntilNextMark);
  return next.getTime() - now.getTime();
}

function scheduleNextRun() {
  const delay = millisecondsUntilNextTenMinuteMark();
  setTimeout(async () => {
    await runScheduledNotifications();
    scheduleNextRun();
  }, delay);
}

function startCactiScheduler() {
  if (schedulerStarted) return;
  schedulerStarted = true;
  scheduleNextRun();
  console.log("⏱️ Alarm Cacti dijadwalkan pada menit 00, 10, 20, 30, 40, dan 50");
}

module.exports = {
  recordCollectorHealth,
  runScheduledNotifications,
  startCactiScheduler,
  splitAlarmDevices,
  healthProblems,
  millisecondsUntilNextTenMinuteMark,
};
