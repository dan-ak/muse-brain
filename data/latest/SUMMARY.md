# Session: calmfocus

- **Duration** 103 s
- **EEG** 26,364 samples (100.0% of 256 Hz)
- **Recording errors** 0
- **Dropouts reported** 0
- **Cued trials** 0 (uncued session)

![overview](overview.png)

## Signal quality

Mains ratio is power at 50/60 Hz over the broadband floor. Above ~100x the
electrode is picking up more mains hum than brain signal — a contact problem,
fixed by dampening the pad and clearing hair, not by tightening the band.
Band percentages are after notching, so they describe the brain signal.

| channel | mains ratio | contact | delta | theta | alpha | beta | gamma |
|---|---|---|---|---|---|---|---|
| TP9 | 6,028x | **hum** | 55% | 26% | 13% | 5% | 1% |
| AF7 | 2x | clean | 49% | 30% | 11% | 8% | 2% |
| AF8 | 2x | clean | 64% | 18% | 8% | 8% | 2% |
| TP10 | 5,573x | **hum** | 51% | 27% | 15% | 5% | 2% |

## Loading it

```python
from analysis.session import Session
s = Session('data/latest')
eeg = s.clean_eeg()        # mains removed, 1-45 Hz
for trial in s.trials():   # condition-labelled slices
    print(trial.eyes, trial.task, trial.eeg().shape)
```
