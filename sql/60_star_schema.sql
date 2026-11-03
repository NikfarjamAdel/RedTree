
/* ============================================================
   60_star_schema.sql — dimensional model

   FACT_STAY_NIGHT — one row per night, per listing, per booking.
   FACT_BOOKING    — one row per booking.

   Regenerated from the operational tables by 61_star_load.sql.
   Never written to directly.

   Dimensions use surrogate keys (IDENTITY) rather than reusing the
   operational ids, so a Type 2 dimension can hold several rows for
   the same real-world thing. The natural key is kept alongside for
   traceability back to the source.
   ============================================================ */

-- Facts first: they reference the dimensions.
DROP TABLE IF EXISTS FACT_EXPENSE;
DROP TABLE IF EXISTS FACT_AVAILABLE_NIGHT;
DROP TABLE IF EXISTS FACT_BOOKING;
DROP TABLE IF EXISTS FACT_STAY_NIGHT;
DROP TABLE IF EXISTS DIM_CATEGORY;
DROP TABLE IF EXISTS DIM_BOOKING;
DROP TABLE IF EXISTS DIM_GUEST;
DROP TABLE IF EXISTS DIM_LISTING;
DROP TABLE IF EXISTS DIM_DATE;
GO


/* ---------- DIM_DATE ----------------------------------------
   Natural key, not a surrogate: dates never change, so there is
   nothing for a surrogate to protect against. Copied wholesale
   from CALENDAR.
   ------------------------------------------------------------ */
CREATE TABLE DIM_DATE (
    date_key          DATE         NOT NULL CONSTRAINT pk_dim_date PRIMARY KEY,
    year_number       INT          NOT NULL,
    quarter_number    INT          NOT NULL,
    month_number      INT          NOT NULL,
    month_name        NVARCHAR(20) NOT NULL,
    day_of_month      INT          NOT NULL,
    day_of_week       INT          NOT NULL,
    day_name          NVARCHAR(20) NOT NULL,
    week_of_year      INT          NOT NULL,
    is_weekend        BIT          NOT NULL,
    is_public_holiday BIT          NOT NULL,
    holiday_name      NVARCHAR(100),
    is_school_holiday BIT          NOT NULL,
    season            NVARCHAR(10) NOT NULL
);


/* ---------- DIM_LISTING -------------------------------------
   Type 1 for now: attributes are overwritten on reload, so history
   reflects the listing as it is today. listing_status is the column
   that makes this wrong -- see the SCD exercise.
   ------------------------------------------------------------ */
CREATE TABLE DIM_LISTING (
    listing_key     INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_dim_listing PRIMARY KEY,
    listing_id      INT           NOT NULL,   -- natural key
    listing_title   NVARCHAR(150) NOT NULL,
    listing_address NVARCHAR(250),
    listing_status  NVARCHAR(20)  NOT NULL,
    listed_from     DATE          NOT NULL
);


/* ---------- DIM_GUEST ---------------------------------------
   Thin, deliberately. Airbnb supplies a display name only; email and
   real names exist for direct bookings. is_repeat_guest is derived at
   load time and is only meaningful where an email lets us match.
   ------------------------------------------------------------ */
CREATE TABLE DIM_GUEST (
    guest_key       INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_dim_guest PRIMARY KEY,
    guest_id        INT           NOT NULL,   -- natural key
    display_name    NVARCHAR(200) NOT NULL,
    has_email       BIT           NOT NULL,
    is_repeat_guest BIT           NOT NULL
);


/* ---------- DIM_BOOKING -------------------------------------
   Attributes that describe a booking but are not numbers.
   current_status is derived from BOOKING_HISTORY at load time: the
   operational model stores status as events, not as a column.
   ------------------------------------------------------------ */
CREATE TABLE DIM_BOOKING (
    booking_key         INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_dim_booking PRIMARY KEY,
    booking_id          INT          NOT NULL,   -- natural key
    confirmation_code   NVARCHAR(30) NOT NULL,
    source              NVARCHAR(20) NOT NULL,
    booking_data_source NVARCHAR(20) NOT NULL,
    current_status      NVARCHAR(20) NOT NULL,
    is_cancelled        BIT          NOT NULL,
    CONSTRAINT ck_dim_booking_status
        CHECK (current_status IN ('confirmed','dates_changed','cancelled','completed'))
);
GO


/* ---------- FACT_STAY_NIGHT ----------------------------------
   Grain: one row per night actually stayed, per listing. Mirrors
   STAY_NIGHT. guest_key is denormalized onto the fact so guest-level
   analysis doesn't need a join through DIM_BOOKING.
   ------------------------------------------------------------ */
CREATE TABLE FACT_STAY_NIGHT (
    date_key    DATE         NOT NULL,
    listing_key INT          NOT NULL,
    guest_key   INT          NOT NULL,
    booking_key INT          NOT NULL,
    rate_night  DECIMAL(8,2) NOT NULL,
    CONSTRAINT pk_fact_stay_night PRIMARY KEY (date_key, listing_key),
    CONSTRAINT fk_fsn_date    FOREIGN KEY (date_key)    REFERENCES DIM_DATE(date_key),
    CONSTRAINT fk_fsn_listing FOREIGN KEY (listing_key) REFERENCES DIM_LISTING(listing_key),
    CONSTRAINT fk_fsn_guest   FOREIGN KEY (guest_key)   REFERENCES DIM_GUEST(guest_key),
    CONSTRAINT fk_fsn_booking FOREIGN KEY (booking_key) REFERENCES DIM_BOOKING(booking_key)
);
GO


/* ---------- FACT_BOOKING ---------------------------------------
   Grain: one row per booking. Three separate date roles (booked,
   checked-in, checked-out) each get their own FK to DIM_DATE, so
   the same dimension can be filtered/joined by any of the three.
   nights, leadtime_days and gross_amount are derived rather than
   loaded, so they can never drift from their source columns.
   ------------------------------------------------------------ */
CREATE TABLE FACT_BOOKING (
    booked_date_key      DATE          NOT NULL,
    checked_in_date_key  DATE          NOT NULL,
    checked_out_date_key DATE          NOT NULL,
    listing_key          INT           NOT NULL,
    guest_key            INT           NOT NULL,
    booking_key          INT           NOT NULL CONSTRAINT pk_fact_booking PRIMARY KEY,
    nights               AS (DATEDIFF(DAY, checked_in_date_key, checked_out_date_key)) PERSISTED,
    platform_fee         DECIMAL(10,2),
    net_amount           DECIMAL(10,2),
    gross_amount         AS (platform_fee + net_amount) PERSISTED,
    cleaning_fee         DECIMAL(10,2),
    cleaning_cost        DECIMAL(10,2),
    leadtime_days        AS (DATEDIFF(DAY, booked_date_key, checked_in_date_key)) PERSISTED,
    number_of_guests     INT,
    overall_rating       INT,
    CONSTRAINT fk_fb_booked      FOREIGN KEY (booked_date_key)      REFERENCES DIM_DATE(date_key),
    CONSTRAINT fk_fb_checked_in  FOREIGN KEY (checked_in_date_key)  REFERENCES DIM_DATE(date_key),
    CONSTRAINT fk_fb_checked_out FOREIGN KEY (checked_out_date_key) REFERENCES DIM_DATE(date_key),
    CONSTRAINT fk_fb_listing     FOREIGN KEY (listing_key)          REFERENCES DIM_LISTING(listing_key),
    CONSTRAINT fk_fb_guest       FOREIGN KEY (guest_key)            REFERENCES DIM_GUEST(guest_key),
    CONSTRAINT fk_fb_booking     FOREIGN KEY (booking_key)          REFERENCES DIM_BOOKING(booking_key)
);
GO

CREATE TABLE FACT_AVAILABLE_NIGHT (
    date_key    DATE NOT NULL,
    listing_key INT  NOT NULL,
    CONSTRAINT pk_fact_available_night PRIMARY KEY (date_key, listing_key),
    CONSTRAINT fk_fan_date    FOREIGN KEY (date_key)    REFERENCES DIM_DATE(date_key),
    CONSTRAINT fk_fan_listing FOREIGN KEY (listing_key) REFERENCES DIM_LISTING(listing_key)
);

GO

CREATE TABLE DIM_CATEGORY (
    category_key          INT IDENTITY(1,1) NOT NULL,
    category_code         NVARCHAR(30)   NOT NULL,     -- natural key
    description           NVARCHAR(200)  NOT NULL,
    default_expense_type  NVARCHAR(20)   NOT NULL,
    CONSTRAINT PK_DIM_CATEGORY PRIMARY KEY (category_key),
    CONSTRAINT UQ_DIM_CATEGORY_code UNIQUE (category_code)
);

-- Grain: one row per expense, per listing it is attributed to, per month it
-- is spread over (capital over its life, period bills over their period).
-- Loaded from vw_expense_monthly, so shares and spreading are defined once.
CREATE TABLE FACT_EXPENSE (
    month_key      DATE           NOT NULL,     -- first day of the month
    listing_key    INT            NOT NULL,
    category_key   INT            NOT NULL,
    expense_id     INT            NOT NULL,     -- degenerate dimension
    expense_type   NVARCHAR(20)   NOT NULL,
    is_verified    BIT            NOT NULL,
    amount         DECIMAL(10,2)  NULL,
    CONSTRAINT PK_FACT_EXPENSE PRIMARY KEY (expense_id, listing_key, month_key),
    CONSTRAINT FK_FACT_EXPENSE_DATE     FOREIGN KEY (month_key)    REFERENCES DIM_DATE (date_key),
    CONSTRAINT FK_FACT_EXPENSE_LISTING  FOREIGN KEY (listing_key)  REFERENCES DIM_LISTING (listing_key),
    CONSTRAINT FK_FACT_EXPENSE_CATEGORY FOREIGN KEY (category_key) REFERENCES DIM_CATEGORY (category_key)
);

GO