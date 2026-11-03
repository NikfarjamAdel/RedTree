-- 33_pipeline_constraint_tests.sql
--
-- Negative and positive tests for the pipeline's constraints (email,
-- receipts, validation, rules, categorisation, review, expenses,
-- properties). Each test is valid in every respect except the one thing
-- under test, and passes only if the error names the expected constraint.
-- Positive tests (expected = NULL) must succeed.
--
-- Everything runs in one transaction that is rolled back: the database is
-- left unchanged. Results live in a table variable, which survives the
-- rollback. The LAST result set lists failures only, so build_db.py can
-- treat an empty last result as a pass.

-- Refuse to run anywhere but a project database: never a system database
-- (the original accident was a script creating tables in master), and only
-- where the schema exists. No database name is assumed.
IF DB_NAME() IN (N'master', N'model', N'msdb', N'tempdb')
    THROW 50000, 'This is a system database: connect to the project database.', 1;
IF OBJECT_ID('dbo.LISTING', 'U') IS NULL
    THROW 50000, 'No project schema here: build it first (build_db.py).', 1;

SET XACT_ABORT OFF;    -- a failed test statement must not abort the batch
SET NOCOUNT ON;

DECLARE @results TABLE (n INT, test NVARCHAR(100), expected NVARCHAR(100),
                        outcome NVARCHAR(4), detail NVARCHAR(400));
DECLARE @tests TABLE (n INT IDENTITY(1,1), test NVARCHAR(100),
                      expected NVARCHAR(100), stmt NVARCHAR(MAX));

DECLARE @listing INT = (SELECT TOP 1 listing_id FROM LISTING WHERE default_for_receipts = 1);
DECLARE @property INT = (SELECT property_id FROM LISTING WHERE listing_id = @listing);
DECLARE @mid NVARCHAR(100) = N'<negative-test-' + CONVERT(NVARCHAR(36), NEWID()) + N'@redtree>';
DECLARE @email INT, @receipt INT, @other_property INT;

IF @listing IS NULL
    THROW 50001, 'No default listing: load a property with setup_property.py first.', 1;

BEGIN TRANSACTION;

-- Fixtures: a valid email, a valid receipt, and a second property
INSERT INTO EMAIL_RAW (message_id, received_at, sender, subject, parsed_as)
VALUES (@mid, SYSDATETIME(), N'test@example.com', N'negative test', N'receipt');
SET @email = SCOPE_IDENTITY();

INSERT INTO RECEIPT (email_id, attachment_index, blob_url, original_filename,
                     content_type, file_size_bytes, file_sha256)
VALUES (@email, 1, N'https://example.com/test.pdf', N'test.pdf',
        N'application/pdf', 10000, REPLICATE('a', 64));
SET @receipt = SCOPE_IDENTITY();

INSERT INTO PROPERTY (property_key, property_name)
VALUES (N'test-' + LEFT(CONVERT(NVARCHAR(36), NEWID()), 8), N'Test property');
SET @other_property = SCOPE_IDENTITY();

INSERT INTO @tests (test, expected, stmt) VALUES
-- EMAIL_RAW
(N'parsed_as outside the allowed labels', N'CK_EMAIL_RAW_parsed_as',
 N'INSERT INTO EMAIL_RAW (message_id, received_at, sender, subject, parsed_as)
   VALUES (@mid + N''-1'', SYSDATETIME(), N''t@example.com'', N''t'', N''spam'')'),
(N'same Message-ID twice', N'UQ_EMAIL_RAW_message_id',
 N'INSERT INTO EMAIL_RAW (message_id, received_at, sender, subject, parsed_as)
   VALUES (@mid, SYSDATETIME(), N''t@example.com'', N''t'', N''receipt'')'),
(N'airbnb_ label is allowed', NULL,
 N'INSERT INTO EMAIL_RAW (message_id, received_at, sender, subject, parsed_as)
   VALUES (@mid + N''-3'', SYSDATETIME(), N''t@example.com'', N''t'', N''airbnb_cancelled'')'),

-- RECEIPT
(N'receipt for a missing email', N'FK_RECEIPT_EMAIL_RAW',
 N'INSERT INTO RECEIPT (email_id, attachment_index, blob_url, original_filename,
                        content_type, file_size_bytes, file_sha256)
   VALUES (-1, 1, N''https://example.com/t'', N''t.pdf'', N''application/pdf'', 10000, REPLICATE(''a'', 64))'),
(N'same email and attachment index twice', N'UQ_RECEIPT_email_attachment',
 N'INSERT INTO RECEIPT (email_id, attachment_index, blob_url, original_filename,
                        content_type, file_size_bytes, file_sha256)
   VALUES (@email, 1, N''https://example.com/t'', N''t.pdf'', N''application/pdf'', 10000, REPLICATE(''a'', 64))'),
(N'status outside the allowed list', N'CK_RECEIPT_status',
 N'INSERT INTO RECEIPT (email_id, attachment_index, blob_url, original_filename,
                        content_type, file_size_bytes, file_sha256, status)
   VALUES (@email, 2, N''https://example.com/t'', N''t.pdf'', N''application/pdf'', 10000, REPLICATE(''a'', 64), N''archived'')'),

-- RECEIPT_ISSUE
(N'issue is allowed', NULL,
 N'INSERT INTO RECEIPT_ISSUE (receipt_id, check_name, detail) VALUES (@receipt, N''low_confidence'', N''t'')'),
(N'same check twice on one receipt', N'UQ_RECEIPT_ISSUE',
 N'INSERT INTO RECEIPT_ISSUE (receipt_id, check_name, detail) VALUES (@receipt, N''low_confidence'', N''t'')'),
(N'unknown check name', N'CK_RECEIPT_ISSUE_check_name',
 N'INSERT INTO RECEIPT_ISSUE (receipt_id, check_name, detail) VALUES (@receipt, N''typo_check'', N''t'')'),
(N'resolution note without resolved_at', N'CK_RECEIPT_ISSUE_resolution',
 N'INSERT INTO RECEIPT_ISSUE (receipt_id, check_name, resolution) VALUES (@receipt, N''gst_too_high'', N''note'')'),

-- MERCHANT_RULE
(N'ABN that is not 11 digits', N'CK_MERCHANT_RULE_match_value',
 N'INSERT INTO MERCHANT_RULE (match_type, match_value, merchant_name) VALUES (N''abn'', N''1234'', N''X'')'),
(N'sender address with capitals', N'CK_MERCHANT_RULE_match_value',
 N'INSERT INTO MERCHANT_RULE (match_type, match_value, merchant_name) VALUES (N''sender'', N''Upper@Example.com'', N''X'')'),
(N'listing scope without a listing', N'CK_MERCHANT_RULE_scope',
 N'INSERT INTO MERCHANT_RULE (match_type, match_value, merchant_name, scope)
   VALUES (N''sender'', N''scope-test@example.com'', N''X'', N''listing'')'),
(N'rule with an unknown category', N'FK_MERCHANT_RULE_CATEGORY',
 N'INSERT INTO MERCHANT_RULE (match_type, match_value, merchant_name, category)
   VALUES (N''sender'', N''cat-test@example.com'', N''X'', N''utilities'')'),
(N'rule source outside manual/learned', N'CK_MERCHANT_RULE_source',
 N'INSERT INTO MERCHANT_RULE (match_type, match_value, merchant_name, source)
   VALUES (N''sender'', N''source-test@example.com'', N''X'', N''guessed'')'),
(N'name-only sender rule is allowed', NULL,
 N'INSERT INTO MERCHANT_RULE (match_type, match_value, merchant_name)
   VALUES (N''sender'', N''negative-test@example.com'', N''X'')'),

-- RECEIPT_CATEGORISATION
(N'answer with an unknown category', N'FK_RECEIPT_CATEGORISATION_CATEGORY',
 N'INSERT INTO RECEIPT_CATEGORISATION (receipt_id, model, prompt_version, category, raw_response)
   VALUES (@receipt, N''test-model'', N''t1'', N''utilities'', N''{}'')'),
(N'set_field without a field and value', N'CK_RECEIPT_CATEGORISATION_set_field',
 N'INSERT INTO RECEIPT_CATEGORISATION (receipt_id, model, prompt_version, action, raw_response)
   VALUES (@receipt, N''test-model'', N''t2'', N''set_field'', N''{}'')'),
(N'field and value without set_field', N'CK_RECEIPT_CATEGORISATION_set_field',
 N'INSERT INTO RECEIPT_CATEGORISATION (receipt_id, model, prompt_version, action, action_field, action_value, raw_response)
   VALUES (@receipt, N''test-model'', N''t3'', N''accept'', N''total'', N''1.00'', N''{}'')'),
(N'accept with no field is allowed', NULL,
 N'INSERT INTO RECEIPT_CATEGORISATION (receipt_id, model, prompt_version, action, raw_response)
   VALUES (@receipt, N''test-model'', N''t4'', N''accept'', N''{}'')'),
(N'same receipt, model and prompt twice', N'UQ_RECEIPT_CATEGORISATION',
 N'INSERT INTO RECEIPT_CATEGORISATION (receipt_id, model, prompt_version, action, raw_response)
   VALUES (@receipt, N''test-model'', N''t4'', N''accept'', N''{}'')'),

-- RECEIPT_REVIEW
(N'not_expense without a reason', N'CK_RECEIPT_REVIEW_reason_needed',
 N'INSERT INTO RECEIPT_REVIEW (receipt_id, decision) VALUES (@receipt, N''not_expense'')'),
(N'duplicate without naming the original', N'CK_RECEIPT_REVIEW_duplicate',
 N'INSERT INTO RECEIPT_REVIEW (receipt_id, decision, not_expense_reason)
   VALUES (@receipt, N''not_expense'', N''duplicate'')'),
(N'not_expense carrying expense values', N'CK_RECEIPT_REVIEW_no_values',
 N'INSERT INTO RECEIPT_REVIEW (receipt_id, decision, not_expense_reason, category)
   VALUES (@receipt, N''not_expense'', N''report'', N''supplies'')'),
(N'duplicate naming the original is allowed', NULL,
 N'INSERT INTO RECEIPT_REVIEW (receipt_id, decision, not_expense_reason, duplicate_of_receipt_id)
   VALUES (@receipt, N''not_expense'', N''duplicate'', @receipt)'),

-- EXPENSE: scope, property and listing
(N'property scope with a listing', N'CK_EXPENSE_scope_listing',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, extracted_by)
   VALUES (@property, @listing, ''2026-10-01'', 10.00, N''repairs'', N''periodic'', N''property'', N''manual'')'),
(N'expense for a property that does not exist', N'FK_EXPENSE_PROPERTY',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, extracted_by)
   VALUES (-1, NULL, ''2026-10-01'', 10.00, N''repairs'', N''periodic'', N''property'', N''manual'')'),
(N'listing from another property', N'FK_EXPENSE_LISTING_PROPERTY',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, extracted_by)
   VALUES (@other_property, @listing, ''2026-10-01'', 5.00, N''supplies'', N''consumable'', N''listing'', N''manual'')'),
(N'listing from its own property is allowed', NULL,
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, extracted_by)
   VALUES (@property, @listing, ''2026-10-01'', 5.00, N''supplies'', N''consumable'', N''listing'', N''manual'')'),

-- EXPENSE: ABN, invoice, receipt, source, category
(N'ABN failing the check digit', N'CK_EXPENSE_supplier_abn',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, supplier_abn, extracted_by)
   VALUES (@property, NULL, ''2026-10-01'', 10.00, N''repairs'', N''periodic'', N''property'',
           ''12345678901'', N''manual'')'),
(N'valid ABN with invoice is allowed', NULL,
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, supplier_abn, invoice_number, receipt_id, extracted_by)
   VALUES (@property, NULL, ''2026-10-01'', 10.00, N''repairs'', N''periodic'', N''property'',
           ''51824753556'', N''NEG-TEST-1'', @receipt, N''manual'')'),
(N'same supplier invoice twice', N'UX_EXPENSE_supplier_invoice',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, supplier_abn, invoice_number, extracted_by)
   VALUES (@property, NULL, ''2026-10-01'', 10.00, N''repairs'', N''periodic'', N''property'',
           ''51824753556'', N''NEG-TEST-1'', N''manual'')'),
(N'second expense from the same receipt', N'ux_expense_receipt',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, receipt_id, extracted_by)
   VALUES (@property, @listing, ''2026-10-01'', 5.00, N''supplies'', N''consumable'', N''listing'',
           @receipt, N''manual'')'),
(N'extracted_by outside pipeline/manual', N'CK_EXPENSE_extracted_by',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, extracted_by)
   VALUES (@property, @listing, ''2026-10-01'', 5.00, N''supplies'', N''consumable'', N''listing'', N''robot'')'),
(N'expense with an unknown category', N'FK_EXPENSE_CATEGORY',
 N'INSERT INTO EXPENSE (property_id, listing_id, expense_date, expense_amount, category,
                        expense_type, scope, extracted_by)
   VALUES (@property, @listing, ''2026-10-01'', 5.00, N''utilities'', N''consumable'', N''listing'', N''manual'')'),

-- LISTING: one default overall
(N'a second default listing', N'UX_LISTING_default',
 N'INSERT INTO LISTING (listing_address, listing_title, listing_status, listed_from,
                        property_id, default_for_receipts)
   VALUES (N''Test address'', N''Test listing'', N''active'', ''2026-01-01'', @other_property, 1)'),

-- CATEGORY and APPORTIONMENT_RULE
(N'category with an unknown expense type', N'CK_CATEGORY_default_expense_type',
 N'INSERT INTO CATEGORY (category_code, description, default_expense_type)
   VALUES (N''neg_test'', N''test'', N''sometimes'')'),
(N'apportionment for an unknown category', N'FK_APPORTIONMENT_RULE_CATEGORY',
 N'INSERT INTO APPORTIONMENT_RULE (listing_id, category, rate, valid_from, basis)
   VALUES (@listing, N''utilities'', 0.50, ''2030-01-01'', N''test'')');

-- Run each test
DECLARE @n INT = 1, @last INT = (SELECT MAX(n) FROM @tests);
DECLARE @test NVARCHAR(100), @expected NVARCHAR(100), @stmt NVARCHAR(MAX);

WHILE @n <= @last
BEGIN
    SELECT @test = test, @expected = expected, @stmt = stmt FROM @tests WHERE n = @n;
    BEGIN TRY
        EXEC sp_executesql @stmt,
             N'@email INT, @receipt INT, @listing INT, @property INT,
               @other_property INT, @mid NVARCHAR(100)',
             @email, @receipt, @listing, @property, @other_property, @mid;
        INSERT @results VALUES (@n, @test, ISNULL(@expected, N'(must succeed)'),
               CASE WHEN @expected IS NULL THEN N'PASS' ELSE N'FAIL' END,
               CASE WHEN @expected IS NULL THEN NULL ELSE N'insert succeeded' END);
    END TRY
    BEGIN CATCH
        INSERT @results VALUES (@n, @test, ISNULL(@expected, N'(must succeed)'),
               CASE WHEN @expected IS NOT NULL
                         AND ERROR_MESSAGE() LIKE N'%' + @expected + N'%'
                    THEN N'PASS' ELSE N'FAIL' END,
               LEFT(ERROR_MESSAGE(), 400));
    END CATCH;

    IF XACT_STATE() = -1 BREAK;       -- transaction unusable: stop; the rollback below still runs
    SET @n += 1;
END;

ROLLBACK TRANSACTION;                 -- leave the database exactly as it was

-- Every result, for reading
SELECT n, outcome, test, expected, detail FROM @results ORDER BY n;

-- Failures only: build_db.py treats this LAST result set as the verdict,
-- so it must be empty when every test passes.
SELECT n, test, expected, detail FROM @results WHERE outcome = 'FAIL' ORDER BY n;