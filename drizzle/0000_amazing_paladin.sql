CREATE TABLE "media_files" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" text NOT NULL,
	"type" text NOT NULL,
	"file_url" text NOT NULL,
	"prompt" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "media_files_type_check" CHECK ("media_files"."type" in ('image_input', 'image_generated'))
);
--> statement-breakpoint
CREATE TABLE "media_generation_jobs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_id" text NOT NULL,
	"request_id" uuid NOT NULL,
	"prompt" text NOT NULL,
	"state" text DEFAULT 'submitting' NOT NULL,
	"prediction_id" text,
	"error_code" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "media_jobs_state_check" CHECK ("media_generation_jobs"."state" in ('submitting', 'processing', 'succeeded', 'failed', 'canceled', 'submission_unknown'))
);
--> statement-breakpoint
CREATE INDEX "media_files_session_created_idx" ON "media_files" USING btree ("session_id","created_at","id");--> statement-breakpoint
CREATE UNIQUE INDEX "media_jobs_session_request_idx" ON "media_generation_jobs" USING btree ("session_id","request_id");