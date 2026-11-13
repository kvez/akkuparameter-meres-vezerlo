# BQ kalibrációs HIL diagnosztikai szkriptek (2026-09-12)

Ezek a szkriptek a `cc_offset_save()` HIL-en megfigyelt `[Errno 110]
ETIMEDOUT` hibájának kivizsgálása során készültek, session-scratchpadból
ide másolva reprodukálhatóság céljából (MAX verifier P2-2 lelet,
2026-09-12, **majd EZ A LELET MÁSODSZOR IS ELŐFORDULT** egy 4. MAX
verifier körben, ugyanezen a napon, ugyanerre a mintára — l. a fájl
végén a "Ismétlődő lelet" szakaszt): a nyers eredmények korábban csak a
session narratívájában szerepeltek, a repóban nem. Teljes eredmények és
értelmezés: `Mérések/nyers_logok/hil_bq_cal_cc_offset_save_retry_
20260912.md` (UPDATE 6/7/8 szakaszok tartalmazzák az utolsó két szkript
nyers kimenetét).

Mind a tíz szkript valós COM10 egységen, battery-disconnected (bare-
board) előfeltétellel futott. Kizárólag olvasás + a kalibrációs
munkamenet szokásos (unseal/full_access/enter_cal/.../exit_cal/seal)
parancsai — nem érint PSU/Load-ot.

- `bq_cal_phase1_step1.py` — a `CcGainCalibrationController` teljes 1.
  lépésének (Board/CC Offset) futtatása a state machine-en keresztül.
- `bq_cal_diag_after_error.py` — hiba utáni chip-állapot ellenőrzés
  (SS/FAS/CALMODE + független unseal→read→seal roundtrip).
- `bq_cal_diag_cca_polling.py` — BCA/CCA bit-polling fix várakozás
  helyett.
- `bq_cal_diag_save_retry.py` — `cc_offset_save()` növekvő várakozású
  újrapróbálkozási sorozat.
- `bq_cal_diag_save_timing_stats.py` — 20x ismételt `cc_offset_save()`
  ~1.0s réssel, válaszidő-statisztika (a determinisztikus X-O-X-O
  váltakozás forrása).
- `bq_cal_diag_save_repeated_after_full_sequence.py` — 10x
  `cc_offset_save()` fix 2.0s réssel (0/10 siker).
- `bq_cal_diag_save_gap_sweep.py` — rés-hossz sweep (0.3s→hiba,
  0.5s→siker).
- `bq_cal_diag_sleep_bit_correlation.py` — SLEEP/FULLSLEEP `CONTROL_
  STATUS` bit korrelációs mérés (csak olvasás).
- `bq_cal_diag_wakeup_then_save.py` — "pingeld ébren, majd indítsd":
  gyors ébresztő-pingelés (SLEEP=0 megerősítéssel), majd azonnali
  `cc_offset_save()` -- 8/8 hibázott a SLEEP-mód hipotézis megcáfolására
  (UPDATE 5).
- `bq_cal_diag_ti_conformant.py` — TI SLUUBF7A/SLUUBW5A-mintás
  BCA/CCA-pollozás (`>=0.6s`-enként) + `cc_offset_save()` 70ms/3-retry —
  2/2 élő reprodukálható siker (UPDATE 6-8), ez alapozza meg a
  `BOARD_OFFSET_MAX_WAIT_S`/`CC_OFFSET_MAX_WAIT_S`/`CC_OFFSET_SAVE_
  RETRY_WAIT_S`/`CC_OFFSET_SAVE_MAX_RETRIES` konstansokat
  `Prog/src/bq_gauge_calibration.py`-ban.

## Ismétlődő lelet: nyers HIL-adat pótlása a repóban (2. előfordulás)

Ez a "diagnosztikai szkript lefut, de nem kerül be a repóba, csak a
session saját narratívájába" mintázat **kétszer** fordult elő ugyanezen
a napon (először a `bq_cal_diag_save_retry.py`/`bq_cal_diag_save_timing_
stats.py` stb. esetében, másodszor `bq_cal_diag_ti_conformant.py`/
`bq_cal_diag_wakeup_then_save.py` esetében) — mindkétszer egy MAX
verifier kör vette észre. **Megelőzési szabály a jövőre**: minden HIL
diagnosztikai szkriptet AZONNAL, a futtatás UTÁN azonnal be kell másolni
ide (`Prog/tools/bq_cal_diagnostics/`) és a nyers kimenetet a HIL-logba
kell írni — NEM szabad ezt a canonical-dokumentum-frissítési körre
halasztani, mert az könnyen kimarad.
