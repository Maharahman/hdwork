module.exports = {
  apps: [{
    name: "datin-cacti-sync",
    cwd: __dirname,
    script: "reconcile.py",
    interpreter: process.env.PYTHON_BIN || "/usr/local/bin/python3",
    args: "--watch --apply --interval 600",
    autorestart: true,
    exp_backoff_restart_delay: 5000,
    time: true,
    env: { PYTHONUNBUFFERED: "1", PYTHONDONTWRITEBYTECODE: "1" },
  }],
};
