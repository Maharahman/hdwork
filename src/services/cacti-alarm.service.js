function displayValue(value) {
  return value || "-";
}

function formatAlarmMessage(customerType, devices) {
  const label = String(customerType || "").toUpperCase();
  const rows = [`⚠️ ALARM CACTI ${label}`, ""];

  devices.forEach((device, index) => {
    rows.push(`${index + 1}. ${displayValue(device.group || device.group_name)}`);
    rows.push(`   ${displayValue(device.name || device.device_name)}`);
    rows.push(`   OLT IP: ${displayValue(device.olt_ip)}`);
    rows.push(`   Slot/Port: ${displayValue(device.slot_port)}`);
    rows.push(`   Status: ${displayValue(device.status || device.alarm_status)}`);
    rows.push(`   Last Cacti: ${displayValue(device.last_cek_cacty || device.last_cacti_check)}`);

    if (index < devices.length - 1) rows.push("");
  });

  return rows.join("\n");
}

function formatCollectorHealthMessage(health) {
  const rows = ["⚠️ JALUR VPN / CACTI TERPUTUS", "", `Collector: ${displayValue(health.collector)}`];

  health.problems.forEach((problem) => rows.push(`• ${problem}`));

  rows.push("", "Pemeriksaan berikutnya: maksimal 10 menit lagi.");
  return rows.join("\n");
}

function formatCollectorRecoveredMessage(collector) {
  return [
    "✅ JALUR VPN / CACTI PULIH",
    "",
    `Collector: ${displayValue(collector)}`,
    "Minimal satu jalur Cacti kembali dapat diakses. Pengiriman alarm ONT dilanjutkan.",
  ].join("\n");
}

module.exports = {
  formatAlarmMessage,
  formatCollectorHealthMessage,
  formatCollectorRecoveredMessage,
};
