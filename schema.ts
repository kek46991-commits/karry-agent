import { sql } from 'drizzle-orm';
import { pgTable, text, timestamp, uuid, doublePrecision, index, uniqueIndex, check } from 'drizzle-orm/pg-core';

export const mediaFiles = pgTable('media_files', {
  id: uuid('id').defaultRandom().primaryKey(),
  sessionId: text('session_id').notNull(),
  type: text('type').notNull(),
  fileUrl: text('file_url').notNull(),
  prompt: text('prompt'),
  createdAt: timestamp('created_at', { withTimezone: true }).defaultNow().notNull(),
}, table => [
  index('media_files_session_created_idx').on(table.sessionId, table.createdAt, table.id),
  check('media_files_type_check', sql`${table.type} in ('image_input', 'image_generated')`),
]);

// A create timeout is not permission to submit a second chargeable prediction.
export const mediaGenerationJobs = pgTable('media_generation_jobs', {
  id: uuid('id').defaultRandom().primaryKey(),
  sessionId: text('session_id').notNull(),
  requestId: uuid('request_id').notNull(),
  prompt: text('prompt').notNull(),
  state: text('state').notNull().default('submitting'),
  predictionId: text('prediction_id'),
  errorCode: text('error_code'),
  createdAt: timestamp('created_at', { withTimezone: true }).defaultNow().notNull(),
  updatedAt: timestamp('updated_at', { withTimezone: true }).defaultNow().notNull(),
}, table => [
  uniqueIndex('media_jobs_session_request_idx').on(table.sessionId, table.requestId),
  check('media_jobs_state_check', sql`${table.state} in ('submitting', 'processing', 'succeeded', 'failed', 'canceled', 'submission_unknown')`),
]);

export const mobileLocationHistory = pgTable('mobile_location_history', {
  id: uuid('id').defaultRandom().primaryKey(),
  deviceId: text('device_id').notNull(),
  latitude: doublePrecision('latitude').notNull(),
  longitude: doublePrecision('longitude').notNull(),
  accuracyM: doublePrecision('accuracy_m').notNull(),
  collectedAt: timestamp('collected_at', { withTimezone: true }).notNull(),
  receivedAt: timestamp('received_at', { withTimezone: true }).defaultNow().notNull(),
}, table => [
  index('mobile_location_device_collected_idx').on(table.deviceId, table.collectedAt, table.id),
  uniqueIndex('mobile_location_device_time_idx').on(table.deviceId, table.collectedAt),
  check('mobile_location_latitude_check', sql`${table.latitude} between -90 and 90`),
  check('mobile_location_longitude_check', sql`${table.longitude} between -180 and 180`),
  check('mobile_location_accuracy_check', sql`${table.accuracyM} between 0 and 100000`),
]);
