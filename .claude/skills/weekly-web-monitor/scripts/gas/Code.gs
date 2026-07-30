/**
 * Optional Outbox dispatcher. Set SLACK_WEBHOOK_URL only in Apps Script
 * Properties. Never store the URL in Sheets or source control.
 */
const OUTBOX_SHEET = 'Outbox';
const MAX_ATTEMPTS = 5;
const BATCH_SIZE = 20;

function dispatchOutbox() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const sheet = SpreadsheetApp.getActive().getSheetByName(OUTBOX_SHEET);
    if (!sheet) throw new Error('Outbox sheet is missing');
    const webhook = PropertiesService.getScriptProperties()
      .getProperty('SLACK_WEBHOOK_URL');
    if (!webhook) throw new Error('SLACK_WEBHOOK_URL property is missing');

    const values = sheet.getDataRange().getValues();
    if (values.length < 2) return;
    const headers = values[0].map(String);
    const required = [
      'event_id', 'target_id', 'payload', 'status', 'attempt_count',
      'created_at', 'updated_at', 'next_attempt_at', 'last_error'
    ];
    required.forEach((name) => {
      if (headers.indexOf(name) < 0) throw new Error(`Missing column: ${name}`);
    });
    const column = Object.fromEntries(headers.map((name, index) => [name, index]));
    const sentEvents = new Set(
      values.slice(1)
        .filter((row) => String(row[column.status]) === 'sent')
        .map((row) => String(row[column.event_id]))
    );
    const handledEvents = new Set();
    let handled = 0;
    for (let rowIndex = 1; rowIndex < values.length && handled < BATCH_SIZE; rowIndex++) {
      const row = values[rowIndex];
      const status = String(row[column.status]);
      if (status !== 'pending' && status !== 'retry') continue;
      const eventId = String(row[column.event_id]);
      if (sentEvents.has(eventId) || handledEvents.has(eventId)) {
        updateOutbox_(sheet, rowIndex + 1, column, 'poison',
                      Number(row[column.attempt_count]) || 0, '', 'duplicate_event_id');
        continue;
      }
      handledEvents.add(eventId);
      const nextAttempt = row[column.next_attempt_at];
      if (nextAttempt && new Date(nextAttempt).getTime() > Date.now()) continue;
      handled++;
      dispatchRow_(sheet, rowIndex + 1, row, column, webhook);
    }
  } finally {
    lock.releaseLock();
  }
}

function dispatchRow_(sheet, rowNumber, row, column, webhook) {
  let payload;
  try {
    payload = JSON.parse(String(row[column.payload]));
    if (typeof payload.message !== 'string' ||
        typeof payload.notification_group !== 'string') {
      throw new Error('invalid payload');
    }
  } catch (error) {
    updateOutbox_(sheet, rowNumber, column, 'poison',
                  Number(row[column.attempt_count]) || 0, '', 'outbox_payload_invalid');
    return;
  }

  const attempts = (Number(row[column.attempt_count]) || 0) + 1;
  // Persist "sending" before delivery. An interrupted ambiguous delivery is not
  // retried automatically, preventing duplicate successful sends.
  updateOutbox_(sheet, rowNumber, column, 'sending', attempts, '', '');
  SpreadsheetApp.flush();
  let response;
  try {
    response = UrlFetchApp.fetch(webhook, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({text: payload.message}),
      muteHttpExceptions: true
    });
  } catch (error) {
    // Transport failure is ambiguous: Slack may have accepted the message.
    // Keep "sending" so the dispatcher cannot duplicate it automatically.
    updateOutbox_(sheet, rowNumber, column, 'sending', attempts, '',
                  'delivery_ambiguous');
    return;
  }
  const code = response.getResponseCode();
  if (code >= 200 && code < 300) {
    updateOutbox_(sheet, rowNumber, column, 'sent', attempts, '', '');
    return;
  }
  const retryable = code === 429 || code >= 500;
  const exhausted = attempts >= MAX_ATTEMPTS;
  const shouldRetry = retryable && !exhausted;
  const delayMinutes = Math.min(60, Math.pow(2, attempts));
  const nextAttempt = shouldRetry ?
    new Date(Date.now() + delayMinutes * 60 * 1000).toISOString() : '';
  updateOutbox_(sheet, rowNumber, column,
                shouldRetry ? 'retry' : 'poison', attempts, nextAttempt,
                `slack_http_${code}`);
}

function updateOutbox_(sheet, rowNumber, column, status, attempts, nextAttempt, errorCode) {
  const now = new Date().toISOString();
  sheet.getRange(rowNumber, column.status + 1).setValue(status);
  sheet.getRange(rowNumber, column.attempt_count + 1).setValue(attempts);
  sheet.getRange(rowNumber, column.updated_at + 1).setValue(now);
  sheet.getRange(rowNumber, column.next_attempt_at + 1).setValue(nextAttempt);
  sheet.getRange(rowNumber, column.last_error + 1).setValue(errorCode);
}
