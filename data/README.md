# data/

Demo datasets only. `.gitignore` ignores everything in this directory except this file and
`german_credit.csv` (the public German Credit dataset as used by `examples/credit-risk`: 998
rows, after the two rows that shipped with a missing field — lines 391 and 789 of the original
file — were removed on 2026-09-05; `register_dataset` refuses a ragged CSV rather than guess the
missing value). Anything else you put here — downloads, private or regulated data,
intermediate files — stays on your machine; add a new tracked dataset only with an explicit
`!data/<file>` allowlist entry, a licence note here, and a size you would accept in every
clone (well under 1 MB).

Run outputs never belong here: they go to `runs/` (ignored) or to the artifact store.
