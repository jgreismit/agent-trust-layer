# L2 policy engine plan

1. `atl/policy/markers.py`: R4 memo scanner (NFKD, accent and case insensitive, word boundaries, Pix key and account regexes).
2. `atl/policy/__init__.py`: `evaluate` as pure function, rule groups in design section 4 order (R1; R2, R9, R3; R4; R5; R10, R6, R7, R8).
3. R1 short-circuits; R2 skips R9 and R3; refuse wins over escalate; R5 sets `original_decision_id`.
4. R7 day start is midnight America/Sao_Paulo (zoneinfo); R8 looks back 24 h through `decisions_since`.
5. Tests in `tests/policy/` with an in-memory fake StoreView and an Ed25519 signed demo mandate (D1, D2, D3, S-20 to S-30, AC5).
