# Checkout SLO

Availability 99%, p95 200ms, offered load around 8 unique-customer checkouts per second.

Gateway retries twice. Product still wants that; last time someone turned retries off to "stop the storm" we just failed faster and pages stayed open.

April Sev-2 was Stripe us-east-1. Graphs looked like this one: payment-service client disconnects stacked up and checkout p95 followed.
