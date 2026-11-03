-- 53_apply_ical_changes.sql
--
-- Turns calendar changes (vw_ical_changes) into facts about bookings that
-- are in BOOKING. Idempotent: each change is recorded once.
--   dates_changed        -> a BOOKING_CORRECTION row per changed field
--   cancelled            -> a 'cancelled' BOOKING_HISTORY row, dated the day
--                           the calendar first showed it gone (approximate:
--                           it happened since the previous snapshot)
--   removed_during_stay  -> no action: an early departure and a mid-stay
--                           cancellation look the same in the feed. Listed
--                           for a person to decide (see the query below).
-- Stay nights are brought into line afterwards by 54_sync_bookings.sql.

INSERT INTO BOOKING_CORRECTION (confirmation_code, field, corrected_value, reason,
                                source, source_snapshot_id)
SELECT ch.confirmation_code, f.field, f.new_value,
       CONCAT(N'Calendar showed ', f.field, N' changed from ', f.old_value,
              N' to ', f.new_value, N' on ', ch.seen_on),
       N'ical', ch.snapshot_id
FROM vw_ical_changes ch
JOIN BOOKING b ON b.confirmation_code = ch.confirmation_code
CROSS APPLY (VALUES (N'check_in',  ch.old_start, ch.new_start),
                    (N'check_out', ch.old_end,   ch.new_end)) f(field, old_value, new_value)
WHERE ch.change_type = 'dates_changed'
  AND f.new_value <> f.old_value
  AND NOT EXISTS (SELECT 1 FROM BOOKING_CORRECTION x
                  WHERE x.confirmation_code  = ch.confirmation_code
                    AND x.field              = f.field
                    AND x.source_snapshot_id = ch.snapshot_id);

INSERT INTO BOOKING_HISTORY (booking_id, action_type, action_date, reason)
SELECT b.booking_id, 'cancelled', CAST(ch.seen_on AS DATETIME),
       N'Inferred: removed from the calendar before check-in'
FROM vw_ical_changes ch
JOIN BOOKING b ON b.confirmation_code = ch.confirmation_code
WHERE ch.change_type = 'cancelled'
  AND NOT EXISTS (SELECT 1 FROM BOOKING_HISTORY h
                  WHERE h.booking_id = b.booking_id AND h.action_type = 'cancelled');