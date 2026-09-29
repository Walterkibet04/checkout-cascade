# Payments

`stripe_latency_inject` is ON in this environment. Against real Stripe it adds 400ms. Against the mock provider it is supposed to be a no-op.

`reconcile_provider_payments` walks pending rows. It has been blamed for lock-ups before. Check `payments.status = 'pending'` volume if charge latency looks real.
