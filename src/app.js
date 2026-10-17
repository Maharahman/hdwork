const express = require("express");
const cors = require("cors");
const path = require("path");

const orderRoutes = require("./routes/order.routes")
const attendanceRoutes = require("./routes/attendance.routes");
const dashboardRoutes = require("./routes/dashboard.routes");
const authRoutes = require("./routes/auth.routes");
const userRoutes = require("./routes/user.routes");
const groupRoutes = require("./routes/group.routes");
const agentRoutes = require("./routes/agent.routes");

const networkRoutes = require("./routes/network.routes");

const cactyRoutes = require("./routes/cacty.routes")

const app = express();

app.use(cors());
// The spreadsheet snapshot has its own bounded parser; register before the default 100 KB parser.
app.use("/api/datin-sheet", require("./routes/datin-sheet-sync.routes"));
app.use(express.json());
app.use("/uploads", express.static(path.join(__dirname, "../uploads")));

app.get("/", (req, res) => {
  res.json({
    message: "HD Work API berjalan",
  });
});

app.use("/api/auth", authRoutes);
app.use("/api/users", userRoutes);
app.use("/api/groups", groupRoutes);
app.use("/api/agents", agentRoutes);

app.use("/api/orders", orderRoutes)

app.use("/api/attendance", attendanceRoutes);

app.use("/api/dashboard", dashboardRoutes);

app.use("/api/network", networkRoutes);

app.use("/api/cactys", cactyRoutes);

app.use((error, req, res, next) => {
  if (error?.code === "LIMIT_FILE_SIZE") {
    return res.status(400).json({
      success: false,
      message: "Ukuran setiap file maksimal 10 MB",
    });
  }

  if (error?.name === "MulterError") {
    return res.status(400).json({
      success: false,
      message: "Lampiran tidak dapat diproses. Maksimal 10 file.",
    });
  }

  if (error?.message === "Tipe file tidak didukung") {
    return res.status(400).json({
      success: false,
      message: error.message,
    });
  }

  console.error(error);

  return res.status(500).json({
    success: false,
    message: "Gagal memproses permintaan",
  });
});



module.exports = app;
