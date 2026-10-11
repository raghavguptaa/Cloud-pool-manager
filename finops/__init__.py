"""Monthly cost model for the warm-pool governor.

Everything this package produces is a **projection built from published AWS list
prices** -- it is not metered spend. No AWS bill was captured from the deployment the
model describes, and the account it ran in has since been closed, so no bill ever can
be. Read every number here as "what this architecture should cost at list price", never
as "what this architecture cost".

The rates live in `pricing.json` as data, each one carrying the AWS page it came from
and the date it was read, so a reader can audit the model line by line instead of
taking its output on trust. `cost_model.py` is the model itself and is runnable:

    python -m finops.cost_model
"""
