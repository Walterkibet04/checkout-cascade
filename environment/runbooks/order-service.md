# Order service

Checkout calls inventory reserve, then payments charge. Pool is 8 connections.

cache.meridian.svc:6379 still appears in error logs. The cluster ticket closed in July. Nobody pulled the client.

Affinity preview is a merchandising job, not checkout. It ran hot in March when a cron overlap stacked two workers.
