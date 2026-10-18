const pool = require("../config/database");

const alarmCategoryByCustomerType = {
  nodeb: "ALARM_NODEB",
  datin: "ALARM_DATIN",
};

function normalizeDatinGroupName(value) {
  return String(value || "").trim().replace(/\s+/g, " ").toUpperCase();
}

async function getCities() {
  const [rows] = await pool.query(
    "SELECT id, name FROM cities ORDER BY name ASC",
  );

  return rows;
}

async function getSegments() {
  const [rows] = await pool.query(
    "SELECT id, code FROM segments ORDER BY code ASC",
  );

  return rows;
}

async function getGroupByScope(cityId, segmentId, category) {
  const [rows] = await pool.query(
    `
    SELECT id, telegram_chat_id, telegram_thread_id, name, city_id, segment_id, category, is_active
    FROM telegram_groups
    WHERE city_id = ?
      AND segment_id = ?
      AND category = ?
    LIMIT 1
    `,
    [cityId, segmentId, category],
  );

  return rows[0] || null;
}

async function saveGroup({
  chatId,
  threadId,
  name,
  cityId,
  segmentId,
  category,
}) {
  const [result] = await pool.query(
    `
    INSERT INTO telegram_groups (
      telegram_chat_id,
      telegram_thread_id,
      name,
      city_id,
      segment_id,
      category,
      is_active
    )
    VALUES (?, ?, ?, ?, ?, ?, 1)
    ON DUPLICATE KEY UPDATE
      name = VALUES(name),
      city_id = VALUES(city_id),
      segment_id = VALUES(segment_id),
      category = VALUES(category),
      is_active = 1,
      updated_at = CURRENT_TIMESTAMP
    `,
    [chatId, threadId, name, cityId, segmentId, category],
  );

  return result;
}

async function getGroupByChatThread(chatId, threadId) {
  const [rows] = await pool.query(
    `
    SELECT id, name, category, is_active
    FROM telegram_groups
    WHERE telegram_chat_id = ? AND telegram_thread_id = ?
    LIMIT 1
    `,
    [chatId, threadId],
  );

  return rows[0] || null;
}

async function moveGroupBinding({ groupId, chatId, threadId, name }) {
  const connection = await pool.getConnection();

  try {
    await connection.beginTransaction();

    const [existingBinding] = await connection.query(
      `
      SELECT id, name
      FROM telegram_groups
      WHERE telegram_chat_id = ? AND telegram_thread_id = ?
      FOR UPDATE
      `,
      [chatId, threadId],
    );

    if (existingBinding[0] && Number(existingBinding[0].id) !== Number(groupId)) {
      const error = new Error(
        `Grup/topic ini sudah dipakai oleh konfigurasi ${existingBinding[0].name}`,
      );
      error.statusCode = 409;
      throw error;
    }

    await connection.query(
      `
      UPDATE telegram_groups
      SET telegram_chat_id = ?, telegram_thread_id = ?, name = ?, is_active = 1, updated_at = CURRENT_TIMESTAMP
      WHERE id = ?
      `,
      [chatId, threadId, name, groupId],
    );

    await connection.commit();
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

function getAlarmCategory(customerType) {
  return alarmCategoryByCustomerType[String(customerType || "").toLowerCase()] || null;
}

async function saveAlarmGroup({ chatId, threadId, name, category }) {
  const connection = await pool.getConnection();

  try {
    await connection.beginTransaction();

    const [boundRows] = await connection.query(
      `
      SELECT id, category
      FROM telegram_groups
      WHERE telegram_chat_id = ? AND telegram_thread_id = ?
      FOR UPDATE
      `,
      [chatId, threadId],
    );

    const boundGroup = boundRows[0];
    if (boundGroup && boundGroup.category !== category) {
      const error = new Error("Grup/topic ini sudah dipakai oleh konfigurasi lain.");
      error.statusCode = 409;
      throw error;
    }

    const [categoryRows] = await connection.query(
      `
      SELECT id
      FROM telegram_groups
      WHERE category = ?
      FOR UPDATE
      `,
      [category],
    );

    const categoryGroup = categoryRows[0];
    if (categoryGroup) {
      await connection.query(
        `
        UPDATE telegram_groups
        SET telegram_chat_id = ?, telegram_thread_id = ?, name = ?, is_active = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        `,
        [chatId, threadId, name, categoryGroup.id],
      );
    } else {
      await connection.query(
        `
        INSERT INTO telegram_groups (
          telegram_chat_id,
          telegram_thread_id,
          name,
          city_id,
          segment_id,
          category,
          is_active
        )
        VALUES (?, ?, ?, NULL, NULL, ?, 1)
        `,
        [chatId, threadId, name, category],
      );
    }

    await connection.commit();
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

async function getAlarmDestination(customerType) {
  const category = getAlarmCategory(customerType);
  if (!category) return null;

  const [rows] = await pool.query(
    `
    SELECT telegram_chat_id, telegram_thread_id, name
    FROM telegram_groups
    WHERE category = ? AND is_active = 1
    LIMIT 1
    `,
    [category],
  );

  return rows[0] || null;
}

async function saveAlarmSubscriber({ customerType, userId, chatId, username, displayName }) {
  if (!getAlarmCategory(customerType)) {
    throw new Error("Jenis alarm tidak dikenal.");
  }

  await pool.query(
    `
    INSERT INTO telegram_alarm_subscribers (
      customer_type, telegram_user_id, telegram_chat_id, telegram_username, display_name, is_active
    )
    VALUES (?, ?, ?, ?, ?, 1)
    ON DUPLICATE KEY UPDATE
      telegram_chat_id = VALUES(telegram_chat_id),
      telegram_username = VALUES(telegram_username),
      display_name = VALUES(display_name),
      is_active = 1,
      updated_at = CURRENT_TIMESTAMP
    `,
    [String(customerType).toLowerCase(), userId, chatId, username || null, displayName || null],
  );
}

async function deactivateAlarmSubscriber(customerType, userId) {
  await pool.query(
    `
    UPDATE telegram_alarm_subscribers
    SET is_active = 0, updated_at = CURRENT_TIMESTAMP
    WHERE customer_type = ? AND telegram_user_id = ?
    `,
    [String(customerType).toLowerCase(), userId],
  );
}

async function getAlarmSubscribers(customerType) {
  const [rows] = await pool.query(
    `
    SELECT id, telegram_user_id, telegram_chat_id, telegram_username, display_name
    FROM telegram_alarm_subscribers
    WHERE customer_type = ? AND is_active = 1
    ORDER BY id ASC
    `,
    [String(customerType).toLowerCase()],
  );

  return rows;
}

async function getAlarmSubscriptions(userId) {
  const [rows] = await pool.query(
    `
    SELECT customer_type
    FROM telegram_alarm_subscribers
    WHERE telegram_user_id = ? AND is_active = 1
    ORDER BY customer_type ASC
    `,
    [userId],
  );

  return rows;
}

async function getDatinInstitutions(page = 0, pageSize = 8, database = pool) {
  const [[countRow]] = await database.query("SELECT COUNT(*) AS total FROM datin_institutions WHERE is_active = 1");
  const lastPage = Math.max(0, Math.ceil(Number(countRow.total) / pageSize) - 1);
  const currentPage = Math.min(lastPage, Math.max(0, Math.floor(Number(page) || 0)));
  const offset = currentPage * pageSize;
  const [rows] = await database.query(
    "SELECT id, name FROM datin_institutions WHERE is_active = 1 ORDER BY name ASC LIMIT ? OFFSET ?",
    [pageSize, offset],
  );
  return { institutions: rows, total: Number(countRow.total), page: Math.floor(offset / pageSize), pageSize };
}

async function getDatinSubscriptionIds(userId) {
  const [rows] = await pool.query(
    "SELECT institution_id FROM telegram_datin_subscriptions WHERE telegram_user_id = ? AND is_active = 1",
    [userId],
  );
  return rows.map((row) => Number(row.institution_id));
}

async function toggleDatinSubscription(userId, institutionId, database = pool) {
  const connection = await database.getConnection();
  try {
    await connection.beginTransaction();
    // A stale Telegram keyboard must not recreate a deleted institution/subscription.
    const [institutions] = await connection.query("SELECT id FROM datin_institutions WHERE id = ? AND is_active = 1 FOR UPDATE", [institutionId]);
    if (!institutions.length) { await connection.commit(); return null; }
    const [rows] = await connection.query(
      "SELECT is_active FROM telegram_datin_subscriptions WHERE telegram_user_id = ? AND institution_id = ? LIMIT 1",
      [userId, institutionId],
    );
    const nextActive = !(rows[0] && Number(rows[0].is_active) === 1);
    await connection.query(
      `
      INSERT INTO telegram_datin_subscriptions (telegram_user_id, institution_id, is_active)
      VALUES (?, ?, ?)
      ON DUPLICATE KEY UPDATE is_active = VALUES(is_active), updated_at = CURRENT_TIMESTAMP
      `,
      [userId, institutionId, Number(nextActive)],
    );
    await connection.commit();
    return nextActive;
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally { connection.release(); }
}

async function clearDatinSubscriptions(userId) {
  await pool.query(
    "UPDATE telegram_datin_subscriptions SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE telegram_user_id = ?",
    [userId],
  );
}

async function getDatinSubscriptionNames(userId) {
  const [rows] = await pool.query(
    `
    SELECT di.name
    FROM telegram_datin_subscriptions tds
    JOIN datin_institutions di ON di.id = tds.institution_id
    WHERE tds.telegram_user_id = ? AND tds.is_active = 1
    ORDER BY di.name ASC
    `,
    [userId],
  );
  return rows.map((row) => row.name);
}

async function getDatinSubscribersByGroupNames(groupNames) {
  const normalizedNames = [...new Set(groupNames.map(normalizeDatinGroupName).filter(Boolean))];
  if (!normalizedNames.length) return [];

  const placeholders = normalizedNames.map(() => "?").join(", ");
  const [rows] = await pool.query(
    `
    SELECT DISTINCT tas.telegram_user_id, tas.telegram_chat_id, dia.normalized_name
    FROM datin_institution_aliases dia
    JOIN telegram_datin_subscriptions tds
      ON tds.institution_id = dia.institution_id AND tds.is_active = 1
    JOIN telegram_alarm_subscribers tas
      ON tas.telegram_user_id = tds.telegram_user_id
      AND tas.customer_type = 'datin'
      AND tas.is_active = 1
    WHERE dia.normalized_name IN (${placeholders})
    `,
    normalizedNames,
  );
  return rows;
}

module.exports = {
  getCities,
  getSegments,
  getGroupByScope,
  getGroupByChatThread,
  moveGroupBinding,
  saveGroup,
  getAlarmCategory,
  getAlarmDestination,
  saveAlarmGroup,
  saveAlarmSubscriber,
  deactivateAlarmSubscriber,
  getAlarmSubscribers,
  getAlarmSubscriptions,
  normalizeDatinGroupName,
  getDatinInstitutions,
  getDatinSubscriptionIds,
  toggleDatinSubscription,
  clearDatinSubscriptions,
  getDatinSubscriptionNames,
  getDatinSubscribersByGroupNames,
};
