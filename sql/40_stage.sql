DROP TABLE IF EXISTS STG_AIRBNB_EARNINGS;

CREATE TABLE STG_AIRBNB_EARNINGS (
    stg_id INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_stage PRIMARY KEY,
    stg_loaded_at DATETIME NOT NULL DEFAULT(SYSDATETIME()),
    stg_source_file NVARCHAR(200) NOT NULL,
    stg_file_kind NVARCHAR(10)
        CONSTRAINT CK_STG_AIRBNB_EARNINGS_file_kind CHECK (stg_file_kind IN ('paid', 'upcoming')),
    stg_date NVARCHAR(50),
    stg_arrived_by_date NVARCHAR(50),
    stg_type NVARCHAR(50),
    stg_confirmation_code NVARCHAR(50),
    stg_booking_date NVARCHAR(50),
    stg_start_date NVARCHAR(50),
    stg_end_date NVARCHAR(50),
    stg_nights NVARCHAR(50),
    stg_guest_name NVARCHAR(50),
    stg_listing NVARCHAR(50),
    stg_details NVARCHAR(500),
    stg_reference_code NVARCHAR(50),
    stg_currency NVARCHAR(50),
    stg_amount NVARCHAR(50),
    stg_payout NVARCHAR(50),
    stg_service_fee NVARCHAR(50),
    stg_fast_pay_fee NVARCHAR(50),
    stg_cleaning_fee NVARCHAR(50),
    stg_gross_earning NVARCHAR(50),
    stg_airbnb_remitted_tax NVARCHAR(50),
    stg_earning_year NVARCHAR(50) 
)