-- Never build into a system database (the original accident was this
-- script creating tables in master). No database name is assumed. Each
-- batch checks: a client that carries on after an error (SSMS, sqlcmd
-- without -b) would otherwise run the next batch anyway.
IF DB_NAME() IN (N'master', N'model', N'msdb', N'tempdb')
    THROW 50000, 'This is a system database: connect to the project database.', 1;

DROP TABLE IF EXISTS STOCK_MOVEMENT, MAINTENANCE, REVIEW, LOAD_LOG, STAY_NIGHT,
    PAYOUT, CLEANING, BOOKING_HISTORY, EXPENSE, RECEIPT_REVIEW,
    RECEIPT_CATEGORISATION, RECEIPT_ISSUE, MERCHANT_RULE, APPORTIONMENT_RULE,
    LOAN_INTEREST, LOAN, BOOKING_CORRECTION, ICAL_EVENT, ICAL_SNAPSHOT, BOOKING, BLOCKED_PERIOD, ITEM, GUEST, LISTING, PROPERTY, CALENDAR,
    RECEIPT, EMAIL_RAW, CATEGORY;
GO

IF DB_NAME() IN (N'master', N'model', N'msdb', N'tempdb')
    THROW 50000, 'This is a system database: connect to the project database.', 1;


-- A property: one physical place, with any number of listings and its loans.
-- Loaded from config/property.json by setup_property.py.
CREATE TABLE PROPERTY (
    property_id           INT IDENTITY(1,1) NOT NULL,
    property_key          NVARCHAR(30)    NOT NULL,     -- the config's key
    property_name         NVARCHAR(100)   NOT NULL,
    address               NVARCHAR(250)   NULL,
    description           NVARCHAR(2000)  NULL,         -- for the categorisation prompt
    categorisation_notes  NVARCHAR(2000)  NULL,         -- property-specific category rules
    CONSTRAINT PK_PROPERTY PRIMARY KEY (property_id),
    CONSTRAINT UQ_PROPERTY_key UNIQUE (property_key)
);

-- The settings below listed_from come from the property config; NULL means
-- the feature isn't used for that listing.
CREATE TABLE LISTING (
    listing_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_listing PRIMARY KEY,
    listing_address NVARCHAR(250),
    listing_title NVARCHAR(150),
    listing_status NVARCHAR(20) NOT NULL,
    listed_from DATE NOT NULL,
    listing_key                 NVARCHAR(30)   NULL,
    property_id                 INT           NOT NULL,
    airbnb_name                 NVARCHAR(100)  NULL,   -- as in the CSV's Listing column
    receipt_address             NVARCHAR(250)  NULL,   -- lower case, e.g. box+redtree@gmail.com
    default_for_receipts        BIT            NOT NULL
        CONSTRAINT DF_LISTING_default_for_receipts DEFAULT 0,
    ical_url_setting            NVARCHAR(100)  NULL,   -- NAME of the setting holding the URL
    cleaning_cost_per_turnover  DECIMAL(8,2)   NULL,   -- NULL = no imputed cleaning
    loan_interest_share         DECIMAL(5,4)   NULL,   -- NULL = no loan interest
    CONSTRAINT ck_listing_status CHECK (listing_status IN ('active', 'inactive')),
    CONSTRAINT FK_LISTING_PROPERTY FOREIGN KEY (property_id) REFERENCES PROPERTY (property_id),
    CONSTRAINT CK_LISTING_cleaning_cost CHECK (cleaning_cost_per_turnover >= 0),
    CONSTRAINT CK_LISTING_loan_share CHECK (loan_interest_share BETWEEN 0 AND 1),
    CONSTRAINT CK_LISTING_receipt_address CHECK (
        receipt_address LIKE '%_@_%'
        AND receipt_address = LOWER(receipt_address) COLLATE Latin1_General_BIN2),
    CONSTRAINT UQ_LISTING_listing_property UNIQUE (listing_id, property_id)
);

-- Keys and receipt addresses unique where set; at most one default
-- listing per property (zero is allowed: receipts then go to review)
CREATE UNIQUE INDEX UX_LISTING_key ON LISTING (listing_key)
    WHERE listing_key IS NOT NULL;
CREATE UNIQUE INDEX UX_LISTING_receipt_address ON LISTING (receipt_address)
    WHERE receipt_address IS NOT NULL;
CREATE UNIQUE INDEX UX_LISTING_default ON LISTING (default_for_receipts)
      WHERE default_for_receipts = 1;


-- The single list of expense categories. Descriptions double as the
-- definitions given to the LLM; default_expense_type is applied when a
-- receipt becomes an expense (review can override).
CREATE TABLE CATEGORY (
    category_code         NVARCHAR(30)   NOT NULL,
    description           NVARCHAR(200)  NOT NULL,
    default_expense_type  NVARCHAR(20)   NOT NULL,
    capital_life_months   INT            NULL,
    spread_months         INT          NULL,
    spread_anchor         NVARCHAR(5)  NULL,
    CONSTRAINT PK_CATEGORY PRIMARY KEY (category_code),
    CONSTRAINT CK_CATEGORY_default_expense_type
        CHECK (default_expense_type IN ('direct', 'periodic', 'capital', 'consumable')),
    CONSTRAINT CK_CATEGORY_capital_life_months CHECK (capital_life_months > 0),
    CONSTRAINT CK_CATEGORY_spread_months CHECK (spread_months > 0),
    CONSTRAINT CK_CATEGORY_spread_anchor CHECK (spread_anchor IN ('start', 'end')),
    CONSTRAINT CK_CATEGORY_spread_pair CHECK (
            (spread_months IS NULL     AND spread_anchor IS NULL)
            OR (spread_months IS NOT NULL AND spread_anchor IS NOT NULL))
);

-- Each listing's share of whole-property expenses, by category.
-- Periods are half-open: valid_from <= date < valid_to; NULL = still in force.
-- Overlapping periods and rates summing above 1 are checked by
-- assertions in 30_data_quality.sql (they span rows).
CREATE TABLE APPORTIONMENT_RULE (
    rule_id     INT IDENTITY(1,1) NOT NULL,
    listing_id  INT               NOT NULL,
    category    NVARCHAR(30)      NOT NULL,   -- match EXPENSE.category's type
    rate        DECIMAL(5,4)      NOT NULL,   -- 0.5000 = 50%
    valid_from  DATE              NOT NULL,
    valid_to    DATE              NULL,
    basis       NVARCHAR(200)     NULL,       -- why this rate

    CONSTRAINT PK_APPORTIONMENT_RULE PRIMARY KEY (rule_id),
    CONSTRAINT FK_APPORTIONMENT_RULE_LISTING
        FOREIGN KEY (listing_id) REFERENCES LISTING (listing_id),
    CONSTRAINT CK_APPORTIONMENT_RULE_rate
        CHECK (rate > 0 AND rate <= 1),
    CONSTRAINT CK_APPORTIONMENT_RULE_period
        CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT UQ_APPORTIONMENT_RULE
        UNIQUE (listing_id, category, valid_from),
    CONSTRAINT FK_APPORTIONMENT_RULE_CATEGORY
        FOREIGN KEY (category) REFERENCES CATEGORY (category_code)
);

-- The loans on the property. A loan is expected to have interest every
-- month from active_from until active_to (paid off; NULL = still active).
CREATE TABLE LOAN (
    loan_code    NVARCHAR(20)   NOT NULL,
    description  NVARCHAR(200)  NOT NULL,
    active_from  DATE           NOT NULL,    -- first month with interest (1st of month)
    active_to    DATE           NULL,        -- first month without (1st of month)
    charge_day   TINYINT        NOT NULL     -- day of month interest is charged
        CONSTRAINT DF_LOAN_charge_day DEFAULT 19,
    property_id  INT            NOT NULL,
    CONSTRAINT PK_LOAN PRIMARY KEY (loan_code),
    CONSTRAINT FK_LOAN_PROPERTY FOREIGN KEY (property_id) REFERENCES PROPERTY (property_id),
    CONSTRAINT CK_LOAN_active_from CHECK (DAY(active_from) = 1),
    CONSTRAINT CK_LOAN_active_to CHECK (
        active_to IS NULL OR (DAY(active_to) = 1 AND active_to > active_from)),
    CONSTRAINT CK_LOAN_charge_day CHECK (charge_day BETWEEN 1 AND 28)
);

-- Interest charged per loan per month, entered from each statement, so
-- every figure matches one statement. Not an expense: applied as a
-- listing share in vw_monthly_report only.
CREATE TABLE LOAN_INTEREST (
    loan_code        NVARCHAR(20)   NOT NULL,
    month_start      DATE           NOT NULL,   -- first day of the month
    interest_amount  DECIMAL(10,2)  NOT NULL,
    source_note      NVARCHAR(200)  NULL,       -- e.g. which statement
    entered_at       DATETIME2(0)   NOT NULL
        CONSTRAINT DF_LOAN_INTEREST_entered_at DEFAULT SYSDATETIME(),
    CONSTRAINT PK_LOAN_INTEREST PRIMARY KEY (loan_code, month_start),
    CONSTRAINT FK_LOAN_INTEREST_LOAN FOREIGN KEY (loan_code) REFERENCES LOAN (loan_code),
    CONSTRAINT CK_LOAN_INTEREST_first_day CHECK (DAY(month_start) = 1),
    CONSTRAINT CK_LOAN_INTEREST_amount CHECK (interest_amount >= 0)
);

CREATE TABLE MERCHANT_RULE (
    rule_id        INT IDENTITY(1,1) NOT NULL,
    match_type     NVARCHAR(10)   NOT NULL,   -- 'abn' or 'sender'
    match_value    NVARCHAR(250)  NOT NULL,   -- 11-digit ABN, or lower-case address
    merchant_name  NVARCHAR(100)  NOT NULL,   -- clean display name
    category       NVARCHAR(30)   NULL,       -- NULL = categorisation decides
    scope          NVARCHAR(10)   NULL,       -- NULL = decided per receipt
    listing_id     INT            NULL,
    date_order     CHAR(3)        NULL,       -- 'DMY' / 'MDY'; NULL = day first
    notes          NVARCHAR(200)  NULL,
    source         NVARCHAR(10)   NOT NULL
        CONSTRAINT DF_MERCHANT_RULE_source DEFAULT 'manual',
    created_at     DATETIME2(0)   NOT NULL
        CONSTRAINT DF_MERCHANT_RULE_created_at DEFAULT SYSDATETIME(),

    CONSTRAINT PK_MERCHANT_RULE PRIMARY KEY (rule_id),
    CONSTRAINT UQ_MERCHANT_RULE UNIQUE (match_type, match_value),
    CONSTRAINT FK_MERCHANT_RULE_CATEGORY
        FOREIGN KEY (category) REFERENCES CATEGORY (category_code),
    CONSTRAINT FK_MERCHANT_RULE_LISTING
        FOREIGN KEY (listing_id) REFERENCES LISTING (listing_id),
    CONSTRAINT CK_MERCHANT_RULE_match_type
        CHECK (match_type IN ('abn', 'sender')),
    CONSTRAINT CK_MERCHANT_RULE_date_order
        CHECK (date_order IN ('DMY', 'MDY')),
    CONSTRAINT CK_MERCHANT_RULE_source
        CHECK (source IN ('manual', 'learned')),
    -- an ABN is 11 digits; a sender is a lower-case address. BIN2 makes
    -- the comparison case-sensitive (the database default isn't).
    CONSTRAINT CK_MERCHANT_RULE_match_value CHECK (
           (match_type = 'abn'
            AND LEN(match_value) = 11
            AND match_value NOT LIKE '%[^0-9]%')
        OR (match_type = 'sender'
            AND match_value LIKE '%_@_%'
            AND match_value = LOWER(match_value) COLLATE Latin1_General_BIN2)
    ),
    -- the same scope/listing rule as EXPENSE, plus "not decided yet"
    CONSTRAINT CK_MERCHANT_RULE_scope CHECK (
           (scope IS NULL        AND listing_id IS NULL)
        OR (scope = 'listing'    AND listing_id IS NOT NULL)
        OR (scope = 'property'   AND listing_id IS NULL)
    )
);

-- One row per fetch of a listing's Airbnb calendar feed.
-- The .ics file in blob storage is the record; this table indexes it.
-- fetched_at is pipeline time (UTC) and matches the
-- timestamp in the blob name exactly.
CREATE TABLE ICAL_SNAPSHOT (
    snapshot_id   INT IDENTITY(1,1) NOT NULL,
    listing_id    INT               NOT NULL,
    fetched_at    DATETIME2(0)      NOT NULL,
    blob_url      NVARCHAR(500)     NOT NULL,
    content_hash  CHAR(64)          NOT NULL,   -- SHA-256 of the file
    parsed_at     DATETIME2(0)      NULL,       -- when its events were parsed; NULL = not yet

    CONSTRAINT PK_ICAL_SNAPSHOT PRIMARY KEY (snapshot_id),
    CONSTRAINT FK_ICAL_SNAPSHOT_LISTING
        FOREIGN KEY (listing_id) REFERENCES LISTING (listing_id),
    CONSTRAINT UQ_ICAL_SNAPSHOT UNIQUE (listing_id, fetched_at)
);

-- One row per event in a snapshot. Dates as in the feed: end_date is the
-- check-out day (exclusive), matching BOOKING.check_out. Only what later
-- steps need is kept; the full .ics (with guest phone digits) stays in
-- blob storage.
CREATE TABLE ICAL_EVENT (
    snapshot_id        INT            NOT NULL,
    uid                NVARCHAR(255)  NOT NULL,
    event_kind         NVARCHAR(10)   NOT NULL,   -- 'reserved' / 'blocked'
    start_date         DATE           NOT NULL,
    end_date           DATE           NOT NULL,
    summary            NVARCHAR(200)  NULL,
    confirmation_code  NVARCHAR(20)   NULL,       -- from the reservation link

    CONSTRAINT PK_ICAL_EVENT PRIMARY KEY (snapshot_id, uid),
    CONSTRAINT FK_ICAL_EVENT_SNAPSHOT
        FOREIGN KEY (snapshot_id) REFERENCES ICAL_SNAPSHOT (snapshot_id),
    CONSTRAINT CK_ICAL_EVENT_kind CHECK (event_kind IN ('reserved', 'blocked')),
    CONSTRAINT CK_ICAL_EVENT_dates CHECK (end_date > start_date),
    CONSTRAINT CK_ICAL_EVENT_code CHECK (event_kind = 'reserved' OR confirmation_code IS NULL)
);

CREATE INDEX IX_ICAL_EVENT_code ON ICAL_EVENT (confirmation_code)
    WHERE confirmation_code IS NOT NULL;

-- Corrections to booking dates the exports get wrong, applied by
-- 54_sync_bookings.sql on every load. Keyed by confirmation code, not
-- booking_id, so corrections survive a rebuild that renumbers BOOKING.
-- For each booking and field, the latest correction wins.
CREATE TABLE BOOKING_CORRECTION (
    correction_id       INT IDENTITY(1,1) NOT NULL,
    confirmation_code   NVARCHAR(20)   NOT NULL,
    field               NVARCHAR(20)   NOT NULL,     -- 'check_in' / 'check_out'
    corrected_value     DATE           NOT NULL,
    reason              NVARCHAR(300)  NOT NULL,
    source              NVARCHAR(10)   NOT NULL,     -- 'manual' / 'ical'
    source_snapshot_id  INT            NULL,         -- the snapshot that showed it
    created_at          DATETIME2(0)   NOT NULL
        CONSTRAINT DF_BOOKING_CORRECTION_created_at DEFAULT SYSDATETIME(),

    CONSTRAINT PK_BOOKING_CORRECTION PRIMARY KEY (correction_id),
    CONSTRAINT FK_BOOKING_CORRECTION_SNAPSHOT
        FOREIGN KEY (source_snapshot_id) REFERENCES ICAL_SNAPSHOT (snapshot_id),
    CONSTRAINT CK_BOOKING_CORRECTION_field  CHECK (field  IN ('check_in', 'check_out')),
    CONSTRAINT CK_BOOKING_CORRECTION_source CHECK (source IN ('manual', 'ical')),
    CONSTRAINT CK_BOOKING_CORRECTION_snapshot CHECK (
           (source = 'ical'   AND source_snapshot_id IS NOT NULL)
        OR (source = 'manual' AND source_snapshot_id IS NULL))
);

CREATE INDEX IX_BOOKING_CORRECTION_code ON BOOKING_CORRECTION (confirmation_code, field);

CREATE TABLE GUEST (
    guest_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_guest PRIMARY KEY,
    guest_email NVARCHAR(250),
    display_name NVARCHAR(200) NOT NULL,
    given_name NVARCHAR(100),
    family_name NVARCHAR(100)
);
CREATE UNIQUE INDEX ux_guest_email
    ON GUEST(guest_email) WHERE guest_email IS NOT NULL;

CREATE TABLE BLOCKED_PERIOD (
    blocked_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_blocked_period PRIMARY KEY,
    blocked_start_date DATE NOT NULL,
    blocked_end_date DATE NOT NULL,
    blocked_reason NVARCHAR(20) NOT NULL,
    blocked_notes NVARCHAR(300),
    listing_id INT NOT NULL,
    CONSTRAINT ck_blocked_dates CHECK (blocked_end_date >= blocked_start_date ),
    CONSTRAINT fk_blocked_listing_id FOREIGN KEY(listing_id) REFERENCES LISTING(listing_id),
    CONSTRAINT ck_blocked_reason CHECK (blocked_reason IN ('owner_use','maintenance','deep_clean','not_listed','other'))
);
CREATE TABLE BOOKING (
    booking_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_booking PRIMARY KEY,
    confirmation_code NVARCHAR(30) NOT NULL UNIQUE,
    guest_id INT NOT NULL,
    listing_id INT NOT NULL,
    booked_on DATETIME NOT NULL,
    check_in DATE NOT NULL,
    check_out DATE NOT NULL,
    number_of_guests INT NULL,              -- not in the Airbnb exports
    nightly_rate DECIMAL(8,2) NOT NULL,
    cleaning_fee DECIMAL(8,2) NOT NULL,
    source NVARCHAR(20) NOT NULL,
    booking_data_source NVARCHAR(20) NOT NULL,
    CONSTRAINT fk_booking_guest_id FOREIGN KEY(guest_id) REFERENCES GUEST(guest_id),
    CONSTRAINT fk_booking_listing_id FOREIGN KEY(listing_id) REFERENCES LISTING(listing_id),
    CONSTRAINT ck_booking_dates CHECK(check_out > check_in AND check_in >= CAST(booked_on AS DATE)),
    CONSTRAINT ck_number_of_guests CHECK(number_of_guests > 0),
    CONSTRAINT ck_booking_source CHECK (source IN ('airbnb','direct','stayz','booking_com','other')),
    CONSTRAINT ck_booking_data_source CHECK (booking_data_source IN ('real','generated'))
);
CREATE TABLE BOOKING_HISTORY (
    history_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_history PRIMARY KEY,
    booking_id INT NOT NULL,
    action_type NVARCHAR(20) NOT NULL,
    action_date DATETIME NOT NULL,
    reason NVARCHAR(300),
    CONSTRAINT fk_history_booking_id FOREIGN KEY(booking_id) REFERENCES BOOKING(booking_id),
    CONSTRAINT ck_history_action CHECK (action_type IN ('enquiry','confirmed','dates_changed','cancelled','completed'))
);
CREATE TABLE CLEANING (
    cleaning_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_cleaning PRIMARY KEY,
    booking_id INT NOT NULL,
    cleaning_date DATE NOT NULL,
    cleaning_type NVARCHAR(20) NOT NULL,
    cleaner_name NVARCHAR(250),
    cleaning_cost DECIMAL(8,2) NOT NULL,
    CONSTRAINT fk_cleaning_booking_id FOREIGN KEY(booking_id) REFERENCES BOOKING(booking_id),
    CONSTRAINT ck_cleaning_type CHECK (cleaning_type IN ('insourced','outsourced'))
);
CREATE TABLE PAYOUT (
    payout_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_payout PRIMARY KEY,
    booking_id INT NOT NULL,
    payout_date DATE NOT NULL,
    gross_amount DECIMAL(10,2) NOT NULL,
    platform_fee DECIMAL(10,2) NOT NULL,
    net_amount AS (gross_amount - platform_fee) PERSISTED,
    currency NVARCHAR(50) NOT NULL DEFAULT('AUD'),
    CONSTRAINT fk_payout_booking_id FOREIGN KEY(booking_id) REFERENCES BOOKING(booking_id)
);
CREATE TABLE STAY_NIGHT (
    stay_night_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_stay_night PRIMARY KEY,
    booking_id INT NOT NULL,
    listing_id INT NOT NULL,
    night_date DATE NOT NULL,
    rate_night DECIMAL(8,2) NOT NULL,
    CONSTRAINT fk_stay_night_booking_id FOREIGN KEY(booking_id) REFERENCES BOOKING(booking_id),
    CONSTRAINT fk_stay_night_listing_id FOREIGN KEY(listing_id) REFERENCES LISTING(listing_id),
    CONSTRAINT uq_stay_night_listing UNIQUE (night_date, listing_id),
    CONSTRAINT uq_stay_night_booking UNIQUE (night_date, booking_id)
);

-- One row per export loaded. A file whose name is here is never loaded
-- again; a file is recorded only after its transform succeeds, so a
-- failed load is retried on the next run.
CREATE TABLE LOAD_LOG (
    file_name    NVARCHAR(200) NOT NULL,
    file_kind    NVARCHAR(10)  NOT NULL,       -- 'paid' / 'upcoming'
    export_date  DATE          NOT NULL,       -- from the file name
    row_count    INT           NOT NULL,
    loaded_at    DATETIME2(0)  NOT NULL
        CONSTRAINT DF_LOAD_LOG_loaded_at DEFAULT SYSDATETIME(),
    CONSTRAINT PK_LOAD_LOG PRIMARY KEY (file_name),
    CONSTRAINT CK_LOAD_LOG_kind CHECK (file_kind IN ('paid', 'upcoming'))
);

CREATE TABLE EMAIL_RAW (
    email_id     INT IDENTITY(1,1) NOT NULL,
    message_id   NVARCHAR(250)     NOT NULL,
    received_at  DATETIME          NOT NULL,
    sender       NVARCHAR(250)     NULL,
    subject      NVARCHAR(500)     NULL,
    body_text    NVARCHAR(MAX)     NULL,
    body_html    NVARCHAR(MAX)     NULL,
    fetched_at   DATETIME          NOT NULL
                 CONSTRAINT DF_EMAIL_RAW_fetched_at DEFAULT SYSDATETIME(),
    parsed_as    NVARCHAR(30)      NULL,
    -- The address the email was sent TO (lower case), which routes its
    -- receipts to a listing. NULL for emails fetched before this existed.
    recipient    NVARCHAR(250)     NULL,

    CONSTRAINT PK_EMAIL_RAW PRIMARY KEY (email_id),
    CONSTRAINT UQ_EMAIL_RAW_message_id UNIQUE (message_id),
    -- underscore is a LIKE wildcard, hence [_]
    CONSTRAINT CK_EMAIL_RAW_parsed_as CHECK (
        parsed_as IN ('receipt', 'ignored')
        OR parsed_as LIKE 'airbnb[_]%'
    )
);

CREATE TABLE RECEIPT (
    receipt_id         INT IDENTITY(1,1) NOT NULL,
    email_id           INT               NOT NULL,
    attachment_index   INT               NOT NULL,
    blob_url           NVARCHAR(500)     NULL,
    original_filename  NVARCHAR(260)     NULL,
    content_type       NVARCHAR(100)     NULL,
    file_size_bytes    INT               NULL,
    extraction_json    NVARCHAR(MAX)     NULL,
    extracted_at       DATETIME          NULL,
    extraction_model   NVARCHAR(50)      NULL,
    error_message      NVARCHAR(500)     NULL,
    attempt_count      INT               NOT NULL CONSTRAINT DF_RECEIPT_attempt_count DEFAULT 0,
    status  NVARCHAR(20)  NOT NULL CONSTRAINT DF_RECEIPT_status DEFAULT 'pending',
    file_sha256        CHAR(64)          NOT NULL,
    CONSTRAINT PK_RECEIPT PRIMARY KEY (receipt_id),
    CONSTRAINT FK_RECEIPT_EMAIL_RAW FOREIGN KEY (email_id) REFERENCES EMAIL_RAW (email_id),
    CONSTRAINT UQ_RECEIPT_email_attachment UNIQUE (email_id, attachment_index),
    CONSTRAINT CK_RECEIPT_status CHECK (
        status IN ('pending', 'extracted', 'validated', 'categorised',
                   'needs_review', 'done', 'not_expense', 'failed'))
);

CREATE INDEX IX_RECEIPT_file_sha256 ON RECEIPT (file_sha256);

CREATE TABLE RECEIPT_ISSUE (
    issue_id     INT IDENTITY(1,1) NOT NULL,
    receipt_id   INT               NOT NULL,
    check_name   NVARCHAR(30)      NOT NULL,
    detail       NVARCHAR(500)     NULL,       -- the numbers behind the issue
    found_at     DATETIME2(0)      NOT NULL
        CONSTRAINT DF_RECEIPT_ISSUE_found_at DEFAULT SYSDATETIME(),
    resolved_at  DATETIME2(0)      NULL,       -- set by review
    resolution   NVARCHAR(200)     NULL,       -- review note, e.g. 'GST on receipt is wrong'

    CONSTRAINT PK_RECEIPT_ISSUE PRIMARY KEY (issue_id),
    CONSTRAINT FK_RECEIPT_ISSUE_RECEIPT
        FOREIGN KEY (receipt_id) REFERENCES RECEIPT (receipt_id),
    -- each check raises at most one issue per receipt, so reruns
    -- can't pile up copies
    CONSTRAINT UQ_RECEIPT_ISSUE UNIQUE (receipt_id, check_name),
    CONSTRAINT CK_RECEIPT_ISSUE_check_name CHECK (
        check_name IN (
            'duplicate_file', 'possible_duplicate',
            'missing_total', 'missing_date', 'missing_merchant',
            'low_confidence', 'amount_mismatch', 'gst_too_high',
            'date_after_email', 'date_too_early',
            'second_document', 'invalid_abn',
            'model_disagreement', 'review_suggested')),
    CONSTRAINT CK_RECEIPT_ISSUE_resolution
        CHECK (resolved_at IS NOT NULL OR resolution IS NULL)
);

CREATE TABLE RECEIPT_CATEGORISATION (
    categorisation_id  INT IDENTITY(1,1) NOT NULL,
    receipt_id         INT            NOT NULL,
    model              NVARCHAR(50)   NOT NULL,   -- e.g. 'claude-haiku-5-5'
    prompt_version     NVARCHAR(20)   NOT NULL,   -- e.g. 'v1'
    document_type      NVARCHAR(30)   NULL,
    category           NVARCHAR(30)   NULL,       -- NULL if the answer was unusable
    scope              NVARCHAR(10)   NULL,
    reason             NVARCHAR(500)  NULL,
    action_reason      NVARCHAR(1000) NULL,
    action             NVARCHAR(20)   NULL,
    action_field       NVARCHAR(30)   NULL,
    action_value       NVARCHAR(100)  NULL,
    raw_response       NVARCHAR(MAX)  NOT NULL,   -- always kept, even if unusable
    created_at         DATETIME2(0)   NOT NULL
        CONSTRAINT DF_RECEIPT_CATEGORISATION_created_at DEFAULT SYSDATETIME(),

    CONSTRAINT PK_RECEIPT_CATEGORISATION PRIMARY KEY (categorisation_id),
    CONSTRAINT FK_RECEIPT_CATEGORISATION_RECEIPT
        FOREIGN KEY (receipt_id) REFERENCES RECEIPT (receipt_id),
    CONSTRAINT FK_RECEIPT_CATEGORISATION_CATEGORY
        FOREIGN KEY (category) REFERENCES CATEGORY (category_code),
    CONSTRAINT CK_RECEIPT_CATEGORISATION_document_type CHECK (document_type IN
        ('receipt', 'invoice', 'payment_confirmation', 'report', 'other')),
    CONSTRAINT CK_RECEIPT_CATEGORISATION_scope
        CHECK (scope IN ('listing', 'property')),
    CONSTRAINT CK_RECEIPT_CATEGORISATION_action
        CHECK (action IN ('not_expense', 'set_field', 'accept', 'needs_human')),
    CONSTRAINT CK_RECEIPT_CATEGORISATION_action_field
        CHECK (action_field IN ('transaction_date', 'total', 'merchant_name')),
    -- a field and value only make sense with set_field
    CONSTRAINT CK_RECEIPT_CATEGORISATION_set_field CHECK (
           (action = 'set_field' AND action_field IS NOT NULL AND action_value IS NOT NULL)
        OR (ISNULL(action, '') <> 'set_field' AND action_field IS NULL AND action_value IS NULL)
    ),
    -- one answer per receipt, model and prompt version; a new prompt
    -- version can be compared against the old one
    CONSTRAINT UQ_RECEIPT_CATEGORISATION UNIQUE (receipt_id, model, prompt_version)
);

CREATE TABLE RECEIPT_REVIEW (
    review_id                INT IDENTITY(1,1) NOT NULL,
    receipt_id               INT            NOT NULL,
    decision                 NVARCHAR(12)   NOT NULL,
    expense_date             DATE           NULL,
    expense_amount           DECIMAL(10,2)  NULL,
    supplier                 NVARCHAR(250)  NULL,
    category                 NVARCHAR(30)   NULL,
    scope                    NVARCHAR(10)   NULL,
    listing_id               INT            NULL,
    booking_id               INT            NULL,
    expense_type             NVARCHAR(20)   NULL,
    not_expense_reason       NVARCHAR(30)   NULL,
    duplicate_of_receipt_id  INT            NULL,
    note                     NVARCHAR(500)  NULL,
    reviewed_at              DATETIME2(0)   NOT NULL
        CONSTRAINT DF_RECEIPT_REVIEW_reviewed_at DEFAULT SYSDATETIME(),

    CONSTRAINT PK_RECEIPT_REVIEW PRIMARY KEY (review_id),
    CONSTRAINT FK_RECEIPT_REVIEW_RECEIPT
        FOREIGN KEY (receipt_id) REFERENCES RECEIPT (receipt_id),
    CONSTRAINT FK_RECEIPT_REVIEW_DUPLICATE
        FOREIGN KEY (duplicate_of_receipt_id) REFERENCES RECEIPT (receipt_id),
    CONSTRAINT FK_RECEIPT_REVIEW_CATEGORY
        FOREIGN KEY (category) REFERENCES CATEGORY (category_code),
    CONSTRAINT FK_RECEIPT_REVIEW_LISTING
        FOREIGN KEY (listing_id) REFERENCES LISTING (listing_id),
    CONSTRAINT FK_RECEIPT_REVIEW_BOOKING
        FOREIGN KEY (booking_id) REFERENCES BOOKING (booking_id),

    CONSTRAINT CK_RECEIPT_REVIEW_decision
        CHECK (decision IN ('expense', 'not_expense')),
    CONSTRAINT CK_RECEIPT_REVIEW_scope
        CHECK (scope IN ('listing', 'property')),
    CONSTRAINT CK_RECEIPT_REVIEW_expense_type
        CHECK (expense_type IN ('direct', 'periodic', 'capital', 'consumable')),
    CONSTRAINT CK_RECEIPT_REVIEW_reason
        CHECK (not_expense_reason IN
               ('report', 'payment_confirmation', 'duplicate', 'personal', 'other')),
    -- a not-expense decision needs a reason; an expense decision has none
    CONSTRAINT CK_RECEIPT_REVIEW_reason_needed CHECK (
           (decision = 'expense'     AND not_expense_reason IS NULL)
        OR (decision = 'not_expense' AND not_expense_reason IS NOT NULL)),
    -- a duplicate names the receipt it duplicates, and nothing else does
    CONSTRAINT CK_RECEIPT_REVIEW_duplicate CHECK (
           (not_expense_reason = 'duplicate' AND duplicate_of_receipt_id IS NOT NULL)
        OR (ISNULL(not_expense_reason, '') <> 'duplicate' AND duplicate_of_receipt_id IS NULL)),
    -- a not-expense decision carries no expense values
    CONSTRAINT CK_RECEIPT_REVIEW_no_values CHECK (
        decision = 'expense'
        OR (expense_date IS NULL AND expense_amount IS NULL AND supplier IS NULL
            AND category IS NULL AND scope IS NULL AND listing_id IS NULL
            AND booking_id IS NULL AND expense_type IS NULL))
);

CREATE INDEX IX_RECEIPT_REVIEW_receipt ON RECEIPT_REVIEW (receipt_id, reviewed_at);

CREATE TABLE EXPENSE (
    expense_id         INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_expense PRIMARY KEY,
    listing_id         INT            NULL,
    property_id        INT            NOT NULL,
    booking_id         INT            NULL,   -- set only when known (review)
    expense_date       DATE           NULL,
    expense_amount     DECIMAL(10,2)  NULL,   -- full bill; shares via APPORTIONMENT_RULE
    category           NVARCHAR(30)   NOT NULL,
    expense_type       NVARCHAR(20)   NOT NULL,
    supplier           NVARCHAR(250)  NULL,
    receipt_id         INT            NULL,
    extracted_by       NVARCHAR(10)   NULL,   -- 'pipeline' / 'manual'
    verified_by_user   BIT            NOT NULL CONSTRAINT df_expense_verified DEFAULT 0,
    scope              NVARCHAR(10)   NOT NULL CONSTRAINT DF_EXPENSE_scope DEFAULT 'listing',
    supplier_abn       CHAR(11)       NULL,   -- digits only, no spaces
    invoice_number     NVARCHAR(50)   NULL,

    CONSTRAINT fk_expense_booking_id FOREIGN KEY (booking_id) REFERENCES BOOKING (booking_id),
    CONSTRAINT fk_expense_listing_id FOREIGN KEY (listing_id) REFERENCES LISTING (listing_id),
    CONSTRAINT fk_expense_receipt    FOREIGN KEY (receipt_id) REFERENCES RECEIPT (receipt_id),
    CONSTRAINT FK_EXPENSE_CATEGORY   FOREIGN KEY (category)   REFERENCES CATEGORY (category_code),

    CONSTRAINT ck_expense_type
        CHECK (expense_type IN ('direct', 'periodic', 'capital', 'consumable')),
    CONSTRAINT CK_EXPENSE_extracted_by
        CHECK (extracted_by IN ('pipeline', 'manual')),
    CONSTRAINT CK_EXPENSE_scope
        CHECK (scope IN ('listing', 'property')),

    -- 'listing'  -> belongs to one listing
    -- 'property' -> whole property, split by APPORTIONMENT_RULE
    CONSTRAINT CK_EXPENSE_scope_listing
        CHECK (   (scope = 'listing'  AND listing_id IS NOT NULL)
               OR (scope = 'property' AND listing_id IS NULL)),

    CONSTRAINT FK_EXPENSE_PROPERTY
        FOREIGN KEY (property_id) REFERENCES PROPERTY (property_id),
    CONSTRAINT FK_EXPENSE_LISTING_PROPERTY
        FOREIGN KEY (listing_id, property_id) REFERENCES LISTING (listing_id, property_id),

    -- ABN: 11 digits; (first digit - 1), weights 10,1,3,5,7,9,11,13,15,17,19,
    -- sum divisible by 89. TRY_CAST gives NULL on a non-digit instead of an
    -- error; the NOT LIKE test then makes the whole check FALSE.
    CONSTRAINT CK_EXPENSE_supplier_abn CHECK (
        supplier_abn IS NULL OR (
            LEN(supplier_abn) = 11
            AND supplier_abn NOT LIKE '%[^0-9]%'
            AND ( (TRY_CAST(SUBSTRING(supplier_abn,  1, 1) AS INT) - 1) * 10
                +  TRY_CAST(SUBSTRING(supplier_abn,  2, 1) AS INT) *  1
                +  TRY_CAST(SUBSTRING(supplier_abn,  3, 1) AS INT) *  3
                +  TRY_CAST(SUBSTRING(supplier_abn,  4, 1) AS INT) *  5
                +  TRY_CAST(SUBSTRING(supplier_abn,  5, 1) AS INT) *  7
                +  TRY_CAST(SUBSTRING(supplier_abn,  6, 1) AS INT) *  9
                +  TRY_CAST(SUBSTRING(supplier_abn,  7, 1) AS INT) * 11
                +  TRY_CAST(SUBSTRING(supplier_abn,  8, 1) AS INT) * 13
                +  TRY_CAST(SUBSTRING(supplier_abn,  9, 1) AS INT) * 15
                +  TRY_CAST(SUBSTRING(supplier_abn, 10, 1) AS INT) * 17
                +  TRY_CAST(SUBSTRING(supplier_abn, 11, 1) AS INT) * 19
                ) % 89 = 0
        )
    )
);

-- One receipt = one expense.
CREATE UNIQUE INDEX ux_expense_receipt ON EXPENSE (receipt_id)
    WHERE receipt_id IS NOT NULL;

-- One bill = one expense. Till receipts have no invoice number,
-- so they aren't covered by this index.
CREATE UNIQUE INDEX UX_EXPENSE_supplier_invoice
    ON EXPENSE (supplier_abn, invoice_number)
    WHERE supplier_abn IS NOT NULL AND invoice_number IS NOT NULL;

CREATE TABLE ITEM (
    item_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_item PRIMARY KEY,
    item_name NVARCHAR(250) NOT NULL,
    item_unit NVARCHAR(100),
    reorder_level INT
);
CREATE TABLE STOCK_MOVEMENT(
    movement_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_movment PRIMARY KEY,
    item_id INT NOT NULL,
    booking_id INT,
    expense_id INT,
    movement_date DATE NOT NULL,
    quantity_change INT NOT NULL,
    movement_type NVARCHAR(20),
    CONSTRAINT fk_movement_item_id FOREIGN KEY(item_id) REFERENCES ITEM(item_id),
    CONSTRAINT fk_movement_booking_id FOREIGN KEY(booking_id) REFERENCES BOOKING(booking_id),
    CONSTRAINT fk_movement_expense_id FOREIGN KEY(expense_id) REFERENCES EXPENSE(expense_id),
    CONSTRAINT ck_quantity_change CHECK (quantity_change != 0),
    CONSTRAINT ck_movement_type CHECK (movement_type IN ('purchase','consumption','stocktake','wastage','personal_use'))
);
CREATE TABLE MAINTENANCE (
    maintenance_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_maintenance PRIMARY KEY,
    listing_id INT NOT NULL,
    booking_id INT,
    expense_id INT,
    reported_on DATE NOT NULL,
    resolved_on DATE,
    category NVARCHAR(30) NOT NULL,
    description NVARCHAR(400) NOT NULL,
    severity NVARCHAR(20) NOT NULL,
    CONSTRAINT fk_maintenance_listing_id FOREIGN KEY(listing_id) REFERENCES LISTING(listing_id),
    CONSTRAINT fk_maintenance_booking_id FOREIGN KEY(booking_id) REFERENCES BOOKING(booking_id),
    CONSTRAINT fk_maintenance_expense_id FOREIGN KEY(expense_id) REFERENCES EXPENSE(expense_id),
    CONSTRAINT ck_maintenance_category CHECK (category IN
    ('plumbing','electrical','appliance','heating_cooling',
     'furniture','fittings','structural','garden','other')),
    CONSTRAINT ck_maintenance_severity CHECK (severity IN ('low','medium','high','urgent'))
);
CREATE TABLE REVIEW (
    review_id INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_review PRIMARY KEY,
    booking_id INT NOT NULL,
    review_date DATE NOT NULL,
    overall_rating INT NOT NULL,
    cleanliness_rating INT,
    accuracy_rating INT,
    communication_rating INT,
    location_rating INT,
    value_rating INT,
    comment NVARCHAR(800),
    direction NVARCHAR(20) NOT NULL,
    CONSTRAINT fk_review_booking_id FOREIGN KEY(booking_id) REFERENCES BOOKING(booking_id),
    CONSTRAINT ck_review_direction CHECK (direction IN ('guest_to_host','host_to_guest')),
    CONSTRAINT ck_review_overall CHECK (overall_rating BETWEEN 1 AND 5),
    CONSTRAINT ck_review_cleanliness CHECK (cleanliness_rating BETWEEN 1 AND 5),
    CONSTRAINT ck_review_accuracy CHECK (accuracy_rating BETWEEN 1 AND 5),
    CONSTRAINT ck_review_communication CHECK (communication_rating BETWEEN 1 AND 5),
    CONSTRAINT ck_review_location CHECK (location_rating BETWEEN 1 AND 5),
    CONSTRAINT ck_review_value CHECK (value_rating BETWEEN 1 AND 5)
);
CREATE TABLE CALENDAR (
    date_key DATE NOT NULL CONSTRAINT pk_calendar PRIMARY KEY,
    year_number INT NOT NULL,
    quarter_number INT NOT NULL,
    month_number INT NOT NULL,
    month_name NVARCHAR(20) NOT NULL,
    day_of_month INT NOT NULL,
    day_of_week INT NOT NULL,
    day_name NVARCHAR(20) NOT NULL,
    week_of_year INT NOT NULL,
    is_weekend BIT NOT NULL,
    is_public_holiday BIT NOT NULL CONSTRAINT df_calendar_holiday DEFAULT(0),
    holiday_name NVARCHAR(100),
    is_school_holiday BIT NOT NULL CONSTRAINT df_calendar_school DEFAULT(0),
    season NVARCHAR(10) NOT NULL,
    CONSTRAINT ck_calendar_season CHECK (season IN ('summer','autumn','winter','spring'))
);
