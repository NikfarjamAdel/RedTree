-- 34_ical_change_tests.sql
--
-- Scenario test for vw_ical_changes: two made-up snapshots (dated 2030,
-- after all real ones) holding one booking of each kind, compared with the
-- expected changes. Everything is rolled back; results survive in table
-- variables. Expect NO rows: any row is a missing or unexpected change.

-- Refuse to run anywhere but a project database: never a system database
-- (the original accident was a script creating tables in master), and only
-- where the schema exists. No database name is assumed.
IF DB_NAME() IN (N'master', N'model', N'msdb', N'tempdb')
    THROW 50000, 'This is a system database: connect to the project database.', 1;
IF OBJECT_ID('dbo.LISTING', 'U') IS NULL
    THROW 50000, 'No project schema here: build it first (build_db.py).', 1;

SET XACT_ABORT OFF;
SET NOCOUNT ON;

DECLARE @expected TABLE (code NVARCHAR(20), change_type NVARCHAR(30));
DECLARE @actual   TABLE (code NVARCHAR(20), change_type NVARCHAR(30));
DECLARE @s1 INT, @s2 INT;
DECLARE @listing INT = (SELECT TOP 1 listing_id FROM LISTING WHERE default_for_receipts = 1);

IF @listing IS NULL
    THROW 50001, 'No default listing: load a property with setup_property.py first.', 1;

BEGIN TRANSACTION;

-- Snapshot 1 on 9 Jan 2030, snapshot 2 on 10 Jan 2030 (10:30 Adelaide)
INSERT INTO ICAL_SNAPSHOT (listing_id, fetched_at, blob_url, content_hash, parsed_at)
VALUES (@listing, '2030-01-09T00:00:00', N'test://snapshot-1', REPLICATE('0', 64), SYSDATETIME());
SET @s1 = SCOPE_IDENTITY();
INSERT INTO ICAL_SNAPSHOT (listing_id, fetched_at, blob_url, content_hash, parsed_at)
VALUES (@listing, '2030-01-10T00:00:00', N'test://snapshot-2', REPLICATE('1', 64), SYSDATETIME());
SET @s2 = SCOPE_IDENTITY();

INSERT INTO ICAL_EVENT (snapshot_id, uid, event_kind, start_date, end_date, confirmation_code) VALUES
    -- in snapshot 1 only
    (@s1, N'u-cancel', 'reserved', '2030-02-01', '2030-02-03', N'TESTCANCEL'),  -- not yet started: cancelled
    (@s1, N'u-ended',  'reserved', '2030-01-05', '2030-01-08', N'TESTENDED'),   -- already over: ignored
    (@s1, N'u-during', 'reserved', '2030-01-08', '2030-01-12', N'TESTDURING'),  -- in progress: removed_during_stay
    -- in both
    (@s1, N'u-moved',  'reserved', '2030-03-01', '2030-03-04', N'TESTMOVED'),
    (@s2, N'u-moved',  'reserved', '2030-03-02', '2030-03-05', N'TESTMOVED'),   -- dates differ: dates_changed
    (@s1, N'u-same',   'reserved', '2030-04-01', '2030-04-03', N'TESTSAME'),
    (@s2, N'u-same',   'reserved', '2030-04-01', '2030-04-03', N'TESTSAME'),    -- unchanged: nothing
    -- in snapshot 2 only
    (@s2, N'u-new',    'reserved', '2030-05-01', '2030-05-02', N'TESTNEW');     -- new_booking

INSERT INTO @expected VALUES
    (N'TESTNEW',    'new_booking'),
    (N'TESTCANCEL', 'cancelled'),
    (N'TESTDURING', 'removed_during_stay'),
    (N'TESTMOVED',  'dates_changed');

INSERT INTO @actual
SELECT confirmation_code, change_type FROM vw_ical_changes WHERE snapshot_id = @s2;

ROLLBACK TRANSACTION;          -- leave the database exactly as it was

-- Failures only (the last result set): missing or unexpected changes
SELECT 'missing' AS problem, code, change_type
FROM (SELECT * FROM @expected EXCEPT SELECT * FROM @actual) m
UNION ALL
SELECT 'unexpected', code, change_type
FROM (SELECT * FROM @actual EXCEPT SELECT * FROM @expected) u;