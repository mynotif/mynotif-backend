BEGIN;
--
-- Add field search_phone to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" DROP COLUMN "search_phone" CASCADE;
--
-- Add field search_city to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" DROP COLUMN "search_city" CASCADE;
--
-- Add field search_lastname to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" DROP COLUMN "search_lastname" CASCADE;
--
-- Add field search_firstname to patient
--
SET LOCAL lock_timeout = '2s';
ALTER TABLE "nurse_patient" DROP COLUMN "search_firstname" CASCADE;
COMMIT;
