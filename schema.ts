import { sql } from 'drizzle-orm';
import { pgTable, text, timestamp, uuid, index, uniqueIndex, check } from 'drizzle-orm/pg-core';

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
