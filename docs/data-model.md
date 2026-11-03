# Data model

About thirty tables in four areas, shown as four diagrams so each stays
readable. Key columns only; the full definitions are in `sql/01_schema.sql`
and `sql/60_star_schema.sql`.

## 1. Bookings and revenue

```mermaid
erDiagram
    PROPERTY ||--|{ LISTING : "has"
    LISTING  ||--o{ BOOKING : "is booked as"
    GUEST    ||--o{ BOOKING : "makes"
    BOOKING  ||--o{ STAY_NIGHT : "is exploded into"
    LISTING  ||--o{ STAY_NIGHT : "is occupied on"
    BOOKING  ||--o{ BOOKING_HISTORY : "changes state via"
    BOOKING  ||--o{ PAYOUT : "earns"
    BOOKING  ||--o{ CLEANING : "triggers"
    BOOKING  ||--o| REVIEW : "receives"
    LISTING  ||--o{ BLOCKED_PERIOD : "is unavailable during"
    BOOKING  }o..o{ BOOKING_CORRECTION : "matched by confirmation code"

    PROPERTY {
        int property_id PK
        nvarchar property_key UK "from the config"
        nvarchar property_name
    }
    LISTING {
        int listing_id PK
        int property_id FK
        nvarchar listing_key UK "from the config"
        nvarchar listing_title
        nvarchar airbnb_name "as in the export"
        nvarchar receipt_address UK "routes receipts"
        bit default_for_receipts "at most one overall"
        date listed_from
        decimal cleaning_cost_per_turnover "imputed insourced clean"
        decimal loan_interest_share
    }
    GUEST {
        int guest_id PK
        nvarchar display_name "as supplied by the platform"
    }
    BOOKING {
        int booking_id PK
        nvarchar confirmation_code UK "natural key"
        int listing_id FK
        int guest_id FK
        datetime booked_on
        date check_in
        date check_out
        decimal nightly_rate "imputed from totals"
        decimal cleaning_fee "charged to the guest"
    }
    STAY_NIGHT {
        int booking_id FK
        int listing_id FK "unique with night_date: no double-booking"
        date night_date
        decimal rate_night
    }
    BOOKING_HISTORY {
        int booking_id FK
        nvarchar action_type "confirmed, cancelled, completed"
        datetime action_date
    }
    PAYOUT {
        int booking_id FK
        date payout_date
        decimal gross_amount
        decimal platform_fee
        decimal net_amount "computed: revenue"
    }
    CLEANING {
        int booking_id FK
        date cleaning_date
        nvarchar cleaning_type "insourced, outsourced"
        decimal cleaning_cost "cost, not the guest fee"
    }
    REVIEW {
        int booking_id FK
        int overall_rating
    }
    BLOCKED_PERIOD {
        int listing_id FK
        date blocked_start_date
        date blocked_end_date
    }
    BOOKING_CORRECTION {
        nvarchar confirmation_code
        nvarchar field "check_in, check_out"
        date corrected_value
        nvarchar source "manual, ical"
    }
```

## 2. Costs: expenses, apportionment and loans

```mermaid
erDiagram
    PROPERTY ||--o{ EXPENSE : "owns"
    LISTING  |o--o{ EXPENSE : "listing scope only"
    CATEGORY ||--o{ EXPENSE : "classifies"
    CATEGORY ||--o{ APPORTIONMENT_RULE : "is split by"
    LISTING  ||--o{ APPORTIONMENT_RULE : "takes a share via"
    PROPERTY ||--o{ LOAN : "is financed by"
    LOAN     ||--o{ LOAN_INTEREST : "charges"
    EXPENSE  ||--o{ STOCK_MOVEMENT : "pays for"
    ITEM     ||--o{ STOCK_MOVEMENT : "moves as"
    LISTING  ||--o{ MAINTENANCE : "requires"

    EXPENSE {
        int expense_id PK
        int property_id FK "every expense has one"
        int listing_id FK "listing scope; composite key with property"
        int receipt_id FK "nullable: manual expenses have none"
        date expense_date
        decimal expense_amount "the full bill"
        nvarchar category FK
        nvarchar scope "listing, property"
        nvarchar expense_type "direct, periodic, capital, consumable"
        char supplier_abn "check digit enforced"
        bit verified_by_user
    }
    CATEGORY {
        nvarchar category_code PK
        nvarchar description "also the LLM's definition"
        nvarchar default_expense_type
        int capital_life_months
        int spread_months
        nvarchar spread_anchor "start, end"
    }
    APPORTIONMENT_RULE {
        int listing_id FK
        nvarchar category FK
        decimal rate
        date valid_from "half-open periods"
        date valid_to
    }
    LOAN {
        nvarchar loan_code PK
        int property_id FK
        date active_from
        int charge_day
    }
    LOAN_INTEREST {
        nvarchar loan_code FK
        date month_start
        decimal interest_amount "split by days across months"
    }
    ITEM {
        int item_id PK
        nvarchar item_name "designed; no pipeline yet"
    }
    STOCK_MOVEMENT {
        int item_id FK
        int quantity_change "signed; levels derived"
    }
    MAINTENANCE {
        int listing_id FK "designed; no pipeline yet"
        date reported_on
        date resolved_on
    }
```

## 3. Ingestion: receipts and calendar snapshots

```mermaid
erDiagram
    EMAIL_RAW     ||--o{ RECEIPT : "carries"
    RECEIPT       ||--o{ RECEIPT_ISSUE : "is flagged by"
    RECEIPT       ||--o{ RECEIPT_CATEGORISATION : "is answered by"
    RECEIPT       ||--o{ RECEIPT_REVIEW : "is decided by"
    RECEIPT       |o--o| EXPENSE : "becomes"
    MERCHANT_RULE }o--o| LISTING : "may assign"
    LISTING       ||--o{ ICAL_SNAPSHOT : "is captured by"
    ICAL_SNAPSHOT ||--o{ ICAL_EVENT : "contains"
    ICAL_SNAPSHOT ||--o{ BOOKING_CORRECTION : "evidences"

    EMAIL_RAW {
        int email_id PK
        nvarchar message_id UK
        nvarchar recipient "routes to a listing"
        datetime received_at
    }
    RECEIPT {
        int receipt_id PK
        int email_id FK
        nvarchar blob_url "raw file in blob storage"
        char file_sha256
        nvarchar extraction_model
        nvarchar status "pending to done"
    }
    RECEIPT_ISSUE {
        int receipt_id FK
        nvarchar check_name "unique per receipt"
        datetime resolved_at
    }
    RECEIPT_CATEGORISATION {
        int receipt_id FK
        nvarchar model "two models per receipt"
        nvarchar prompt_version
        nvarchar category
        nvarchar action "accept, set_field, not_expense, needs_human"
    }
    RECEIPT_REVIEW {
        int receipt_id FK
        nvarchar decision "expense, not_expense"
    }
    MERCHANT_RULE {
        nvarchar match_type "abn, sender"
        nvarchar match_value
        nvarchar merchant_name
        nvarchar source "manual, learned"
    }
    ICAL_SNAPSHOT {
        int snapshot_id PK
        int listing_id FK
        datetime fetched_at
        nvarchar blob_url "raw .ics in blob storage"
    }
    ICAL_EVENT {
        int snapshot_id FK
        nvarchar uid
        nvarchar confirmation_code
        date start_date
        date end_date
    }
```

## 4. The star schema (presentation layer)

Strictly derived: rebuilt from the operational tables and views on every
load, and reconciled against them.

```mermaid
erDiagram
    DIM_DATE     ||--o{ FACT_STAY_NIGHT : "night of"
    DIM_LISTING  ||--o{ FACT_STAY_NIGHT : "at"
    DIM_GUEST    ||--o{ FACT_STAY_NIGHT : "for"
    DIM_BOOKING  ||--o{ FACT_STAY_NIGHT : "part of"
    DIM_DATE     ||--o{ FACT_BOOKING : "booked, in, out"
    DIM_LISTING  ||--o{ FACT_BOOKING : "at"
    DIM_GUEST    ||--o{ FACT_BOOKING : "by"
    DIM_BOOKING  ||--|| FACT_BOOKING : "describes"
    DIM_DATE     ||--o{ FACT_AVAILABLE_NIGHT : "night of"
    DIM_LISTING  ||--o{ FACT_AVAILABLE_NIGHT : "at"
    DIM_DATE     ||--o{ FACT_EXPENSE : "month of"
    DIM_LISTING  ||--o{ FACT_EXPENSE : "share for"
    DIM_CATEGORY ||--o{ FACT_EXPENSE : "classified as"

    FACT_STAY_NIGHT {
        date date_key PK
        int listing_key PK
        int booking_key FK
        decimal rate_night
    }
    FACT_BOOKING {
        int booking_key PK
        date booked_date_key FK "role-playing"
        date checked_in_date_key FK "role-playing"
        date checked_out_date_key FK "role-playing"
        decimal net_amount
        decimal cleaning_cost
        int leadtime_days "computed"
    }
    FACT_AVAILABLE_NIGHT {
        date date_key PK
        int listing_key PK "factless: the row is the fact"
    }
    FACT_EXPENSE {
        int expense_id PK
        int listing_key PK
        date month_key PK "spread and apportioned"
        int category_key FK
        decimal amount
    }
    DIM_DATE {
        date date_key PK
        nvarchar season
        bit is_weekend
    }
    DIM_LISTING {
        int listing_key PK "surrogate"
        int listing_id "natural key"
    }
    DIM_GUEST {
        int guest_key PK
        bit is_repeat_guest
    }
    DIM_BOOKING {
        int booking_key PK
        nvarchar current_status
        bit is_cancelled
    }
    DIM_CATEGORY {
        int category_key PK
        nvarchar category_code "natural key"
    }
```

## Grain

| Table | One row is |
|---|---|
| `BOOKING` | one reservation, by confirmation code |
| `STAY_NIGHT` | one night of one booking at one listing |
| `PAYOUT` | one payment from the platform for a booking |
| `EXPENSE` | one bill or purchase, at its full amount |
| `APPORTIONMENT_RULE` | one listing's share of one category, for one period |
| `LOAN_INTEREST` | one loan's interest charge for one month |
| `RECEIPT` | one attachment from one email |
| `RECEIPT_CATEGORISATION` | one model's answer for one receipt and prompt version |
| `ICAL_EVENT` | one calendar event in one snapshot |
| `FACT_STAY_NIGHT` | one night, per listing, per booking |
| `FACT_BOOKING` | one booking |
| `FACT_AVAILABLE_NIGHT` | one bookable night, per listing |
| `FACT_EXPENSE` | one expense's share, per listing, per month |