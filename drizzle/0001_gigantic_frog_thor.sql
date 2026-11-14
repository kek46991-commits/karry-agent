CREATE TABLE "mobile_location_history" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"device_id" text NOT NULL,
	"latitude" double precision NOT NULL,
	"longitude" double precision NOT NULL,
	"accuracy_m" double precision NOT NULL,
	"collected_at" timestamp with time zone NOT NULL,
	"received_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "mobile_location_latitude_check" CHECK ("mobile_location_history"."latitude" between -90 and 90),
	CONSTRAINT "mobile_location_longitude_check" CHECK ("mobile_location_history"."longitude" between -180 and 180),
	CONSTRAINT "mobile_location_accuracy_check" CHECK ("mobile_location_history"."accuracy_m" between 0 and 100000)
);
--> statement-breakpoint
CREATE INDEX "mobile_location_device_collected_idx" ON "mobile_location_history" USING btree ("device_id","collected_at","id");--> statement-breakpoint
CREATE UNIQUE INDEX "mobile_location_device_time_idx" ON "mobile_location_history" USING btree ("device_id","collected_at");