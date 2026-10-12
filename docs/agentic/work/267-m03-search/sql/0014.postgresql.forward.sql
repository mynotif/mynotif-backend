BEGIN;
--
-- Add field search_firstname to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" ADD COLUMN "search_firstname" text NULL;
--
-- Add field search_lastname to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" ADD COLUMN "search_lastname" text NULL;
--
-- Add field search_city to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" ADD COLUMN "search_city" text NULL;
--
-- Add field search_phone to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" ADD COLUMN "search_phone" text NULL;
COMMIT;
