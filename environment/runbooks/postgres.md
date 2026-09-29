# Postgres

We raised `log_min_duration_statement` last month to cut slow-query log volume
during the holiday traffic ramp. Current value is whatever's live in the
running instance -- don't take it on faith either way.

`pg_stat_statements` isn't loaded in this environment.

Services connect as `checkout_svc`, not as the `checkout` role you're using
for investigation. `pg_stat_activity.query` for another role's session isn't
visible to a non-superuser without `pg_read_all_stats`, so you can't just read
live query text off another session the way you could if everything shared
one identity.
